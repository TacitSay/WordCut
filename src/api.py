"""后端 API 模块：用 FastAPI 暴露 WordCut 的剪辑能力。

本模块只做「HTTP 适配 + 进程内状态管理」，算法全部下沉到既有模块：

* :mod:`pipeline`：``load_tokens`` / ``build_deletes`` / ``run_pipeline``；
* :mod:`subtitle`：``rebuild_srt``（字幕预览，不写盘）；
* :mod:`model`：``TimeMap``（源时间轴 ↔ 目标时间轴换算）；
* :mod:`edl_render` / :mod:`exporter`：由 :func:`pipeline.run_pipeline` 间接调用。

遵守的不变量（见 docs/不变量.md）：

* 不变量 1：源音频只读。上传后的 ``uploads/{id}.wav`` 只被读取（转写 / 导出），
  剪辑结果一律由 :func:`pipeline.run_pipeline` 写入 ``outputs/{id}/`` 下的副本。
* 不变量 2：对外所有时间字段（``start`` / ``end`` / ``duration`` /
  ``output_duration`` / ``total_deleted``）单位一律为秒（float）。
* 不变量 4：``token.id`` 由 :func:`pipeline.load_tokens` 按词序一次性生成，
  API 层只做增删引用（``deleted_ids``），从不重排、从不改写 id。

运行方式（在 ``src/`` 目录下）：``uvicorn api:app --reload --port 8000``。
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set

import numpy as np
import soundfile as sf
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

# ----------------------------------------------------------------------
# 路径引导：src/ 下的模块之间使用扁平导入（``import pipeline`` 等），
# 因此把本文件所在目录放到 sys.path 最前面，保证以任意工作目录启动都能导入。
# ----------------------------------------------------------------------
_SRC_DIR: Path = Path(__file__).resolve().parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

import media  # noqa: E402  （必须在 sys.path 调整之后导入）
import pipeline  # noqa: E402
import project_file  # noqa: E402
import segmenter  # noqa: E402
import subtitle  # noqa: E402
from model import DeleteSpan, TimeMap, Token  # noqa: E402

__all__ = [
    "app",
    "SAMPLE_RATE",
    "UPLOAD_DIR",
    "OUTPUT_DIR",
    "MODEL_DIR",
]

# ----------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------
#: 统一转码后的采样率（Hz，单声道）。全项目口径一致，避免重采样混用。
SAMPLE_RATE: int = 16_000

#: 项目根目录（``src`` 的上一级）。
BASE_DIR: Path = _SRC_DIR.parent

#: 源音频落盘目录：``uploads/{project_id}.wav``（只读，见不变量 1）。
UPLOAD_DIR: Path = BASE_DIR / "uploads"

#: 导出目录：``outputs/{project_id}/``。
OUTPUT_DIR: Path = BASE_DIR / "outputs"

#: faster-whisper 模型的绝对路径候选（按顺序探测）。
MODEL_DIR: Path = BASE_DIR / "models" / "faster-whisper-small"

#: 模型路径候选：优先绝对路径，其次相对项目根目录的相对路径。
_MODEL_CANDIDATES: Sequence[Path] = (
    Path(r"E:\WordCut\models\faster-whisper-small"),
    MODEL_DIR,
    Path("models/faster-whisper-small"),
)

#: 转写时若某个词的时间戳非法（``end <= start``）时为其补的最小间隔（秒）。
_MIN_WORD_DURATION: float = 0.02

#: 错误信息里最多列出的 token id 个数。
_MAX_LISTED_IDS: int = 20


# ----------------------------------------------------------------------
# 进程内全局状态
# ----------------------------------------------------------------------
#: 项目表：``project_id -> project dict``。
#:
#: 每个项目的字段（键名固定，前端按此消费）：
#:
#: * ``id`` (str)：项目 id；
#: * ``name`` (str)：项目名；
#: * ``audio_path`` (str)：源音频绝对路径（只读）；
#: * ``sample_rate`` (int)：源音频采样率，统一为 :data:`SAMPLE_RATE`；
#: * ``words`` (List[dict])：faster-whisper word dict 列表（未转写时为空）；
#: * ``deleted_ids`` (Set[str])：被删除的 Token id 集合；
#: * ``created_at`` (str)：创建时间（ISO-8601，UTC）。
_projects: Dict[str, Dict[str, Any]] = {}

#: 保护 :data:`_projects` 的锁（读写都加锁，临界区内只做纯内存操作）。
_PROJECTS_LOCK: threading.Lock = threading.Lock()

#: faster-whisper 模型单例（惰性加载）及其锁。
_whisper_model: Optional[Any] = None
_whisper_lock: threading.Lock = threading.Lock()


# ----------------------------------------------------------------------
# Pydantic 模型
# ----------------------------------------------------------------------
class ProjectCreated(BaseModel):
    """``POST /api/projects`` 的响应：新建项目的摘要。"""

    id: str = Field(..., description="项目 id")
    name: str = Field(..., description="项目名")
    sample_rate: int = Field(..., description="采样率（Hz），统一为 16000")
    duration: float = Field(..., description="源音频时长（秒）")
    source_kind: str = Field(
        "audio", description="源类型：audio（纯音频）/ video（含视频轨，可导出成品视频）"
    )


class WordOut(BaseModel):
    """单个词级单元的对齐结果（faster-whisper word dict 的 HTTP 形态）。"""

    seg: int = Field(..., description="所属 segment 序号")
    start: float = Field(..., description="起始时间（秒）")
    end: float = Field(..., description="结束时间（秒）")
    dur: float = Field(..., description="时长（秒）")
    word: str = Field(..., description="词文本（未 strip）")
    prob: float = Field(..., description="词置信度，范围 [0, 1]")


class TranscribeResponse(BaseModel):
    """``POST /api/projects/{pid}/transcribe`` 的响应。"""

    project_id: str = Field(..., description="项目 id")
    words: List[WordOut] = Field(..., description="词级对齐结果，按时间排序")
    duration: float = Field(..., description="源音频时长（秒）")
    elapsed_seconds: float = Field(..., description="本次转写耗时（秒）")
    model: str = Field(..., description="实际使用的模型路径")


class TokenOut(BaseModel):
    """词级单元（Token）的 HTTP 形态，字段与 :meth:`model.Token.to_dict` 一致。"""

    id: str = Field(..., description="Token id，形如 t00000，一经生成不可重排")
    text: str = Field(..., description="词文本")
    start: float = Field(..., description="源时间轴起始时间（秒）")
    end: float = Field(..., description="源时间轴结束时间（秒）")
    is_deleted: bool = Field(..., description="是否被删除（恒为 False，删除由 deleted_ids 表达）")
    confidence: float = Field(..., description="置信度，范围 [0, 1]")


class SegmentOut(BaseModel):
    """段落的 HTTP 形态（由 segmenter.segment_tokens 生成）。"""

    index: int = Field(..., description="段落序号（0 起）")
    start: float = Field(..., description="段落起始时间（秒，源时间轴）")
    end: float = Field(..., description="段落结束时间（秒，源时间轴）")
    duration: float = Field(..., description="段落时长（秒）")
    text: str = Field(..., description="段落拼接文本")
    token_ids: List[str] = Field(..., description="段落包含的 Token id（有序）")


class TokenListResponse(BaseModel):
    """``GET /api/projects/{pid}/tokens`` 的响应。"""

    project_id: str = Field(..., description="项目 id")
    sample_rate: int = Field(..., description="采样率（Hz）")
    duration: float = Field(..., description="源音频时长（秒）")
    tokens: List[TokenOut] = Field(..., description="Token 列表（时间序）")
    deleted_ids: List[str] = Field(..., description="被删除的 Token id，升序")
    segments: List[SegmentOut] = Field(
        default_factory=list, description="按分段策略切出的段落（仅影响显示）"
    )


class DeleteSpanOut(BaseModel):
    """删除区间（DeleteSpan）的 HTTP 形态，时间单位为秒。"""

    start: float = Field(..., description="起始时间（秒）")
    end: float = Field(..., description="结束时间（秒）")
    token_id: Optional[str] = Field(None, description="来源 Token id")
    cause: str = Field(..., description="删除原因：word / pause / repeat")


class TokenIdsRequest(BaseModel):
    """删除 / 恢复的请求体。"""

    token_ids: List[str] = Field(
        default_factory=list, description="要删除或恢复的 Token id 列表"
    )


class EditStateResponse(BaseModel):
    """删除 / 恢复后的最新编辑状态。"""

    project_id: str = Field(..., description="项目 id")
    deleted_ids: List[str] = Field(..., description="最新的被删 Token id，升序")
    deletes: List[DeleteSpanOut] = Field(..., description="build_deletes 的结果")
    total_deleted: float = Field(..., description="删除总时长（秒，已合并重叠）")


class SaveProjectRequest(BaseModel):
    """POST /api/projects/{pid}/save 的请求体。"""

    path: str = Field(..., description="工程文件保存路径（.wcproj）")


class LoadProjectRequest(BaseModel):
    """POST /api/projects/load 的请求体。"""

    path: str = Field(..., description="工程文件路径（.wcproj）")


class LoadProjectResponse(BaseModel):
    """POST /api/projects/load 的响应：恢复的上下文与完整性校验。"""

    project_id: str = Field(..., description="原项目 id")
    name: str = Field(..., description="项目名")
    audio_path: str = Field(..., description="源音频路径")
    sample_rate: int = Field(..., description="采样率")
    words: List[WordOut] = Field(..., description="词级对齐结果")
    deleted_ids: List[str] = Field(..., description="被删 Token id")
    tokens: List[TokenOut] = Field(..., description="重建的 Token 列表")
    segments: List[SegmentOut] = Field(default_factory=list, description="重新分段结果")
    source_kind: str = Field("audio", description="源类型：audio / video")
    integrity: Dict[str, Any] = Field(..., description="源文件校验结果")


class SaveProjectResponse(BaseModel):
    """POST /api/projects/{pid}/save 的响应。"""

    path: str = Field(..., description="实际写入的工程文件路径")
    size_bytes: int = Field(..., description="文件大小（字节）")
    modified_at: str = Field(..., description="保存时间（ISO-8601）")


class ExportRequest(BaseModel):
    """``POST /api/projects/{pid}/export`` 的请求体。"""

    audio_format: str = Field(
        "wav", description="音频格式：wav / mp3 / flac / m4a"
    )
    export_video: bool = Field(
        False, description="是否同时导出成品视频（需项目来自视频上传）"
    )
    video_mode: str = Field(
        "precise", description="视频剪辑模式：precise（fast 已禁用，自动回退）"
    )


class ExportResponse(BaseModel):
    """``POST /api/projects/{pid}/export`` 的响应。"""

    project_id: str = Field(..., description="项目 id")
    output_duration: float = Field(..., description="目标音频时长（秒）")
    paths: Dict[str, str] = Field(
        ...,
        description="导出文件绝对路径：audio_path / srt_path / edl_path / video_path(可选)",
    )
    audio_format: str = Field("wav", description="实际导出的音频格式")
    video_exported: bool = Field(False, description="是否导出了视频")
    video_info: Optional[Dict[str, Any]] = Field(
        None, description="视频导出的性能数据（mode/segments/elapsed_ms 等）"
    )
    token_count: int = Field(..., description="Token 总数")
    deleted_count: int = Field(..., description="实际被删除的 Token 数")
    total_deleted: float = Field(..., description="删除总时长（秒，已合并重叠）")


class PreviewResponse(BaseModel):
    """``GET /api/projects/{pid}/preview`` 的响应（不写盘）。"""

    project_id: str = Field(..., description="项目 id")
    format: str = Field(..., description="返回格式：srt 或 plain")
    content: str = Field(..., description="字幕预览文本；无可输出内容时为空串")


class ProjectSummary(BaseModel):
    """``GET /api/projects`` 列表项。"""

    id: str = Field(..., description="项目 id")
    name: str = Field(..., description="项目名")
    sample_rate: int = Field(..., description="采样率（Hz）")
    duration: float = Field(..., description="源音频时长（秒）")
    word_count: int = Field(..., description="词级单元数量")
    deleted_count: int = Field(..., description="被删 Token 数量")
    transcribed: bool = Field(..., description="是否已完成转写")
    created_at: str = Field(..., description="创建时间（ISO-8601，UTC）")


class HealthResponse(BaseModel):
    """``GET /api/health`` 的响应。"""

    status: str = Field(..., description="健康状态，恒为 ok")
    version: str = Field(..., description="API 版本")
    project_count: int = Field(..., description="当前内存中的项目数")
    sample_rate: int = Field(..., description="统一采样率（Hz）")


# ----------------------------------------------------------------------
# 应用对象
# ----------------------------------------------------------------------
def _read_version() -> str:
    """从项目根目录的 VERSION 文件读取版本号（单一版本源）。

    文件缺失或内容非法时回退到 0.0.0，不阻断服务启动。
    """
    version_file = BASE_DIR / "VERSION"
    try:
        text = version_file.read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0"
    return text or "0.0.0"


app = FastAPI(title="WordCut", version=_read_version())


# ----------------------------------------------------------------------
# 内部工具：状态访问
# ----------------------------------------------------------------------
def _now_iso() -> str:
    """当前 UTC 时间的 ISO-8601 字符串（秒级精度）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _get_project(project_id: str) -> Dict[str, Any]:
    """按 id 取出项目；不存在时抛 ``404``。

    返回的是项目字典引用本身；调用方若要修改 ``deleted_ids`` 等可变字段，
    必须自行持有 :data:`_PROJECTS_LOCK`。
    """
    with _PROJECTS_LOCK:
        project = _projects.get(project_id)
    if project is None:
        raise HTTPException(
            status_code=404,
            detail=f"项目 {project_id!r} 不存在（可能已被删除或服务已重启）",
        )
    return project


def _snapshot_deleted_ids(project: Dict[str, Any]) -> Set[str]:
    """在锁内取一份 ``deleted_ids`` 快照，避免迭代时被并发修改。"""
    with _PROJECTS_LOCK:
        return set(project["deleted_ids"])


def _snapshot_words(project: Dict[str, Any]) -> List[Dict[str, Any]]:
    """在锁内取一份 ``words`` 快照（浅拷贝列表，元素为不可变使用）。"""
    with _PROJECTS_LOCK:
        return list(project["words"])


def _require_words(project: Dict[str, Any]) -> List[Dict[str, Any]]:
    """取出已转写的词表；尚未转写时抛 ``400``。"""
    words = _snapshot_words(project)
    if not words:
        raise HTTPException(
            status_code=400,
            detail=f"项目 {project['id']!r} 尚未转写，请先调用 transcribe 接口",
        )
    return words


def _duration_of(project: Dict[str, Any]) -> float:
    """源音频时长（秒）：优先用样本数 / 采样率，读不到时回退到文件头信息。"""
    audio_path = Path(project["audio_path"])
    try:
        info = sf.info(str(audio_path))
    except (RuntimeError, OSError) as exc:  # pragma: no cover - 文件被外部删除
        raise HTTPException(
            status_code=500,
            detail=f"读取源音频信息失败：{audio_path}（{exc}）",
        ) from exc
    return float(info.frames) / float(info.samplerate)


def _load_samples(project: Dict[str, Any]) -> np.ndarray:
    """读取源音频样本（``float32``，单声道，只读语义，不变量 1）。"""
    audio_path = Path(project["audio_path"])
    if not audio_path.is_file():
        raise HTTPException(
            status_code=500,
            detail=f"源音频不存在：{audio_path}（请重新上传）",
        )
    try:
        data, rate = sf.read(
            str(audio_path), dtype="float32", always_2d=True
        )
    except (RuntimeError, OSError) as exc:
        raise HTTPException(
            status_code=500,
            detail=f"读取源音频失败：{audio_path}（{exc}）",
        ) from exc
    if int(rate) != int(project["sample_rate"]):
        raise HTTPException(
            status_code=500,
            detail=(
                f"源音频采样率 {rate}Hz 与项目记录 {project['sample_rate']}Hz 不一致"
            ),
        )
    mono = np.asarray(data, dtype=np.float32)
    if mono.ndim == 2 and mono.shape[1] > 1:
        mono = mono.mean(axis=1, dtype=np.float32)
    mono = np.ascontiguousarray(mono.reshape(-1), dtype=np.float32)
    if mono.size == 0:
        raise HTTPException(status_code=500, detail="源音频没有任何样本")
    return mono


def _tokens_of(
    project: Dict[str, Any], deleted_ids: Set[str]
) -> List[Token]:
    """由项目词表构造 Token 列表（id 由 :func:`pipeline.load_tokens` 生成）。"""
    try:
        return pipeline.load_tokens(_snapshot_words(project), deleted_ids)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=500, detail=f"词表数据非法，无法构造 Token：{exc}"
        ) from exc


def _deletes_of(
    tokens: Sequence[Token], deleted_ids: Set[str]
) -> List[DeleteSpan]:
    """由 Token 与被删 id 构造删除区间（cause 恒为 word）。"""
    try:
        return pipeline.build_deletes(tokens, deleted_ids)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=500, detail=f"构造删除区间失败：{exc}"
        ) from exc


def _total_deleted(deletes: Sequence[DeleteSpan]) -> float:
    """删除总时长（秒）：合并重叠区间后求和，单位与不变量 2 一致。"""
    if not deletes:
        return 0.0
    merged: List[DeleteSpan] = []
    for span in sorted(deletes, key=lambda item: (item.start, item.end)):
        if merged and span.start <= merged[-1].end:
            last = merged[-1]
            if span.end > last.end:
                merged[-1] = DeleteSpan(
                    start=last.start,
                    end=span.end,
                    token_id=last.token_id,
                    cause=last.cause,
                )
        else:
            merged.append(span)
    return float(sum(span.end - span.start for span in merged))


def _format_ids(ids: Sequence[str]) -> str:
    """把 id 列表格式化成可读文本（过长时截断）。"""
    shown = [repr(item) for item in ids[:_MAX_LISTED_IDS]]
    text = "、".join(shown)
    if len(ids) > _MAX_LISTED_IDS:
        text += f" …（共 {len(ids)} 个）"
    return text


def _validate_token_ids(token_ids: Sequence[str], known_ids: Set[str]) -> List[str]:
    """校验请求里的 token id 全部存在，返回去重后的列表；否则抛 ``400``。"""
    if not isinstance(token_ids, (list, tuple)):
        raise HTTPException(status_code=400, detail="token_ids 必须是字符串数组")
    cleaned: List[str] = []
    seen: Set[str] = set()
    for index, raw in enumerate(token_ids):
        if not isinstance(raw, str) or not raw.strip():
            raise HTTPException(
                status_code=400,
                detail=f"token_ids[{index}] 必须是非空字符串，实际为 {raw!r}",
            )
        value = raw.strip()
        if value not in seen:
            seen.add(value)
            cleaned.append(value)
    unknown = sorted(seen - known_ids)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=(
                f"有 {len(unknown)} 个 token id 不存在于当前项目：{_format_ids(unknown)}"
                "（id 由转写结果生成，重新转写后需重新拉取 tokens）"
            ),
        )
    return cleaned


def _model_candidates() -> List[Path]:
    """模型路径候选（已去重、相对路径按项目根目录解析、保持声明顺序）。"""
    resolved: List[Path] = []
    seen: Set[str] = set()
    for candidate in _MODEL_CANDIDATES:
        path = candidate if candidate.is_absolute() else BASE_DIR / candidate
        key = str(path).lower()
        if key not in seen:
            seen.add(key)
            resolved.append(path)
    return resolved


def _resolve_model_path() -> Path:
    """探测 faster-whisper 模型目录；全部不存在时抛 ``503``。"""
    candidates = _model_candidates()
    for candidate in candidates:
        if candidate.is_dir():
            return candidate.resolve()
    tried = "、".join(str(path) for path in candidates)
    raise HTTPException(
        status_code=503,
        detail=(
            "未找到 faster-whisper-small 模型目录，已尝试：" f"{tried}；"
            "请把模型放到 models/faster-whisper-small 下"
        ),
    )


def _get_whisper_model() -> Any:
    """惰性加载并复用 faster-whisper 模型（进程级单例，线程安全）。"""
    global _whisper_model
    with _whisper_lock:
        if _whisper_model is not None:
            return _whisper_model
        model_path = _resolve_model_path()
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:  # pragma: no cover - 依赖缺失
            raise HTTPException(
                status_code=503,
                detail=f"未安装 faster-whisper，无法转写：{exc}",
            ) from exc
        try:
            _whisper_model = WhisperModel(
                str(model_path), device="cpu", compute_type="int8"
            )
        except Exception as exc:  # noqa: BLE001 - 底层可能抛任意异常
            raise HTTPException(
                status_code=500,
                detail=f"加载 faster-whisper 模型失败：{model_path}（{exc}）",
            ) from exc
        return _whisper_model


def _normalize_audio(raw: bytes, suffix: str) -> np.ndarray:
    """把上传的音频字节解码成 16kHz 单声道 ``float32`` 样本。

    先落到临时文件再用 soundfile 解码（兼容 wav/flac/ogg 等 libsndfile 支持的格式），
    解码成功后临时文件立即删除；样本一律做声道混缩与线性重采样。
    """
    if not raw:
        raise HTTPException(status_code=400, detail="上传的文件为空")

    temp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=suffix or ".bin", delete=False
        ) as handle:
            handle.write(raw)
            temp_path = Path(handle.name)
        try:
            data, rate = sf.read(
                str(temp_path), dtype="float32", always_2d=True
            )
        except (RuntimeError, OSError) as exc:
            raise HTTPException(
                status_code=400,
                detail=(
                    "无法解码上传的音频文件，请确认是有效的音频格式"
                    f"（wav/flac/ogg 等）：{exc}"
                ),
            ) from exc
        if data.size == 0:
            raise HTTPException(status_code=400, detail="上传的音频没有任何样本")
        if not np.all(np.isfinite(data)):
            data = np.nan_to_num(data, nan=0.0, posinf=0.0, neginf=0.0)
        mono = np.asarray(data, dtype=np.float32)
        if mono.ndim == 2 and mono.shape[1] > 1:
            mono = mono.mean(axis=1, dtype=np.float32)
        mono = np.ascontiguousarray(mono.reshape(-1), dtype=np.float32)
        return _resample(mono, int(rate), SAMPLE_RATE)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _resample(
    samples: np.ndarray, source_rate: int, target_rate: int
) -> np.ndarray:
    """线性插值重采样到 ``target_rate``；采样率一致时原样返回。"""
    if source_rate <= 0 or target_rate <= 0:
        raise HTTPException(
            status_code=400,
            detail=f"非法采样率：source={source_rate}Hz, target={target_rate}Hz",
        )
    if source_rate == target_rate or samples.size < 2:
        return np.clip(samples, -1.0, 1.0).astype(np.float32)

    target_length = max(int(round(samples.size * target_rate / source_rate)), 1)
    old_axis = np.arange(samples.size, dtype=np.float64)
    new_axis = np.linspace(
        0.0, float(samples.size - 1), num=target_length, dtype=np.float64
    )
    resampled = np.interp(new_axis, old_axis, samples.astype(np.float64))
    return np.clip(resampled, -1.0, 1.0).astype(np.float32)


def _keep_source_media(
    raw: bytes, suffix: str, project_id: str, media_info: Dict[str, Any]
) -> Optional[Path]:
    """保留原始上传文件（仅视频），供导出时剪视频使用。

    音频文件不需要保留原始副本——统一转码后的 WAV 已包含全部信息。
    视频必须保留原文件：按 EDL 剪视频需要原始码流（重编码会损失画质与速度）。

    参数:
        raw: 上传文件的原始字节。
        suffix: 原扩展名（含点）。
        project_id: 项目 id，用于命名。
        media_info: :func:`_detect_media_kind` 的结果。

    返回:
        保留的源文件路径；非视频或保存失败时返回 None（不影响上传成功）。
    """
    if media_info.get("kind") != "video":
        return None

    target = UPLOAD_DIR / f"{project_id}.source{suffix}"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        return target
    except OSError:
        return None


def _ingest_media(raw: bytes, suffix: str, destination: Path) -> Dict[str, Any]:
    """把上传的原始字节落成 16kHz 单声道 WAV，返回时长（秒）。

    走 ffprobe 探测 + ffmpeg 转码，支持任意音视频容器（视频自动丢弃画面）。
    先写临时文件（保留原扩展名，便于 ffmpeg 识别容器），转换成功后删除。

    参数:
        raw: 上传文件的原始字节。
        suffix: 原文件扩展名（含点，如 .mp4）；未知时用 .bin。
        destination: 目标 WAV 路径（父目录自动创建）。

    返回:
        目标音频时长（秒），由 ffprobe 对输出文件探测得出。

    异常:
        400: 文件不是有效媒体，或不含音频流。
        500: ffmpeg/ffprobe 不可用或转码失败。
    """
    temp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=suffix, delete=False, dir=str(UPLOAD_DIR)
        ) as handle:
            handle.write(raw)
            temp_path = Path(handle.name)

        # 1. 探测：确认是有效媒体且含音频流
        try:
            info = media.probe(temp_path)
        except media.MediaToolError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"无法识别的媒体文件（需要 ffmpeg 可解码的音频或视频）：{exc}",
            ) from exc

        if not any(stream.type == "audio" for stream in info.streams):
            raise HTTPException(
                status_code=400,
                detail=(
                    "上传的文件中没有音频流"
                    f"（探测到：{[s.type for s in info.streams] or '无任何流'}）"
                ),
            )

        # 2. 转码：任意音视频 -> 16kHz 单声道 PCM16 WAV
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            media.convert_to_wav16k(temp_path, destination, SAMPLE_RATE)
        except media.MediaToolError as exc:
            raise HTTPException(
                status_code=500, detail=f"音频转码失败：{exc}"
            ) from exc

        # 3. 以输出文件为准取时长（转码后可能因重采样有毫秒级差异）
        try:
            out_info = media.probe(destination)
        except media.MediaToolError as exc:
            raise HTTPException(
                status_code=500, detail=f"转码后探测输出失败：{exc}"
            ) from exc

        has_video = any(stream.type == "video" for stream in info.streams)
        return {
            "duration": float(out_info.duration),
            "kind": "video" if has_video else "audio",
            "has_audio": True,
            "format_name": info.format_name,
        }
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _write_wav(destination: Path, samples: np.ndarray) -> None:
    """把样本写成 16kHz 单声道 PCM_16 WAV（目标格式统一）。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        sf.write(
            str(destination),
            np.clip(samples, -1.0, 1.0).astype(np.float32),
            SAMPLE_RATE,
            subtype="PCM_16",
        )
    except (RuntimeError, OSError) as exc:
        raise HTTPException(
            status_code=500,
            detail=f"写入音频文件失败：{destination}（{exc}）",
        ) from exc




#: 导出支持的音频格式。
_EXPORT_AUDIO_FORMATS = ("wav", "mp3", "flac", "m4a")


def _export_video(
    source_path: Path,
    target_audio: Path,
    deletes: Sequence[DeleteSpan],
    mode: str,
    output_dir: Path,
    basename: str,
) -> Dict[str, Any]:
    """按 EDL 剪视频并与剪辑后音频合流，返回性能数据与输出路径。

    两步：① cut_video 按删除区间切视频；② concat_av 用目标音频替换音轨。
    中间产物写到临时目录，合流成功后清理。

    参数:
        source_path: 原始视频路径（只读，不变量 1）。
        target_audio: 剪辑后的目标音频（供合流使用）。
        deletes: 删除区间（DeleteSpan，秒）。
        mode: 视频剪辑模式（fast 会被内部回退到 precise）。
        output_dir: 输出目录。
        basename: 文件名主名。

    返回:
        ``{"path": str, "mode": str, "segments": int, "elapsed_ms": float,
        "reencoded_fraction": float, "total_elapsed_ms": float}``。

    异常:
        HTTPException: 切视频或合流失败时返回 500。
    """
    import tempfile
    import shutil

    output_dir.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="wordcut_av_"))
    try:
        cut_path = workdir / "cut.mp4"
        final_path = output_dir / f"{basename}.mp4"

        try:
            cut_info = media.cut_video(source_path, list(deletes), cut_path, mode=mode)
            media.concat_av(cut_path, target_audio, final_path)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"视频导出失败：{exc}"
            ) from exc
        except media.MediaToolError as exc:
            raise HTTPException(
                status_code=500, detail=f"视频导出失败：{exc}"
            ) from exc

        info: Dict[str, Any] = dict(cut_info)
        info["path"] = str(final_path.resolve())
        info["total_elapsed_ms"] = round(
            float(cut_info.get("elapsed_ms", 0.0)) * 1.0, 1
        )
        return info
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _srt_to_plain(srt_text: str) -> str:
    """把 SRT 文本转成纯文本预览（去掉序号行与时间码行）。"""
    lines: List[str] = []
    for raw in srt_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.isdigit():
            continue
        if "-->" in line:
            continue
        lines.append(line)
    return "\n".join(lines)


# ----------------------------------------------------------------------
# 接口：健康检查与项目列表
# ----------------------------------------------------------------------
@app.get("/api/health", response_model=HealthResponse, summary="健康检查")
def health() -> HealthResponse:
    """返回服务存活状态与内存中的项目数量。"""
    with _PROJECTS_LOCK:
        count = len(_projects)
    return HealthResponse(
        status="ok",
        version=app.version,
        project_count=count,
        sample_rate=SAMPLE_RATE,
    )


@app.get(
    "/api/projects",
    response_model=List[ProjectSummary],
    summary="列出所有项目",
)
def list_projects() -> List[ProjectSummary]:
    """列出内存中所有项目的摘要（按创建时间倒序，最新在前）。"""
    with _PROJECTS_LOCK:
        items = list(_projects.values())

    summaries: List[ProjectSummary] = []
    for project in items:
        try:
            duration = _duration_of(project)
        except HTTPException:
            duration = 0.0
        with _PROJECTS_LOCK:
            word_count = len(project["words"])
            deleted_count = len(project["deleted_ids"])
        summaries.append(
            ProjectSummary(
                id=project["id"],
                name=project["name"],
                sample_rate=int(project["sample_rate"]),
                duration=duration,
                word_count=word_count,
                deleted_count=deleted_count,
                transcribed=word_count > 0,
                created_at=str(project["created_at"]),
            )
        )
    summaries.sort(key=lambda item: item.created_at, reverse=True)
    return summaries


# ----------------------------------------------------------------------
# 接口：上传音频
# ----------------------------------------------------------------------
@app.post(
    "/api/projects",
    response_model=ProjectCreated,
    status_code=201,
    summary="上传音频并创建项目",
)
async def create_project(
    file: UploadFile = File(..., description="音频文件（wav/flac/ogg 等）"),
    name: str = Form(..., description="项目名"),
) -> ProjectCreated:
    """上传音视频文件并创建项目。

    流程：字节落临时文件 → ffprobe 探测（校验是有效媒体）→ ffmpeg 统一转码为
    16kHz 单声道 PCM16 WAV → 落盘 ``uploads/{project_id}.wav``
    （不变量 1：此文件之后只读）。

    支持任意 ffmpeg 可解码的音频/视频容器（mp4/mov/mkv/mp3/flac/ogg/m4a…）；
    视频文件自动丢弃画面只取音轨。

    参数:
        file: 上传的音视频文件。
        name: 项目名，非空。

    返回:
        ``{id, name, sample_rate, duration}``，``duration`` 单位秒。
    """
    project_name = (name or "").strip()
    if not project_name:
        raise HTTPException(status_code=400, detail="项目名 name 不能为空")

    raw = await file.read()
    await file.close()
    if not raw:
        raise HTTPException(status_code=400, detail="上传的文件为空")

    suffix = Path(file.filename or "").suffix.lower() or ".bin"
    project_id = uuid.uuid4().hex[:12]
    audio_path = UPLOAD_DIR / f"{project_id}.wav"
    media_info = _ingest_media(raw, suffix, audio_path)
    duration = float(media_info["duration"])

    # 若上传的是视频，额外保留一份原始文件，供导出时剪视频使用（只读，不变量 1）
    source_path = _keep_source_media(raw, suffix, project_id, media_info)

    project: Dict[str, Any] = {
        "id": project_id,
        "name": project_name,
        "audio_path": str(audio_path.resolve()),
        "source_path": str(source_path.resolve()) if source_path else None,
        "source_kind": media_info.get("kind", "audio"),
        "sample_rate": SAMPLE_RATE,
        "words": [],
        "deleted_ids": set(),
        "created_at": _now_iso(),
    }
    with _PROJECTS_LOCK:
        _projects[project_id] = project

    return ProjectCreated(
        id=project_id,
        name=project_name,
        sample_rate=SAMPLE_RATE,
        duration=duration,
        source_kind=str(project["source_kind"]),
    )


# ----------------------------------------------------------------------
# 接口：转写
# ----------------------------------------------------------------------
@app.post(
    "/api/projects/{pid}/transcribe",
    response_model=TranscribeResponse,
    summary="转写项目音频（同步）",
)
def transcribe_project(pid: str) -> TranscribeResponse:
    """用 faster-whisper 对项目音频做词级对齐转写（同步执行）。

    完成后把 word dict 列表写入项目的 ``words`` 字段，并返回 ``words`` 与本次
    耗时；已存在的 ``deleted_ids`` 只保留在新词表里仍存在的 id。

    参数:
        pid: 项目 id。

    返回:
        ``{project_id, words, duration, elapsed_seconds, model}``。

    异常:
        404：项目不存在；503：模型目录缺失或 faster-whisper 未安装；
        500：模型加载或转写失败。
    """
    project = _get_project(pid)
    model = _get_whisper_model()
    model_path = _resolve_model_path()
    audio_path = Path(project["audio_path"])
    if not audio_path.is_file():
        raise HTTPException(
            status_code=500, detail=f"源音频不存在：{audio_path}（请重新上传）"
        )

    started = time.perf_counter()
    words: List[Dict[str, Any]] = []
    try:
        segments, info = model.transcribe(
            str(audio_path),
            language="zh",
            word_timestamps=True,
            vad_filter=False,
            beam_size=5,
        )
        audio_duration = float(getattr(info, "duration", 0.0) or 0.0)
        for segment in segments:
            seg_id = int(getattr(segment, "id", 0) or 0)
            for word in getattr(segment, "words", None) or []:
                start = float(getattr(word, "start", 0.0) or 0.0)
                end = float(getattr(word, "end", 0.0) or 0.0)
                if start < 0.0:
                    start = 0.0
                if not end > start:
                    end = start + _MIN_WORD_DURATION
                if audio_duration > 0.0 and start >= audio_duration:
                    continue
                probability = getattr(word, "probability", None)
                try:
                    prob = float(probability) if probability is not None else 1.0
                except (TypeError, ValueError):
                    prob = 1.0
                prob = min(max(prob, 0.0), 1.0)
                words.append(
                    {
                        "seg": seg_id,
                        "start": start,
                        "end": end,
                        "dur": end - start,
                        "word": str(getattr(word, "word", "") or ""),
                        "prob": prob,
                    }
                )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - 底层可能抛任意异常
        raise HTTPException(
            status_code=500, detail=f"转写失败：{exc}"
        ) from exc
    elapsed = float(time.perf_counter() - started)

    words.sort(key=lambda item: (item["start"], item["end"]))
    if not words:
        raise HTTPException(
            status_code=422,
            detail="转写结果为空：音频中未识别到任何词，请检查音频内容与音量",
        )

    # 不变量 4：id 由词序决定，不重排；旧删除引用只保留仍存在的 id。
    known_ids = {f"{pipeline.TOKEN_ID_PREFIX}{index:05d}" for index in range(len(words))}
    with _PROJECTS_LOCK:
        project["words"] = words
        project["deleted_ids"] &= known_ids

    return TranscribeResponse(
        project_id=pid,
        words=[WordOut(**item) for item in words],
        duration=_duration_of(project),
        elapsed_seconds=elapsed,
        model=str(model_path),
    )


# ----------------------------------------------------------------------
# 接口：Token 列表
# ----------------------------------------------------------------------
@app.get(
    "/api/projects/{pid}/tokens",
    response_model=TokenListResponse,
    summary="获取词级单元列表",
)
def get_tokens(pid: str) -> TokenListResponse:
    """返回项目的 Token 列表与当前被删 id。

    Token 由 :func:`pipeline.load_tokens` 生成：``id`` 按词序一次性生成
    （``t00000`` 起），同一份词表多次调用结果完全一致（不变量 4）。

    参数:
        pid: 项目 id。

    返回:
        ``{project_id, sample_rate, duration, tokens, deleted_ids}``。
    """
    project = _get_project(pid)
    _require_words(project)
    deleted_ids = _snapshot_deleted_ids(project)
    tokens = _tokens_of(project, deleted_ids)
    segments = segmenter.segment_tokens(tokens)
    return TokenListResponse(
        project_id=pid,
        sample_rate=int(project["sample_rate"]),
        duration=_duration_of(project),
        tokens=[TokenOut(**token.to_dict()) for token in tokens],
        deleted_ids=sorted(deleted_ids),
        segments=[SegmentOut(**seg.to_dict()) for seg in segments],
    )


# ----------------------------------------------------------------------
# 接口：删除 / 恢复
# ----------------------------------------------------------------------
@app.post(
    "/api/projects/{pid}/delete",
    response_model=EditStateResponse,
    summary="删除若干 Token",
)
def delete_tokens(pid: str, payload: TokenIdsRequest) -> EditStateResponse:
    """把给定 token id 加入项目的 ``deleted_ids``（非破坏性，源音频不变）。

    参数:
        pid: 项目 id。
        payload: ``{"token_ids": [...]}``，id 必须属于当前项目。

    返回:
        最新的 ``deleted_ids`` 与 :func:`pipeline.build_deletes` 的结果。
    """
    project = _get_project(pid)
    _require_words(project)
    deleted_ids = _snapshot_deleted_ids(project)
    tokens = _tokens_of(project, deleted_ids)
    known_ids = {token.id for token in tokens}
    ids = _validate_token_ids(payload.token_ids, known_ids)

    with _PROJECTS_LOCK:
        project["deleted_ids"].update(ids)
        current = set(project["deleted_ids"])

    deletes = _deletes_of(tokens, current)
    return EditStateResponse(
        project_id=pid,
        deleted_ids=sorted(current),
        deletes=[DeleteSpanOut(**span.to_dict()) for span in deletes],
        total_deleted=_total_deleted(deletes),
    )


@app.post(
    "/api/projects/{pid}/restore",
    response_model=EditStateResponse,
    summary="恢复若干 Token",
)
def restore_tokens(pid: str, payload: TokenIdsRequest) -> EditStateResponse:
    """把给定 token id 从项目的 ``deleted_ids`` 中移除。

    参数:
        pid: 项目 id。
        payload: ``{"token_ids": [...]}``，id 必须属于当前项目。

    返回:
        最新的 ``deleted_ids`` 与 ``build_deletes`` 的结果。
    """
    project = _get_project(pid)
    _require_words(project)
    deleted_ids = _snapshot_deleted_ids(project)
    tokens = _tokens_of(project, deleted_ids)
    known_ids = {token.id for token in tokens}
    ids = _validate_token_ids(payload.token_ids, known_ids)

    with _PROJECTS_LOCK:
        project["deleted_ids"].difference_update(ids)
        current = set(project["deleted_ids"])

    deletes = _deletes_of(tokens, current)
    return EditStateResponse(
        project_id=pid,
        deleted_ids=sorted(current),
        deletes=[DeleteSpanOut(**span.to_dict()) for span in deletes],
        total_deleted=_total_deleted(deletes),
    )


# ----------------------------------------------------------------------
# 接口：导出
# ----------------------------------------------------------------------
@app.post(
    "/api/projects/{pid}/export",
    response_model=ExportResponse,
    summary="导出剪辑结果",
)
def export_project(pid: str, payload: Optional[ExportRequest] = None) -> ExportResponse:
    """调用 :func:`pipeline.run_pipeline` 导出音频 / 字幕 / EDL 三件套。

    输出写入 ``outputs/{project_id}/``（不变量 1：源音频只读，剪辑结果是新副本）。

    参数:
        pid: 项目 id。

    返回:
        ``{project_id, output_duration, paths, token_count, deleted_count,
        total_deleted}``，``paths`` 为绝对路径字符串映射。

    异常:
        400：尚未转写，或删除后没有任何保留区间；
        404：项目不存在；500：读写 / 渲染失败。
    """
    options = payload or ExportRequest()
    audio_format = (options.audio_format or "wav").lower().lstrip(".")
    if audio_format not in _EXPORT_AUDIO_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"不支持的音频格式 {audio_format!r}，可选："
                f"{'/'.join(_EXPORT_AUDIO_FORMATS)}"
            ),
        )

    project = _get_project(pid)
    words = _require_words(project)
    deleted_ids = _snapshot_deleted_ids(project)
    samples = _load_samples(project)
    sample_rate = int(project["sample_rate"])
    output_dir = OUTPUT_DIR / pid

    try:
        result = pipeline.run_pipeline(
            words=words,
            samples=samples,
            sample_rate=sample_rate,
            deleted_ids=deleted_ids,
            output_dir=output_dir,
            basename=f"{pid}_cut",
            audio_format=audio_format,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"导出失败：{exc}") from exc
    except TypeError as exc:
        raise HTTPException(
            status_code=400, detail=f"导出参数非法：{exc}"
        ) from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(
            status_code=500, detail=f"导出落盘失败：{exc}"
        ) from exc

    raw_paths = result.get("paths") or {}
    paths: Dict[str, str] = {
        str(key): str(value) for key, value in dict(raw_paths).items()
    }
    edl = result.get("edl")
    deletes: Sequence[DeleteSpan] = (
        getattr(edl, "deletes", []) or [] if edl is not None else []
    )

    # ---- 可选：导出成品视频 ----
    video_info: Optional[Dict[str, Any]] = None
    if options.export_video:
        source_path = project.get("source_path")
        if not source_path or not Path(source_path).is_file():
            raise HTTPException(
                status_code=400,
                detail="该项目不是从视频上传的，无法导出视频",
            )
        video_info = _export_video(
            source_path=Path(source_path),
            target_audio=Path(paths["audio_path"]),
            deletes=deletes,
            mode=options.video_mode,
            output_dir=output_dir,
            basename=f"{pid}_cut",
        )
        paths["video_path"] = video_info.pop("path")

    return ExportResponse(
        project_id=pid,
        output_duration=float(result.get("output_duration", 0.0)),
        paths=paths,
        token_count=int(result.get("token_count", 0)),
        deleted_count=int(result.get("deleted_count", 0)),
        total_deleted=_total_deleted(deletes),
        audio_format=audio_format,
        video_exported=bool(video_info),
        video_info=video_info,
    )


class ProjectFileResponse(BaseModel):
    """POST /api/projects/{pid}/project-file 的响应：可直接写盘的工程文档。"""

    document: Dict[str, Any] = Field(
        ..., description="完整工程文档（与 .wcproj 内容一致）"
    )
    suggested_name: str = Field(..., description="建议的文件名（含 .wcproj）")


# ----------------------------------------------------------------------
# 接口：工程文件保存 / 加载
# ----------------------------------------------------------------------
@app.get(
    "/api/projects/{pid}/project-file",
    response_model=ProjectFileResponse,
    summary="生成工程文档（由浏览器写入本地文件）",
)
def get_project_file(pid: str) -> ProjectFileResponse:
    """返回完整工程文档，供前端用 File System Access API 直接写盘。

    与 /save 的区别：本接口不写服务器磁盘，只返回内容。
    浏览器（Chrome/Edge）可据此弹出原生保存对话框，用户选择位置后直接写入，
    无需知道绝对路径。

    参数:
        pid: 项目 id。

    返回:
        ``{document, suggested_name}``。

    异常:
        400: 源音频缺失等前置条件不满足；404: 项目不存在。
    """
    project = _get_project(pid)
    try:
        doc = project_file.build_project_doc(project)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    safe_name = "".join(
        ch for ch in str(project.get("name") or "untitled") if ch not in '\\/:*?"<>|'
    ).strip() or "untitled"
    return ProjectFileResponse(
        document=doc,
        suggested_name=f"{safe_name}{project_file.PROJECT_SUFFIX}",
    )


@app.post(
    "/api/projects/{pid}/save",
    response_model=SaveProjectResponse,
    summary="保存工程文件（.wcproj）",
)
def save_project_file(pid: str, payload: SaveProjectRequest) -> SaveProjectResponse:
    """把当前项目的编辑状态写成 .wcproj 工程文件。

    方案 A：工程文件只记录源文件路径与编辑状态，不复制音频/视频。
    保存时计算源音频的 SHA-256，重新打开时据此校验源文件是否被替换。

    参数:
        pid: 项目 id。
        payload: ``{"path": "D:/xxx/我的工程.wcproj"}``。

    返回:
        ``{path, size_bytes, modified_at}``。

    异常:
        400: 路径为空或源音频缺失；404: 项目不存在；500: 写入失败。
    """
    target = (payload.path or "").strip()
    if not target:
        raise HTTPException(status_code=400, detail="保存路径不能为空")

    project = _get_project(pid)
    path = Path(target)

    try:
        doc = project_file.save_project(project, path)
        written = path if path.suffix.lower() == project_file.PROJECT_SUFFIX else path.with_suffix(project_file.PROJECT_SUFFIX)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=500, detail=f"写入工程文件失败：{path}（{exc}）"
        ) from exc

    return SaveProjectResponse(
        path=str(written.resolve()),
        size_bytes=int(written.stat().st_size),
        modified_at=str(doc.get("modified_at", "")),
    )


@app.post(
    "/api/projects/load",
    response_model=LoadProjectResponse,
    summary="加载工程文件（.wcproj）",
)
def load_project_file(payload: LoadProjectRequest) -> LoadProjectResponse:
    """从 .wcproj 恢复一个项目（注册到内存项目表）。

    源音频存在且 hash 匹配时正常恢复；不存在或已变更时仍在响应的
    ``integrity`` 中返回原因，由前端提示用户。

    参数:
        payload: ``{"path": "D:/xxx/我的工程.wcproj"}``。

    返回:
        ``{project_id, name, audio_path, sample_rate, words, deleted_ids,
        tokens, segments, source_kind, integrity}``。

    异常:
        400: 不是合法工程文件或版本不支持；404: 文件不存在。
    """
    target = (payload.path or "").strip()
    if not target:
        raise HTTPException(status_code=400, detail="工程文件路径不能为空")

    path = Path(target)
    try:
        loaded = project_file.load_project(path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"工程文件不是合法 JSON：{exc}") from exc

    integrity = loaded["integrity"]
    if not integrity.get("audio_exists"):
        raise HTTPException(status_code=400, detail=str(integrity.get("message")))

    # 恢复为一个内存项目（沿用工程里的 id；若已存在则覆盖）
    project: Dict[str, Any] = {
        "id": loaded["id"],
        "name": loaded["name"],
        "audio_path": loaded["audio_path"],
        "source_path": loaded.get("source_path"),
        "source_kind": loaded.get("source_kind", "audio"),
        "sample_rate": loaded["sample_rate"] or SAMPLE_RATE,
        "words": loaded["words"],
        "deleted_ids": set(loaded["deleted_ids"]),
        "created_at": loaded["created_at"] or _now_iso(),
    }
    with _PROJECTS_LOCK:
        _projects[project["id"]] = project

    tokens = loaded["tokens"]
    segments = segmenter.segment_tokens(tokens)

    return LoadProjectResponse(
        project_id=project["id"],
        name=project["name"],
        audio_path=project["audio_path"],
        sample_rate=int(project["sample_rate"]),
        words=[WordOut(**item) for item in project["words"]],
        deleted_ids=sorted(project["deleted_ids"]),
        tokens=[TokenOut(**token.to_dict()) for token in tokens],
        segments=[SegmentOut(**seg.to_dict()) for seg in segments],
        source_kind=str(project["source_kind"]),
        integrity=integrity,
    )


class LoadProjectUploadResponse(BaseModel):
    """POST /api/projects/load-file 的响应（与 load 相同，额外回显文件名）。"""

    file_name: str = Field("", description="上传的工程文件名")
    project_id: str = Field(..., description="原项目 id")
    name: str = Field(..., description="项目名")
    audio_path: str = Field(..., description="源音频路径")
    sample_rate: int = Field(..., description="采样率")
    words: List[WordOut] = Field(..., description="词级对齐结果")
    deleted_ids: List[str] = Field(..., description="被删 Token id")
    tokens: List[TokenOut] = Field(..., description="重建的 Token 列表")
    segments: List[SegmentOut] = Field(default_factory=list, description="重新分段结果")
    source_kind: str = Field("audio", description="源类型：audio / video")
    integrity: Dict[str, Any] = Field(..., description="源文件校验结果")


def _restore_project_from_doc(loaded: Dict[str, Any]) -> Dict[str, Any]:
    """把 load_project 的解析结果恢复成内存项目并重新分段。"""
    integrity = loaded["integrity"]
    if not integrity.get("audio_exists"):
        raise HTTPException(status_code=400, detail=str(integrity.get("message")))

    project: Dict[str, Any] = {
        "id": loaded["id"],
        "name": loaded["name"],
        "audio_path": loaded["audio_path"],
        "source_path": loaded.get("source_path"),
        "source_kind": loaded.get("source_kind", "audio"),
        "sample_rate": loaded["sample_rate"] or SAMPLE_RATE,
        "words": loaded["words"],
        "deleted_ids": set(loaded["deleted_ids"]),
        "created_at": loaded["created_at"] or _now_iso(),
    }
    with _PROJECTS_LOCK:
        _projects[project["id"]] = project

    tokens = loaded["tokens"]
    segments = segmenter.segment_tokens(tokens)
    return {
        "project_id": project["id"],
        "name": project["name"],
        "audio_path": project["audio_path"],
        "sample_rate": int(project["sample_rate"]),
        "words": [WordOut(**item) for item in project["words"]],
        "deleted_ids": sorted(project["deleted_ids"]),
        "tokens": [TokenOut(**token.to_dict()) for token in tokens],
        "segments": [SegmentOut(**seg.to_dict()) for seg in segments],
        "source_kind": str(project["source_kind"]),
        "integrity": integrity,
    }


@app.post(
    "/api/projects/load-file",
    response_model=LoadProjectUploadResponse,
    summary="上传 .wcproj 文件内容并加载工程",
)
async def load_project_from_upload(
    file: UploadFile = File(..., description=".wcproj 工程文件"),
) -> LoadProjectUploadResponse:
    """直接接收工程文件内容并加载（浏览器沙箱拿不到绝对路径时的正路）。

    与 /api/projects/load 的区别：本接口不需要服务器可访问的路径，
    前端读文件字节直接上传即可，适用于浏览器环境与远程后端。

    参数:
        file: .wcproj 文件内容。

    返回:
        与 load 相同的恢复结果，外加 file_name。

    异常:
        400: 内容不是合法工程文件或版本不支持；
        404: 工程记录的源音频在服务器上不存在。
    """
    raw = await file.read()
    await file.close()
    if not raw:
        raise HTTPException(status_code=400, detail="上传的工程文件为空")

    try:
        doc = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=400, detail=f"工程文件不是合法 JSON：{exc}"
        ) from exc

    try:
        loaded = project_file.parse_project_doc(doc)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    restored = _restore_project_from_doc(loaded)
    return LoadProjectUploadResponse(file_name=file.filename or "", **restored)


@app.get(
    "/api/projects/{pid}/source-audio",
    summary="按工程记录的路径重新读取源音频（加载工程后恢复波形）",
)
def get_source_audio(pid: str) -> FileResponse:
    """按项目记录的 source 路径重新转码源音频，返回 16kHz 单声道 WAV 供波形使用。

    加载 .wcproj 后，浏览器无法直接读取源文件的本地路径，因此由后端
    重新从原路径转码一份 WAV 返回。若原路径不可用（文件被移动/删除），
    返回 404，前端提示用户手动重新导入。

    参数:
        pid: 项目 id。

    返回:
        WAV 文件流（audio/wav）。

    异常:
        404: 项目不存在，或源文件路径已失效；500: 转码失败。
    """
    project = _get_project(pid)

    # 优先用工程里记录的原始文件（视频会丢弃画面只取音轨）
    candidates = [
        project.get("original_media_path"),
        project.get("source_path"),
        project.get("audio_path"),
    ]
    source: Optional[Path] = None
    for item in candidates:
        if not item:
            continue
        candidate = Path(str(item))
        if candidate.is_file():
            source = candidate
            break

    if source is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "源文件路径已失效（可能被移动或删除）："
                f"{project.get('original_media_path') or project.get('source_path') or project.get('audio_path')}"
                "；请重新导入源文件"
            ),
        )

    cache_dir = OUTPUT_DIR / pid
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache = cache_dir / "source_preview.wav"

    try:
        # 已经是目标格式的 WAV 直接复用，避免重复转码
        if source.suffix.lower() == ".wav" and source.name.endswith(".wav"):
            if source.stat().st_size > 44:
                return FileResponse(str(source), media_type="audio/wav", filename="source.wav")
        media.convert_to_wav16k(source, cache, SAMPLE_RATE)
    except media.MediaToolError as exc:
        raise HTTPException(
            status_code=500, detail=f"读取源音频失败：{exc}"
        ) from exc

    return FileResponse(str(cache), media_type="audio/wav", filename="source.wav")


# ----------------------------------------------------------------------
# 接口：字幕预览
# ----------------------------------------------------------------------
@app.get(
    "/api/projects/{pid}/preview",
    response_model=PreviewResponse,
    summary="字幕预览（不写盘）",
)
def preview_subtitle(
    pid: str,
    srt: bool = Query(True, description="true 返回 SRT 原文，false 返回纯文本"),
) -> PreviewResponse:
    """按当前删除状态重建字幕预览，不写盘。

    字幕时间一律经 :class:`model.TimeMap` 换算到目标时间轴（不变量 6），
    与导出结果保持同一口径。

    参数:
        pid: 项目 id。
        srt: ``True``（默认）返回 SRT 原文；``False`` 返回去掉时间码的纯文本。

    返回:
        ``{project_id, format, content}``；没有任何可输出内容时 ``content`` 为空串。
    """
    project = _get_project(pid)
    _require_words(project)
    deleted_ids = _snapshot_deleted_ids(project)
    tokens = _tokens_of(project, deleted_ids)
    deletes = _deletes_of(tokens, deleted_ids)

    try:
        time_map = TimeMap.from_deletes(deletes)
        srt_text = subtitle.rebuild_srt(tokens, time_map, deleted_ids)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=500, detail=f"生成字幕预览失败：{exc}"
        ) from exc

    if srt:
        return PreviewResponse(project_id=pid, format="srt", content=srt_text)
    return PreviewResponse(
        project_id=pid, format="plain", content=_srt_to_plain(srt_text)
    )


# ----------------------------------------------------------------------
# 本地调试入口
# ----------------------------------------------------------------------
if __name__ == "__main__":  # pragma: no cover - 仅本地调试
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
