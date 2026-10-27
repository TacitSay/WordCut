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
from pydantic import BaseModel, Field

# ----------------------------------------------------------------------
# 路径引导：src/ 下的模块之间使用扁平导入（``import pipeline`` 等），
# 因此把本文件所在目录放到 sys.path 最前面，保证以任意工作目录启动都能导入。
# ----------------------------------------------------------------------
_SRC_DIR: Path = Path(__file__).resolve().parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

import pipeline  # noqa: E402  （必须在 sys.path 调整之后导入）
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


class TokenListResponse(BaseModel):
    """``GET /api/projects/{pid}/tokens`` 的响应。"""

    project_id: str = Field(..., description="项目 id")
    sample_rate: int = Field(..., description="采样率（Hz）")
    duration: float = Field(..., description="源音频时长（秒）")
    tokens: List[TokenOut] = Field(..., description="Token 列表（时间序）")
    deleted_ids: List[str] = Field(..., description="被删除的 Token id，升序")


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


class ExportResponse(BaseModel):
    """``POST /api/projects/{pid}/export`` 的响应。"""

    project_id: str = Field(..., description="项目 id")
    output_duration: float = Field(..., description="目标音频时长（秒）")
    paths: Dict[str, str] = Field(
        ..., description="导出文件绝对路径：audio_path / srt_path / edl_path"
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
app = FastAPI(title="WordCut", version="0.1.0")


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
    """上传音频文件并创建项目。

    流程：读入字节 → soundfile 解码校验 → 混缩为单声道 → 重采样到 16kHz →
    落盘 ``uploads/{project_id}.wav``（不变量 1：此文件之后只读）。

    参数:
        file: 上传的音频文件。
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

    suffix = Path(file.filename or "").suffix.lower()
    samples = _normalize_audio(raw, suffix)

    project_id = uuid.uuid4().hex[:12]
    audio_path = UPLOAD_DIR / f"{project_id}.wav"
    _write_wav(audio_path, samples)

    duration = float(samples.size) / float(SAMPLE_RATE)
    project: Dict[str, Any] = {
        "id": project_id,
        "name": project_name,
        "audio_path": str(audio_path.resolve()),
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
    return TokenListResponse(
        project_id=pid,
        sample_rate=int(project["sample_rate"]),
        duration=_duration_of(project),
        tokens=[TokenOut(**token.to_dict()) for token in tokens],
        deleted_ids=sorted(deleted_ids),
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
def export_project(pid: str) -> ExportResponse:
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

    return ExportResponse(
        project_id=pid,
        output_duration=float(result.get("output_duration", 0.0)),
        paths=paths,
        token_count=int(result.get("token_count", 0)),
        deleted_count=int(result.get("deleted_count", 0)),
        total_deleted=_total_deleted(deletes),
    )


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
