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
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import numpy as np
import soundfile as sf
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

# ----------------------------------------------------------------------
# 路径引导：src/ 下的模块之间使用扁平导入（``import pipeline`` 等），
# 因此把本文件所在目录放到 sys.path 最前面，保证以任意工作目录启动都能导入。
# ----------------------------------------------------------------------
_SRC_DIR: Path = Path(__file__).resolve().parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

import audio_fx  # noqa: E402
import media  # noqa: E402  （必须在 sys.path 调整之后导入）
import edl_render  # noqa: E402
import pipeline  # noqa: E402
import project_file  # noqa: E402
import segmenter  # noqa: E402
import store as store_mod  # noqa: E402
import silence  # noqa: E402
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

#: 上传落盘的分块大小（8MB）：2 小时视频不再一次性读进内存。
_UPLOAD_CHUNK_BYTES: int = 8 * 1024 * 1024

#: 超过该时长（秒）的素材，波形改用低码率 MP3 预览（避免浏览器解码巨型 WAV）。
_COMPACT_PREVIEW_MIN_SECONDS: float = 1800.0

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
#: 项目持久化仓库（SQLite）。后端重启后项目不再丢失。
DB_PATH: Path = BASE_DIR / "data" / "wordcut.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
_store: store_mod.Repository = store_mod.Repository(DB_PATH)

#: 兼容旧代码的轻量视图：多数接口改为经 _store 读写，
#: 保留 _projects 仅用于兼容外部测试脚本直接清空内存表。
_projects: Dict[str, Dict[str, Any]] = {}

#: 保护 :data:`_projects` 的锁（读写都加锁，临界区内只做纯内存操作）。
_PROJECTS_LOCK: threading.Lock = threading.Lock()

#: faster-whisper 模型单例（惰性加载）及其锁。
_whisper_model: Optional[Any] = None
_whisper_lock: threading.Lock = threading.Lock()

#: 实际生效的转写后端信息（``{"device": "cuda", "compute_type": "float16"}``）。
_whisper_runtime: Dict[str, Any] = {
    "device": "",
    "compute_type": "",
    "fallback_reason": "",
}

#: 需要 onnxruntime；缺失时自动退回顺序转写
_batched_pipeline: Optional[Any] = None
_batched_pipeline_model: Optional[Any] = None

#: 长素材分段转写的块长（秒）：每块完成即落库，前端可"先看到一部分"
_TRANSCRIBE_CHUNK_SECONDS: float = 600.0

#: 设备策略环境变量：``auto``（默认）/ ``cuda`` / ``cpu``。
_WHISPER_DEVICE_ENV: str = "WORDCUT_WHISPER_DEVICE"
#: 计算精度环境变量：不设时按设备取默认（cuda→float16，cpu→int8）。
_WHISPER_COMPUTE_ENV: str = "WORDCUT_WHISPER_COMPUTE"


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


class SilenceSuggestRequest(BaseModel):
    """POST /api/projects/{pid}/silences 的请求体。"""

    min_silence: float = Field(0.8, description="最短静音时长（秒）")
    threshold_db: float = Field(-38.0, description="静音判定阈值（dBFS）")
    padding: float = Field(0.08, description="静音段两端各收缩（秒）")


class SilenceSuggestResponse(BaseModel):
    """POST /api/projects/{pid}/silences 的响应。"""

    project_id: str = Field(..., description="项目 id")
    silences: List[Dict[str, Any]] = Field(..., description="检测到的静音段（秒）")
    deletes: List[DeleteSpanOut] = Field(..., description="建议删除区间")
    total_silence: float = Field(..., description="静音总时长（秒）")
    total_deleted: float = Field(..., description="建议删除总时长（秒）")


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
    audio_params: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "可选的音频后处理覆盖：gain_db / normalize / fade_in / fade_out。"
            "不传则使用项目已保存的参数（POST /api/projects/{pid}/audio-params）"
        ),
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
    whisper_device: str = Field(
        "", description="转写设备（cuda / cpu）；模型尚未加载时为空"
    )
    whisper_compute_type: str = Field(
        "", description="转写计算精度（float16 / int8 等）；未加载时为空"
    )
    whisper_note: str = Field(
        "", description="转写后端说明（例如 CUDA 不可用的原因）"
    )
    cuda_devices: int = Field(0, description="可用的 CUDA 设备数（0 表示无 GPU）")


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

# 允许本地前端开发服务器跨域访问（浏览器把 localhost 与 127.0.0.1 视为不同源）。
# 这是本地单机工具，放开本地源不构成安全风险；生产部署时按需收紧。
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)


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
    project = _store.get(project_id)
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


def _resume_offset(
    words: Sequence[Dict[str, Any]], duration: float
) -> Tuple[float, List[Dict[str, Any]]]:
    """判断能否断点续转，返回 ``(起点秒, 复用的词表)``。

    场景：长素材按 10 分钟分块转写，中途服务被重启（或用户关了启动窗口），
    已完成的块已经落库，但内存里的任务状态没了。再次点击"转写"时如果从头
    重跑，既浪费已完成的部分，也会把用户在被截断文本上做的删除标记冲掉。

    判断规则：
    * 没有词表 → 从头转（``(0, [])``）；
    * 最后一个词的结束时间已经贴近总时长（差 < 60 秒）→ 视为已转完，从头重转；
    * 否则从"最后一个词结束处"接着转，并复用已有词表。

    参数:
        words: 项目当前已落库的词表（按时间升序）。
        duration: 音频总时长（秒）。

    返回:
        ``(resume_from, kept_words)``。
    """
    if not words:
        return 0.0, []
    try:
        last_end = float(words[-1].get("end") or 0.0)
    except (TypeError, ValueError):
        return 0.0, []
    if duration <= 0.0 or last_end <= 0.0:
        return 0.0, []
    if duration - last_end < 60.0:
        # 基本转完了：从头重转，避免"半截词表 + 新词表"拼接出重复内容
        return 0.0, []
    # 严格从断点续：**不回退、不重叠**。
    #
    # 词 id 是按位置编号的（TOKEN_ID_PREFIX + 序号），如果回退半块重转，
    # 重转区域产出的词数一旦与原结果不同，后面所有 id 都会位移，
    # 用户之前在那段文本上做的删除标记就会指到别的词上。
    # 代价是分块边界处可能有一个被切断的词，但保住了编辑结果。
    return last_end, list(words)


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


def _cuda_device_count() -> int:
    """探测可用的 CUDA 设备数；探测失败（无 GPU / 缺 ctranslate2）返回 0。"""
    try:
        import ctranslate2

        return int(ctranslate2.get_cuda_device_count())
    except Exception:  # noqa: BLE001 - 探测失败一律当作"没有 GPU"
        return 0


#: CUDA 可用性探测结果缓存：``None`` 表示还没探测过。
_cuda_usable: Optional[bool] = None
#: 探测失败原因（用于健康检查与错误提示）。
_cuda_probe_reason: str = ""

#: 本项目自带的 CUDA 运行库目录（由 ``scripts/fetch_cuda_libs.py`` 解包）。
_LOCAL_CUDA_BIN: Path = Path(__file__).resolve().parent.parent / "libs" / "cuda" / "bin"

#: ``os.add_dll_directory`` 返回的句柄：必须持有，释放后目录就不再被搜索。
_cuda_dll_handles: List[Any] = []


def _cuda_dll_dirs() -> List[Path]:
    """收集可能存放 CUDA DLL 的目录（本项目自带 + pip 装的 nvidia-* 包）。"""
    dirs: List[Path] = []
    if _LOCAL_CUDA_BIN.is_dir():
        dirs.append(_LOCAL_CUDA_BIN)
    nvidia_root = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    if nvidia_root.is_dir():
        for sub in sorted(nvidia_root.glob("*/bin")):
            if sub.is_dir():
                dirs.append(sub)
    return dirs


def _prepare_cuda_dll_paths() -> List[str]:
    """把 CUDA DLL 目录加入 Windows DLL 搜索路径，返回已加入的目录字符串。

    ctranslate2 通过 ``LoadLibrary`` 找 ``cublas64_12.dll`` / ``cudnn_*64_8.dll``，
    只认 PATH 与 ``os.add_dll_directory`` 注册过的目录。ctranslate2 自带的
    ``cudnn64_8.dll`` 只是个 0.3MB 的转发壳，真正的算子库必须另外提供。
    """
    added: List[str] = []
    for directory in _cuda_dll_dirs():
        path_text = str(directory)
        if path_text in added:
            continue
        added.append(path_text)
        # 1) 加进 PATH（部分库内部用 LoadLibrary 裸名字加载）
        current = os.environ.get("PATH", "")
        if path_text not in current.split(os.pathsep):
            os.environ["PATH"] = path_text + os.pathsep + current
        # 2) 注册 DLL 目录（Python 3.8+ 在 Windows 上默认不再搜索 PATH，
        #    必须显式注册，否则 ctranslate2 仍找不到依赖）
        if hasattr(os, "add_dll_directory"):
            try:
                _cuda_dll_handles.append(os.add_dll_directory(path_text))
            except OSError:
                pass
    return added

#: 探测子进程的源码：只做"加载模型 + 转写 0.5 秒静音"。
#: 关键点是**必须放在子进程里**：cuDNN 版本不匹配时 ctranslate2 是硬崩溃
#: （0xC0000005 / DLL 加载失败），在同一进程里 try/except 根本抓不住，
#: 会把整个后端带走。
_CUDA_PROBE_CODE: str = r"""
import json, os, sys

for directory in json.loads(sys.argv[2]):
    if os.path.isdir(directory):
        os.environ["PATH"] = directory + os.pathsep + os.environ.get("PATH", "")
        if hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(directory)
            except OSError:
                pass

import numpy as np
from faster_whisper import WhisperModel
model = WhisperModel(sys.argv[1], device="cuda", compute_type="float16")
audio = np.zeros(8000, dtype=np.float32)
list(model.transcribe(audio, language="zh", word_timestamps=True, beam_size=1)[0])
print("CUDA_OK")
"""


def _probe_cuda_usable(model_path: Path) -> bool:
    """在独立子进程里验证 CUDA 是否真的可用（结果缓存）。

    为什么值得多花几秒：GPU 能把 2 小时素材的转写从"几十分钟"压到"几分钟"，
    但 cuDNN/cuBLAS 缺失时 ctranslate2 会直接崩进程。子进程探测既能拿到结论，
    又保证崩溃不影响正在运行的后端。

    参数:
        model_path: faster-whisper 模型目录。

    返回:
        ``True`` 表示 CUDA 可正常推理。
    """
    global _cuda_usable, _cuda_probe_reason
    if _cuda_usable is not None:
        return _cuda_usable

    if _cuda_device_count() <= 0:
        _cuda_usable = False
        _cuda_probe_reason = "未检测到 CUDA 设备"
        return False

    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                _CUDA_PROBE_CODE,
                str(model_path),
                json.dumps([str(item) for item in _cuda_dll_dirs()]),
            ],
            capture_output=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        _cuda_usable = False
        _cuda_probe_reason = "CUDA 探测超时（300s）"
        return False
    except OSError as exc:
        _cuda_usable = False
        _cuda_probe_reason = f"无法启动探测子进程：{exc}"
        return False

    if completed.returncode == 0 and b"CUDA_OK" in (completed.stdout or b""):
        _cuda_usable = True
        _cuda_probe_reason = ""
        return True

    stderr = (completed.stderr or b"").decode("utf-8", errors="replace")
    tail = stderr.strip().splitlines()[-1] if stderr.strip() else ""
    _cuda_usable = False
    _cuda_probe_reason = (
        f"CUDA 不可用（退出码 {completed.returncode}）：{tail or '子进程异常退出'}"
    )
    return False


def _resolve_whisper_device() -> Tuple[str, str]:
    """决定转写用的 ``(device, compute_type)``。

    策略（环境变量可覆盖，便于论文里做 CPU/GPU 对照实验）：

    * ``WORDCUT_WHISPER_DEVICE=auto``（默认）：先用子进程探测 CUDA，可用才用
      ``cuda``，否则 ``cpu``；
    * ``WORDCUT_WHISPER_DEVICE=cuda|cpu``：强制指定（``cuda`` 仍会先探测，
      探测不过就回退 CPU，避免服务被硬崩溃带走）；
    * ``WORDCUT_WHISPER_COMPUTE``：计算精度，默认 ``cuda→float16`` / ``cpu→int8``。

    返回:
        ``(device, compute_type)``。
    """
    requested = (os.environ.get(_WHISPER_DEVICE_ENV) or "auto").strip().lower()
    compute = (os.environ.get(_WHISPER_COMPUTE_ENV) or "").strip()

    if requested == "cpu":
        device = "cpu"
    else:
        model_path = _resolve_model_path()
        device = "cuda" if _probe_cuda_usable(model_path) else "cpu"

    if not compute:
        compute = "float16" if device == "cuda" else "int8"
    return device, compute


def _get_whisper_model() -> Any:
    """惰性加载并复用 faster-whisper 模型（进程级单例，线程安全）。

    默认优先用 GPU（fp16）：2 小时素材在 CPU 上要跑几十分钟，GPU 是可用性的前提。
    GPU 初始化失败时自动回退 CPU/int8 并把原因记进 :data:`_whisper_runtime`，
    避免"有显卡但驱动/cuDNN 不匹配"直接让转写不可用。
    """
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

        device, compute_type = _resolve_whisper_device()
        fallback_reason = ""
        if device == "cuda":
            # 让 ctranslate2 能找到 cublas64_12.dll / cudnn_*64_8.dll
            _prepare_cuda_dll_paths()
        try:
            _whisper_model = WhisperModel(
                str(model_path), device=device, compute_type=compute_type
            )
        except Exception as exc:  # noqa: BLE001 - 底层可能抛任意异常
            if device != "cuda":
                raise HTTPException(
                    status_code=500,
                    detail=f"加载 faster-whisper 模型失败：{model_path}（{exc}）",
                ) from exc
            # GPU 起不来（缺 cuBLAS/cuDNN、显存不足等）：回退 CPU，保证还能用
            fallback_reason = str(exc)
            try:
                _whisper_model = WhisperModel(
                    str(model_path), device="cpu", compute_type="int8"
                )
            except Exception as cpu_exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=500,
                    detail=(
                        f"加载 faster-whisper 模型失败：GPU 与 CPU 均不可用"
                        f"（GPU: {exc}；CPU: {cpu_exc}）"
                    ),
                ) from cpu_exc
            device, compute_type = "cpu", "int8"

        _whisper_runtime.update(
            {
                "device": device,
                "compute_type": compute_type,
                "fallback_reason": fallback_reason,
            }
        )
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
    source_file: Path, suffix: str, project_id: str, media_info: Dict[str, Any]
) -> Optional[Path]:
    """保留原始上传文件（仅视频），供导出时剪视频使用。

    音频文件不需要保留原始副本——统一转码后的 WAV 已包含全部信息。
    视频必须保留原文件：按 EDL 剪视频需要原始码流（重编码会损失画质与速度）。

    参数:
        source_file: 已落盘的原始上传文件（只读，本函数用 ``copy`` 复制）。
        suffix: 原扩展名（含点）。
        project_id: 项目 id，用于命名。
        media_info: :func:`_ingest_media` 的结果。

    返回:
        保留的源文件路径；非视频或保存失败时返回 None（不影响上传成功）。
    """
    if media_info.get("kind") != "video":
        return None

    target = UPLOAD_DIR / f"{project_id}.source{suffix}"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_file, target)
        return target
    except OSError:
        return None


def _ingest_media(source_file: Path, destination: Path) -> Dict[str, Any]:
    """把已落盘的上传文件转成 16kHz 单声道 WAV，返回媒体信息。

    走 ffprobe 探测 + ffmpeg 转码，支持任意音视频容器（视频自动丢弃画面）。

    参数:
        source_file: 已落盘的上传文件（只读，不会被修改）。
        destination: 目标 WAV 路径（父目录自动创建）。

    返回:
        ``{duration, kind, has_audio, format_name}``，``duration`` 单位秒。

    异常:
        400: 文件不是有效媒体，或不含音频流。
        500: ffmpeg/ffprobe 不可用或转码失败。
    """
    # 1. 探测：确认是有效媒体且含音频流
    try:
        info = media.probe(source_file)
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
        media.convert_to_wav16k(source_file, destination, SAMPLE_RATE)
    except media.MediaToolError as exc:
        raise HTTPException(status_code=500, detail=f"音频转码失败：{exc}") from exc

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
    on_stage: Optional[Any] = None,
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
            # 音频 AAC 编码与视频裁剪并行：视频走 GPU 编码、音频走 CPU，
            # 2 小时素材能省掉约 100 秒的串行等待。
            aac_path = workdir / "audio.m4a"
            audio_error: List[Any] = []

            def _encode_audio() -> None:
                try:
                    media.encode_audio_aac(target_audio, aac_path)
                except Exception as exc:  # noqa: BLE001 - 失败就走串行兜底
                    audio_error.append(exc)

            audio_thread = threading.Thread(target=_encode_audio, daemon=True)
            audio_thread.start()

            cut_info = media.cut_video(
                source_path,
                list(deletes),
                cut_path,
                mode=mode,
                on_progress=(
                    (lambda done, total, kept: on_stage("video", 0.4 + 0.5 * (done / max(1, total))))
                    if on_stage is not None
                    else None
                ),
            )
            audio_thread.join()
            if on_stage is not None:
                try:
                    on_stage("mux", 0.9)
                except Exception:  # noqa: BLE001
                    pass
            if aac_path.is_file() and not audio_error:
                # 音频已编码好：合流只做流复制，几秒完成
                media.mux_av(cut_path, aac_path, final_path)
            else:
                # 兜底：现场编码 AAC（会慢约 100 秒，但保证能出片）
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
    """返回服务存活状态、项目数量与转写后端信息。"""
    count = len(_store.list_all())
    return HealthResponse(
        status="ok",
        version=app.version,
        project_count=count,
        sample_rate=SAMPLE_RATE,
        whisper_device=str(_whisper_runtime.get("device") or ""),
        whisper_compute_type=str(_whisper_runtime.get("compute_type") or ""),
        whisper_note=str(_cuda_probe_reason or _whisper_runtime.get("fallback_reason") or ""),
        cuda_devices=_cuda_device_count(),
    )


@app.get(
    "/api/projects",
    response_model=List[ProjectSummary],
    summary="列出所有项目",
)
def list_projects() -> List[ProjectSummary]:
    """列出内存中所有项目的摘要（按创建时间倒序，最新在前）。"""
    items = _store.list_all()

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
# 接口：项目详情（切换到已有项目）
# ----------------------------------------------------------------------
class ProjectDetailResponse(BaseModel):
    """``GET /api/projects/{pid}`` 的响应：足以恢复整个编辑界面。"""

    project_id: str = Field(..., description="项目 id")
    name: str = Field(..., description="项目名")
    audio_path: str = Field(..., description="工程内 16kHz WAV 路径")
    sample_rate: int = Field(..., description="采样率（Hz）")
    duration: float = Field(..., description="源音频时长（秒）")
    source_kind: str = Field("audio", description="原始素材类型：audio / video")
    words: List[WordOut] = Field(default_factory=list, description="词级对齐结果")
    deleted_ids: List[str] = Field(default_factory=list, description="被删 Token id")
    tokens: List[TokenOut] = Field(default_factory=list, description="Token 列表")
    segments: List[SegmentOut] = Field(default_factory=list, description="段落")


@app.get(
    "/api/projects/{pid}",
    response_model=ProjectDetailResponse,
    summary="取项目详情（用于切换到已有项目）",
)
def get_project_detail(pid: str) -> ProjectDetailResponse:
    """返回一个项目当前完整的编辑状态，前端据此直接恢复界面。

    与「打开工程文件（.wcproj）」的区别：本接口读的是后端数据库里的项目，
    不需要用户提供任何路径；波形音频仍走 ``/source-audio`` 取回。

    参数:
        pid: 项目 id。

    返回:
        ``{project_id, name, audio_path, sample_rate, duration, source_kind,
        words, deleted_ids, tokens, segments}``。

    异常:
        404: 项目不存在。
    """
    project = _get_project(pid)
    try:
        duration = _duration_of(project)
    except HTTPException:
        duration = 0.0

    words = [WordOut(**item) for item in (project.get("words") or [])]
    deleted_ids = _snapshot_deleted_ids(project)
    tokens = _tokens_of(project, deleted_ids)
    segments = segmenter.segment_tokens(tokens)

    return ProjectDetailResponse(
        project_id=str(project["id"]),
        name=str(project["name"]),
        audio_path=str(project.get("audio_path") or ""),
        sample_rate=int(project["sample_rate"]),
        duration=duration,
        source_kind=str(project.get("source_kind") or "audio"),
        words=words,
        deleted_ids=sorted(deleted_ids),
        tokens=[TokenOut(**token.to_dict()) for token in tokens],
        segments=[SegmentOut(**seg.to_dict()) for seg in segments],
    )


# ----------------------------------------------------------------------
# 接口：项目重命名 / 删除
# ----------------------------------------------------------------------
class RenameProjectRequest(BaseModel):
    """``PATCH /api/projects/{pid}`` 的请求体。"""

    name: str = Field(..., description="新的项目名（去空格后不能为空）")


class DeletedProjectResponse(BaseModel):
    """``DELETE /api/projects/{pid}`` 的响应。"""

    project_id: str = Field(..., description="被删除的项目 id")
    removed_files: List[str] = Field(
        default_factory=list, description="一并删除的运行时文件路径（未清理时为空）"
    )


@app.patch(
    "/api/projects/{pid}",
    response_model=ProjectSummary,
    summary="重命名项目",
)
def rename_project(pid: str, payload: RenameProjectRequest) -> ProjectSummary:
    """修改项目名（只改数据库里的 name，不动任何音频文件）。

    参数:
        pid: 项目 id。
        payload: ``{name}``；首尾空格会被去掉，空名直接拒绝。

    返回:
        更新后的项目摘要。

    异常:
        400: 新名称为空；404: 项目不存在。
    """
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="项目名不能为空")

    project = _get_project(pid)
    project["name"] = name
    _store.put(project)

    try:
        duration = _duration_of(project)
    except HTTPException:
        duration = 0.0
    word_count = len(project.get("words") or [])
    delete_count = len(project.get("deleted_ids") or ())
    return ProjectSummary(
        id=str(project["id"]),
        name=str(project["name"]),
        sample_rate=int(project["sample_rate"]),
        duration=duration,
        word_count=word_count,
        deleted_count=delete_count,
        transcribed=word_count > 0,
        created_at=str(project["created_at"]),
    )


@app.delete(
    "/api/projects/{pid}",
    response_model=DeletedProjectResponse,
    summary="删除项目",
)
def delete_project(
    pid: str,
    purge_files: bool = Query(
        False, description="是否同时删除 uploads/ 与 outputs/ 下的运行时文件"
    ),
) -> DeletedProjectResponse:
    """从数据库删除项目；可选连带清理运行时文件。

    默认 **只删数据库记录**（不碰源音频，遵循"源文件只读"的谨慎口径）：
    误删项目还能重新导入源文件恢复；传 ``purge_files=true`` 才额外清理
    ``uploads/{pid}.*`` 与 ``outputs/{pid}/``——这两个目录里都是本工具生成的
    副本，删掉不影响用户的原始素材。

    参数:
        pid: 项目 id。
        purge_files: 是否清理运行时副本。

    返回:
        ``{project_id, removed_files}``。

    异常:
        404: 项目不存在。
    """
    if not _store.exists(pid):
        raise HTTPException(status_code=404, detail=f"项目不存在：{pid}")

    removed: List[str] = []
    if purge_files:
        for candidate in UPLOAD_DIR.glob(f"{pid}.*"):
            try:
                candidate.unlink()
                removed.append(str(candidate))
            except OSError:
                continue
        project_dir = OUTPUT_DIR / pid
        if project_dir.is_dir():
            try:
                shutil.rmtree(project_dir)
                removed.append(str(project_dir))
            except OSError:
                pass

    _store.delete(pid)
    return DeletedProjectResponse(project_id=pid, removed_files=removed)


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

    suffix = Path(file.filename or "").suffix.lower() or ".bin"
    project_id = uuid.uuid4().hex[:12]
    audio_path = UPLOAD_DIR / f"{project_id}.wav"

    # 分块落盘：2 小时视频动辄几个 GB，一次性 read() 进内存会直接 OOM。
    # 边读边写临时文件，内存占用恒定在一个块（8MB）以内。
    upload_temp: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=suffix, delete=False, dir=str(UPLOAD_DIR)
        ) as handle:
            upload_temp = Path(handle.name)
            while True:
                chunk = await file.read(_UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                handle.write(chunk)
        await file.close()

        if upload_temp.stat().st_size == 0:
            raise HTTPException(status_code=400, detail="上传的文件为空")

        media_info = _ingest_media(upload_temp, audio_path)
        duration = float(media_info["duration"])

        # 若上传的是视频，额外保留一份原始文件，供导出时剪视频使用（只读，不变量 1）
        source_path = _keep_source_media(upload_temp, suffix, project_id, media_info)
    finally:
        if upload_temp is not None:
            upload_temp.unlink(missing_ok=True)

    project: Dict[str, Any] = {
        "id": project_id,
        "name": project_name,
        "audio_path": str(audio_path.resolve()),
        "source_path": str(source_path.resolve()) if source_path else None,
        "source_kind": media_info.get("kind", "audio"),
        "sample_rate": SAMPLE_RATE,
        "words": [],
        "deleted_ids": set(),
        "audio_params": {},
        "created_at": _now_iso(),
    }
    _store.put(project)

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

    started = time.perf_counter()
    try:
        words, audio_duration = _run_transcription(project, model)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - 底层可能抛任意异常
        raise HTTPException(
            status_code=500, detail=f"转写失败：{exc}"
        ) from exc
    elapsed = float(time.perf_counter() - started)

    _apply_transcription(pid, project, words)

    return TranscribeResponse(
        project_id=pid,
        words=[WordOut(**item) for item in words],
        duration=_duration_of(project),
        elapsed_seconds=elapsed,
        model=str(model_path),
    )


def _extract_words(segments: Any, offset: float = 0.0, limit: float = 0.0) -> List[Dict[str, Any]]:
    """把 faster-whisper 的 segment 流拍平成 word dict 列表。

    参数:
        segments: ``model.transcribe`` 返回的生成器。
        offset: 时间偏移（秒）——分段转写时把每块的相对时间加回来。
        limit: 超过该源时间（秒）的词丢弃；``0`` 表示不限制。

    返回:
        word dict 列表（按出现顺序，时间已加 ``offset``）。
    """
    words: List[Dict[str, Any]] = []
    for segment in segments:
        seg_id = int(getattr(segment, "id", 0) or 0)
        for word in getattr(segment, "words", None) or []:
            start = float(getattr(word, "start", 0.0) or 0.0) + offset
            end = float(getattr(word, "end", 0.0) or 0.0) + offset
            if start < 0.0:
                start = 0.0
            if not end > start:
                end = start + _MIN_WORD_DURATION
            if limit > 0.0 and start >= limit:
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
    return words


def _get_batched_pipeline(model: Any) -> Any:
    """惰性构造 faster-whisper 的批处理流水线；不可用时返回 ``None``。

    批处理把 30 秒窗口成批送进 GPU，实测 180 秒素材从 11.0s 降到 3.0s（3.7×），
    2 小时素材因此从 ~10 分钟降到 ~2 分钟。它依赖 VAD（onnxruntime + silero），
    环境缺依赖时静默退回顺序解码，不影响可用性。

    ``WORDCUT_WHISPER_BATCHED=0`` 可强制关闭（做对照实验用）。
    """
    if (os.environ.get("WORDCUT_WHISPER_BATCHED") or "1").strip() == "0":
        return None
    if _whisper_runtime.get("device") != "cuda":
        return None
    global _batched_pipeline, _batched_pipeline_model
    if _batched_pipeline is not None and _batched_pipeline_model is model:
        return _batched_pipeline
    try:
        from faster_whisper import BatchedInferencePipeline

        _batched_pipeline = BatchedInferencePipeline(model=model)
        _batched_pipeline_model = model
        return _batched_pipeline
    except Exception as exc:  # noqa: BLE001 - 缺 onnxruntime 等情况都退回顺序模式
        _whisper_runtime["batch_note"] = f"批处理不可用，退回顺序模式：{exc}"
        _batched_pipeline = None
        _batched_pipeline_model = None
        return None


def _run_transcription(
    project: Dict[str, Any],
    model: Any,
    on_progress: Optional[Any] = None,
    on_partial: Optional[Any] = None,
    resume_from: float = 0.0,
    resume_words: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[List[Dict[str, Any]], float]:
    """跑一次词级转写，返回 ``(words, audio_duration)``。

    三个提升长素材体验的机制：

    * **批处理**：GPU 上用 ``BatchedInferencePipeline``（3.7× 实测）；
    * **分段增量**：超过 ``_TRANSCRIBE_CHUNK_SECONDS`` 的音频按块转写，每块完成
      就通过 ``on_partial`` 把累计词表落库，前端能"先看到一部分"，不必等 2 小时
      全部跑完。分段边界会切断个别词，属于已知取舍（见 docs）；
    * **断点续转**：``resume_from`` > 0 时跳过前面已完成的块，把 ``resume_words``
      当作已有结果继续往后接——服务重启后重新点"转写"不必从头再跑。

    参数:
        project: 项目字典（需要 ``audio_path``）。
        model: 已加载的 faster-whisper 模型。
        on_progress: 可选回调 ``(fraction: float, word_count: int) -> None``。
        on_partial: 可选回调 ``(words_so_far: list) -> None``，每块完成后调用。
        resume_from: 续转起点（秒）；0 表示从头转。
        resume_words: 续转时沿用的已有词表。

    返回:
        ``(words, audio_duration)``；``words`` 已按时间排序。

    异常:
        HTTPException: 源音频缺失（500）或转写结果为空（422）。
    """
    audio_path = Path(project["audio_path"])
    if not audio_path.is_file():
        raise HTTPException(
            status_code=500, detail=f"源音频不存在：{audio_path}（请重新上传）"
        )

    try:
        info = sf.info(str(audio_path))
        audio_duration = float(info.frames) / float(info.samplerate or SAMPLE_RATE)
    except (RuntimeError, OSError):
        audio_duration = 0.0

    batched = _get_batched_pipeline(model)
    batch_size = int(os.environ.get("WORDCUT_WHISPER_BATCH_SIZE") or 8)

    def transcribe_once(
        source: Any, length_limit: float = 0.0, base_offset: float = 0.0
    ) -> List[Dict[str, Any]]:
        """转写一段（路径或 numpy 数组），返回 word 列表。

        ``base_offset`` 是这一段在整段音频里的起点（秒），用于把块内相对时间
        换算成全局进度；拢共只有整段转写（短素材）时传 0。
        """
        if batched is not None:
            segments, _info = batched.transcribe(
                source,
                language="zh",
                word_timestamps=True,
                batch_size=batch_size,
                vad_filter=True,
            )
        else:
            segments, _info = model.transcribe(
                source,
                language="zh",
                word_timestamps=True,
                vad_filter=False,
                beam_size=int(os.environ.get("WORDCUT_WHISPER_BEAM") or 5),
            )
        result: List[Dict[str, Any]] = []
        for segment in segments:
            result.extend(_extract_words([segment]))
            if on_progress is not None and audio_duration > 0.0:
                try:
                    done = base_offset + float(getattr(segment, "end", 0.0) or 0.0)
                    on_progress(min(1.0, done / audio_duration), len(result))
                except Exception:  # noqa: BLE001
                    pass
        return result
    # ---- 短素材：一次转完 ----
    if audio_duration <= _TRANSCRIBE_CHUNK_SECONDS * 1.5:
        words = transcribe_once(str(audio_path), audio_duration)
    else:
        # ---- 长素材：按块转写，块内用相对时间，写回时加偏移 ----
        # 断点续转：沿用已完成部分的词表，从 resume_from 对应的块开始。
        words: List[Dict[str, Any]] = list(resume_words or [])  # type: ignore[no-redef]
        rate = int(info.samplerate or SAMPLE_RATE)
        chunk = int(_TRANSCRIBE_CHUNK_SECONDS * rate)
        start_index = int(max(0.0, resume_from) // _TRANSCRIBE_CHUNK_SECONDS)
        if start_index > 0:
            # 往后多退一块：续转起点落在"已转完的最后一块"之内，
            # 这样不会漏掉断点附近的内容（多转的部分会按时间被后续去重）
            start_index = max(0, start_index)
            skip = start_index * chunk
        else:
            skip = 0

        with sf.SoundFile(str(audio_path)) as handle:
            if skip > 0:
                handle.seek(min(skip, len(handle)))
            index = start_index
            while True:
                block = handle.read(chunk, dtype="float32", always_2d=False)
                if block is None or len(block) == 0:
                    break
                offset = index * _TRANSCRIBE_CHUNK_SECONDS
                part = transcribe_once(np.asarray(block), audio_duration, offset)
                for item in part:
                    item["start"] += offset
                    item["end"] += offset
                # 续转起点落在块中间：丢掉已转写过的部分，避免重复词
                if index == start_index and resume_from > 0.0:
                    part = [w for w in part if w["start"] >= resume_from]
                words.extend(part)
                index += 1
                if on_progress is not None and audio_duration > 0.0:
                    try:
                        done = min(1.0, (offset + _TRANSCRIBE_CHUNK_SECONDS) / audio_duration)
                        on_progress(done, len(words))
                    except Exception:  # noqa: BLE001
                        pass
                if on_partial is not None:
                    try:
                        on_partial(sorted(words, key=lambda w: (w["start"], w["end"])))
                    except Exception:  # noqa: BLE001
                        pass

    words.sort(key=lambda item: (item["start"], item["end"]))
    if not words:
        raise HTTPException(
            status_code=422,
            detail="转写结果为空：音频中未识别到任何词，请检查音频内容与音量",
        )
    return words, audio_duration


def _apply_transcription(
    pid: str, project: Dict[str, Any], words: List[Dict[str, Any]]
) -> None:
    """把转写结果写回项目（不变量 4：id 由词序决定，旧删除引用只保留仍存在的）。"""
    known_ids = {
        f"{pipeline.TOKEN_ID_PREFIX}{index:05d}" for index in range(len(words))
    }
    project["words"] = words
    project["deleted_ids"] &= known_ids
    _store.update_words(pid, words)
    _store.update_deleted_ids(pid, project["deleted_ids"])


# ----------------------------------------------------------------------
# 接口：异步转写（长音频必需：2 小时素材在 CPU 上要几十分钟）
# ----------------------------------------------------------------------
class TranscribeJobResponse(BaseModel):
    """异步转写任务的状态。"""

    job_id: str = Field(..., description="任务 id")
    project_id: str = Field(..., description="项目 id")
    status: str = Field(..., description="running / done / error")
    progress: float = Field(0.0, description="进度 0–1（按已转写时长估算）")
    words: int = Field(0, description="已产出的词数")
    elapsed_seconds: float = Field(0.0, description="已运行秒数")
    duration: float = Field(0.0, description="音频总时长（秒）")
    device: str = Field("", description="实际使用的设备（cuda / cpu）")
    compute_type: str = Field("", description="计算精度")
    partial: bool = Field(
        False, description="是否已有分段增量结果落库（前端可提前刷新 Token）"
    )
    resumed_from: float = Field(
        0.0, description="断点续转的起点（秒）；0 表示从头转"
    )
    error: str = Field("", description="失败原因（status=error 时有值）")


#: 进行中的转写任务：job_id -> 状态字典。
_transcribe_jobs: Dict[str, Dict[str, Any]] = {}
#: 项目最近一次转写任务：pid -> job_id（供前端"恢复轮询"）。
_transcribe_latest: Dict[str, str] = {}
_TRANSCRIBE_JOBS_LOCK: threading.Lock = threading.Lock()

#: 转写执行锁：faster-whisper 的模型实例不保证可并发推理，
#: 两个项目同时转写会互相拖慢甚至显存/内存翻倍，这里串行化。
_TRANSCRIBE_EXEC_LOCK: threading.Lock = threading.Lock()


def _job_snapshot(job: Dict[str, Any]) -> Dict[str, Any]:
    """取任务状态的浅拷贝（加锁由调用方负责）。"""
    return {
        "job_id": job["job_id"],
        "project_id": job["project_id"],
        "status": job["status"],
        "progress": round(float(job["progress"]), 4),
        "words": int(job["words"]),
        "elapsed_seconds": round(float(job["elapsed_seconds"]), 2),
        "duration": float(job["duration"]),
        "device": str(job["device"]),
        "compute_type": str(job["compute_type"]),
        "partial": bool(job.get("partial")),
        "resumed_from": round(float(job.get("resumed_from") or 0.0), 2),
        "error": str(job["error"]),
    }


def _run_transcribe_job(job_id: str, pid: str) -> None:
    """后台线程主体：跑转写并持续更新任务状态。"""
    started = time.perf_counter()

    def on_progress(fraction: float, count: int) -> None:
        with _TRANSCRIBE_JOBS_LOCK:
            job = _transcribe_jobs.get(job_id)
            if job is None:
                return
            job["progress"] = max(float(job["progress"]), fraction)
            # 分段转写时 count 是"本块累计"，必须取 max：
            # 否则每块开始时计数会回跳（829 → 1913 → 896 …），界面看着像出错。
            job["words"] = max(int(job["words"]), int(count))
            job["elapsed_seconds"] = time.perf_counter() - started

    try:
        project = _get_project(pid)
        model = _get_whisper_model()
        with _TRANSCRIBE_JOBS_LOCK:
            job = _transcribe_jobs.get(job_id)
            if job is not None:
                job["device"] = str(_whisper_runtime.get("device") or "")
                job["compute_type"] = str(_whisper_runtime.get("compute_type") or "")
        def on_partial(words_so_far: List[Dict[str, Any]]) -> None:
            """分段增量：只落词表，不动删除状态。

            注意不能调用 :func:`_apply_transcription`——它会把 ``deleted_ids``
            截断到当前已知 id，分段中途调用会把用户后面的删除点误删。
            """
            try:
                _store.update_words(pid, words_so_far)
            except Exception:  # noqa: BLE001 - 增量落库失败不该中断转写
                pass
            with _TRANSCRIBE_JOBS_LOCK:
                job = _transcribe_jobs.get(job_id)
                if job is not None:
                    job["words"] = len(words_so_far)
                    job["partial"] = True

        with _TRANSCRIBE_EXEC_LOCK:
            # 断点续转：项目里已有部分词表（上次跑到一半服务被重启 / 用户关了窗口）
            # 就从中断处接着转，已完成的删除标记也不会丢。
            existing = list(_project_words(project) or [])
            resume_from, resume_words = _resume_offset(existing, _duration_of(project))
            if resume_from > 0.0:
                with _TRANSCRIBE_JOBS_LOCK:
                    job = _transcribe_jobs.get(job_id)
                    if job is not None:
                        job["resumed_from"] = resume_from
                        job["words"] = len(resume_words)
            words, duration = _run_transcription(
                project,
                model,
                on_progress,
                on_partial,
                resume_from=resume_from,
                resume_words=resume_words,
            )
        _apply_transcription(pid, project, words)
        with _TRANSCRIBE_JOBS_LOCK:
            job = _transcribe_jobs.get(job_id)
            if job is not None:
                job.update(
                    status="done",
                    progress=1.0,
                    words=len(words),
                    duration=duration,
                    elapsed_seconds=time.perf_counter() - started,
                )
    except HTTPException as exc:
        with _TRANSCRIBE_JOBS_LOCK:
            job = _transcribe_jobs.get(job_id)
            if job is not None:
                job.update(
                    status="error",
                    error=str(exc.detail),
                    elapsed_seconds=time.perf_counter() - started,
                )
    except Exception as exc:  # noqa: BLE001 - 后台线程不能把异常抛出去
        with _TRANSCRIBE_JOBS_LOCK:
            job = _transcribe_jobs.get(job_id)
            if job is not None:
                job.update(
                    status="error",
                    error=f"转写失败：{exc}",
                    elapsed_seconds=time.perf_counter() - started,
                )


@app.post(
    "/api/projects/{pid}/transcribe/start",
    response_model=TranscribeJobResponse,
    summary="启动异步转写（长音频）",
)
def start_transcription(pid: str) -> TranscribeJobResponse:
    """启动后台转写并立刻返回任务 id，前端轮询状态接口取进度。

    为什么需要异步：2 小时素材在 CPU 上要几十分钟，同步 HTTP 请求会一直挂着，
    浏览器/代理稍一超时就前功尽弃；异步任务把结果写进数据库，断线也能恢复。

    参数:
        pid: 项目 id。

    返回:
        ``{job_id, project_id, status, progress, ...}``。

    异常:
        404: 项目不存在；409: 该项目已有转写任务在跑。
    """
    project = _get_project(pid)

    with _TRANSCRIBE_JOBS_LOCK:
        latest = _transcribe_latest.get(pid)
        if latest:
            running = _transcribe_jobs.get(latest)
            if running is not None and running["status"] == "running":
                raise HTTPException(
                    status_code=409,
                    detail=f"项目 {pid} 已有转写任务在进行（job {latest}）",
                )
        job_id = uuid.uuid4().hex[:12]
        job = {
            "job_id": job_id,
            "project_id": pid,
            "status": "running",
            "progress": 0.0,
            "words": 0,
            "elapsed_seconds": 0.0,
            "duration": float(_duration_of(project)),
            "device": str(_whisper_runtime.get("device") or ""),
            "compute_type": str(_whisper_runtime.get("compute_type") or ""),
            "partial": False,
            "error": "",
        }
        _transcribe_jobs[job_id] = job
        _transcribe_latest[pid] = job_id
        snapshot = _job_snapshot(job)

    thread = threading.Thread(
        target=_run_transcribe_job, args=(job_id, pid), daemon=True
    )
    thread.start()
    return TranscribeJobResponse(**snapshot)


@app.get(
    "/api/projects/{pid}/transcribe/status",
    response_model=TranscribeJobResponse,
    summary="查询异步转写进度",
)
def get_transcription_status(pid: str) -> TranscribeJobResponse:
    """返回该项目最近一次转写任务的状态；没有任何任务时 404。

    参数:
        pid: 项目 id。

    返回:
        ``{job_id, status, progress, words, elapsed_seconds, ...}``。

    异常:
        404: 项目不存在，或该项目从未启动过转写任务。
    """
    _get_project(pid)
    with _TRANSCRIBE_JOBS_LOCK:
        job_id = _transcribe_latest.get(pid)
        job = _transcribe_jobs.get(job_id) if job_id else None
        if job is None:
            raise HTTPException(
                status_code=404, detail=f"项目 {pid} 没有转写任务记录"
            )
        return TranscribeJobResponse(**_job_snapshot(job))


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

    project["deleted_ids"].update(ids)
    current = set(project["deleted_ids"])
    _store.update_deleted_ids(pid, current)

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

    project["deleted_ids"].difference_update(ids)
    current = set(project["deleted_ids"])
    _store.update_deleted_ids(pid, current)

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
    """同步导出（保留给脚本/测试；前端用异步任务版以显示进度）。"""
    return _execute_export(pid, payload or ExportRequest())


def _execute_export(
    pid: str,
    options: ExportRequest,
    on_stage: Optional[Any] = None,
) -> ExportResponse:
    """真正执行导出：音频渲染/编码 → （可选）视频裁剪 → 合流。

    参数:
        on_stage: 可选回调 ``(stage: str, fraction: float) -> None``，
            ``stage`` ∈ ``{"audio", "video", "mux"}``，供异步任务上报进度。
    """

    def report(stage: str, fraction: float) -> None:
        if on_stage is None:
            return
        try:
            on_stage(stage, max(0.0, min(1.0, float(fraction))))
        except Exception:  # noqa: BLE001 - 进度回调绝不影响导出
            pass

    audio_format = (options.audio_format or "wav").lower().lstrip(".")
    if audio_format not in _EXPORT_AUDIO_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"不支持的音频格式 {audio_format!r}，可选："
                f"{'/'.join(_EXPORT_AUDIO_FORMATS)}"
            ),
        )

    report("audio", 0.02)
    project = _get_project(pid)
    words = _require_words(project)
    deleted_ids = _snapshot_deleted_ids(project)
    samples = _load_samples(project)
    sample_rate = int(project["sample_rate"])
    output_dir = OUTPUT_DIR / pid

    # 后处理参数：请求体显式覆盖优先，否则用项目里保存的参数
    effective_params = (
        options.audio_params
        if options.audio_params is not None
        else (project.get("audio_params") or {})
    )

    try:
        result = pipeline.run_pipeline(
            words=words,
            samples=samples,
            sample_rate=sample_rate,
            deleted_ids=deleted_ids,
            output_dir=output_dir,
            basename=f"{pid}_cut",
            audio_format=audio_format,
            audio_params=effective_params,
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
    report("audio", 0.4)

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
            on_stage=on_stage,
        )
        paths["video_path"] = video_info.pop("path")
    report("done", 1.0)

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


class ExportJobResponse(BaseModel):
    """异步导出任务的状态。"""

    job_id: str = Field(..., description="任务 id")
    project_id: str = Field(..., description="项目 id")
    status: str = Field(..., description="running / done / error")
    stage: str = Field("audio", description="当前阶段：audio / video / mux / done")
    progress: float = Field(0.0, description="进度 0–1")
    elapsed_seconds: float = Field(0.0, description="已运行秒数")
    result: Optional[ExportResponse] = Field(None, description="完成后的导出结果")
    error: str = Field("", description="失败原因")


#: 异步导出任务：job_id -> 状态；以及每个项目最近一次任务。
_export_jobs: Dict[str, Dict[str, Any]] = {}
_export_latest: Dict[str, str] = {}
_EXPORT_JOBS_LOCK: threading.Lock = threading.Lock()

#: 导出串行化：同时跑两个导出会抢 CPU/GPU，且会互相覆盖同目录产物。
_EXPORT_EXEC_LOCK: threading.Lock = threading.Lock()


def _export_job_snapshot(job: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "job_id": job["job_id"],
        "project_id": job["project_id"],
        "status": job["status"],
        "stage": job["stage"],
        "progress": round(float(job["progress"]), 4),
        "elapsed_seconds": round(float(job["elapsed_seconds"]), 2),
        "result": job.get("result"),
        "error": str(job["error"]),
    }


def _run_export_job(job_id: str, pid: str, options: ExportRequest) -> None:
    """后台线程：执行导出并持续更新阶段/进度。"""
    started = time.perf_counter()

    def on_stage(stage: str, fraction: float) -> None:
        with _EXPORT_JOBS_LOCK:
            job = _export_jobs.get(job_id)
            if job is None:
                return
            # 阶段只能前进：audio → video → mux → done
            order = {"audio": 0, "video": 1, "mux": 2, "done": 3}
            if order.get(stage, 0) >= order.get(job["stage"], 0):
                job["stage"] = stage
            job["progress"] = max(float(job["progress"]), float(fraction))
            job["elapsed_seconds"] = time.perf_counter() - started

    try:
        with _EXPORT_EXEC_LOCK:
            result = _execute_export(pid, options, on_stage)
        with _EXPORT_JOBS_LOCK:
            job = _export_jobs.get(job_id)
            if job is not None:
                job.update(
                    status="done",
                    stage="done",
                    progress=1.0,
                    result=result,
                    elapsed_seconds=time.perf_counter() - started,
                )
    except HTTPException as exc:
        with _EXPORT_JOBS_LOCK:
            job = _export_jobs.get(job_id)
            if job is not None:
                job.update(
                    status="error",
                    error=str(exc.detail),
                    elapsed_seconds=time.perf_counter() - started,
                )
    except Exception as exc:  # noqa: BLE001 - 后台线程不能抛
        with _EXPORT_JOBS_LOCK:
            job = _export_jobs.get(job_id)
            if job is not None:
                job.update(
                    status="error",
                    error=f"导出失败：{exc}",
                    elapsed_seconds=time.perf_counter() - started,
                )


@app.post(
    "/api/projects/{pid}/export/start",
    response_model=ExportJobResponse,
    summary="启动异步导出（长视频必需，返回任务 id 供轮询）",
)
def start_export(pid: str, payload: Optional[ExportRequest] = None) -> ExportJobResponse:
    """启动后台导出并立刻返回任务 id。

    为什么需要异步：2 小时视频导出要重编码上千个片段，实测 2–4 分钟；同步请求
    期间前端完全没有反馈，看起来像卡死。异步任务把阶段（音频/视频/合流）与
    进度暴露出来，断开页面也能在状态接口里查到结果。

    异常:
        404: 项目不存在；409: 该项目已有导出任务在跑。
    """
    _get_project(pid)
    options = payload or ExportRequest()

    with _EXPORT_JOBS_LOCK:
        latest = _export_latest.get(pid)
        if latest:
            running = _export_jobs.get(latest)
            if running is not None and running["status"] == "running":
                raise HTTPException(
                    status_code=409,
                    detail=f"项目 {pid} 已有导出任务在进行（job {latest}）",
                )
        job_id = uuid.uuid4().hex[:12]
        job = {
            "job_id": job_id,
            "project_id": pid,
            "status": "running",
            "stage": "audio",
            "progress": 0.0,
            "elapsed_seconds": 0.0,
            "result": None,
            "error": "",
        }
        _export_jobs[job_id] = job
        _export_latest[pid] = job_id
        snapshot = _export_job_snapshot(job)

    threading.Thread(
        target=_run_export_job, args=(job_id, pid, options), daemon=True
    ).start()
    return ExportJobResponse(**snapshot)


@app.get(
    "/api/projects/{pid}/export/status",
    response_model=ExportJobResponse,
    summary="查询异步导出进度",
)
def get_export_status(pid: str) -> ExportJobResponse:
    """返回该项目最近一次导出任务的状态；没有任务时 404。"""
    _get_project(pid)
    with _EXPORT_JOBS_LOCK:
        job_id = _export_latest.get(pid)
        job = _export_jobs.get(job_id) if job_id else None
        if job is None:
            raise HTTPException(status_code=404, detail=f"项目 {pid} 没有导出任务记录")
        return ExportJobResponse(**_export_job_snapshot(job))


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
        "audio_params": loaded.get("audio_params") or {},
        "created_at": loaded["created_at"] or _now_iso(),
    }
    _store.put(project)

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
        "audio_params": loaded.get("audio_params") or {},
        "created_at": loaded["created_at"] or _now_iso(),
    }
    _store.put(project)

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

    # 关键：**优先用上传时已经转好的 16kHz WAV**（audio_path）。
    # 以前这里优先拿 original_media_path / source_path（原始视频），每次打开
    # 项目都要把 2 小时视频重新转一遍——单次 ffmpeg 可能超过 600 秒超时，
    # 前端就一直卡在"加载中"，同一时刻的删除请求也会因为线程池被占满而失败。
    # audio_path 本身就是目标格式（16k 单声道 PCM16），直接回它最快。
    audio_file = project.get("audio_path")
    audio_path_obj = Path(str(audio_file)) if audio_file else None
    if audio_path_obj and audio_path_obj.is_file() and audio_path_obj.stat().st_size > 44:
        source = audio_path_obj
        need_transcode = False
    else:
        candidates = [
            project.get("original_media_path"),
            project.get("source_path"),
        ]
        source = None
        for item in candidates:
            if not item:
                continue
            candidate = Path(str(item))
            if candidate.is_file():
                source = candidate
                break
        need_transcode = True

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

    # 长素材用"轻量预览"：2 小时 WAV 有 200MB+，浏览器要先下载再整体解码成
    # float32（内存翻倍），很容易把标签页拖死（表现为 decodeAudioData 失败）。
    # 压成 8kHz/24kbps 单声道 MP3 后体积与解码内存都降到 1/4 左右。
    duration = 0.0
    try:
        duration = _duration_of(project)
    except HTTPException:
        duration = 0.0

    if duration >= _COMPACT_PREVIEW_MIN_SECONDS:
        compact = cache_dir / "preview_compact.mp3"
        try:
            fresh = (
                compact.is_file()
                and compact.stat().st_size > 1024
                and compact.stat().st_mtime >= source.stat().st_mtime
            )
        except OSError:
            fresh = False
        if not fresh:
            # 先写临时文件再改名：避免并发/中断留下半截 MP3 被当成有效缓存，
            # 那种文件正好会让浏览器报 "Unable to decode audio data"。
            temp_target = cache_dir / "preview_compact.tmp.mp3"
            temp_target.unlink(missing_ok=True)
            try:
                media.convert_to_compact_preview(source, temp_target)
                temp_target.replace(compact)
            except media.MediaToolError as exc:
                temp_target.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=500, detail=f"生成波形预览失败：{exc}"
                ) from exc
        return FileResponse(
            str(compact),
            media_type="audio/mpeg",
            filename="preview.mp3",
            headers={"Cache-Control": "no-store"},
        )

    # 短素材：直接回 16k WAV（若原本就是 WAV 则零转码）
    if not need_transcode and source.suffix.lower() == ".wav":
        return FileResponse(
            str(source), media_type="audio/wav", filename="source.wav"
        )

    cache = cache_dir / "source_preview.wav"
    try:
        media.convert_to_wav16k(source, cache, SAMPLE_RATE)
    except media.MediaToolError as exc:
        raise HTTPException(
            status_code=500, detail=f"读取源音频失败：{exc}"
        ) from exc

    return FileResponse(str(cache), media_type="audio/wav", filename="source.wav")


@app.get(
    "/api/projects/{pid}/waveform-peaks",
    summary="取波形峰值（预计算，前端无需解码整段音频）",
)
def get_waveform_peaks(
    pid: str,
    buckets: int = Query(4000, description="峰值点数量（越大越细，建议 1000–8000）"),
) -> Dict[str, Any]:
    """返回归一化波形峰值，供前端直接绘制，避免 ``decodeAudioData`` 整段解码。

    为什么需要这个接口：2 小时音频即使压成 8kHz MP3，浏览器解码到 AudioContext
    采样率（通常 48kHz）后仍要 ~1.3GB 内存，Chrome 会直接抛
    ``Unable to decode audio data``。把"画波形"需要的包络在服务端算好（每个桶
    一个振幅），前端只传几十 KB，任意长度都能画，也顺带让缩放/滚动变轻。

    参数:
        pid: 项目 id。
        buckets: 峰值点数量，夹在 ``[200, 20000]``。

    返回:
        ``{project_id, duration, sample_rate, buckets, peaks: [[float, ...]]}``
        （``peaks`` 是"每声道一组"的二维结构，与 WaveSurfer 的格式一致）。

    异常:
        404: 项目不存在；500: 读取音频失败。
    """
    buckets = max(200, min(int(buckets), 20000))
    project = _get_project(pid)
    audio_file = Path(str(project.get("audio_path") or ""))
    if not audio_file.is_file():
        raise HTTPException(
            status_code=404, detail=f"项目音频不存在：{audio_file}（请重新上传）"
        )

    cache_dir = OUTPUT_DIR / pid
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache = cache_dir / f"peaks_{buckets}.json"

    try:
        stat = audio_file.stat()
        fresh = (
            cache.is_file()
            and cache.stat().st_size > 2
            and cache.stat().st_mtime >= stat.st_mtime
        )
    except OSError:
        fresh = False

    if not fresh:
        try:
            peaks, duration, rate = _compute_waveform_peaks(audio_file, buckets)
        except (RuntimeError, OSError) as exc:
            raise HTTPException(
                status_code=500, detail=f"计算波形峰值失败：{exc}"
            ) from exc
        payload = {
            "duration": duration,
            "sample_rate": rate,
            "buckets": len(peaks),
            "peaks": [peaks],
        }
        temp = cache.with_suffix(".tmp.json")
        temp.write_text(json.dumps(payload), encoding="utf-8")
        temp.replace(cache)
    else:
        payload = json.loads(cache.read_text(encoding="utf-8"))

    return {
        "project_id": pid,
        "duration": payload.get("duration", 0.0),
        "sample_rate": payload.get("sample_rate", SAMPLE_RATE),
        "buckets": payload.get("buckets", 0),
        "peaks": payload.get("peaks", [[]]),
    }


def _compute_waveform_peaks(
    audio_file: Path, buckets: int
) -> tuple[List[float], float, int]:
    """分块读取音频并计算归一化峰值包络，返回 ``(peaks, duration, rate)``。

    分块读（而不是 ``sf.read`` 整段）是为了让 2 小时素材的内存占用保持在
    几 MB：每次只读一个桶对应的帧数。
    """
    peaks: List[float] = []
    with sf.SoundFile(str(audio_file)) as handle:
        rate = int(handle.samplerate) or SAMPLE_RATE
        total = int(handle.frames)
        if total <= 0:
            return [], 0.0, rate
        span = max(1, total // buckets)
        while True:
            block = handle.read(span, dtype="float32", always_2d=False)
            if block is None or len(block) == 0:
                break
            array = np.asarray(block)
            peaks.append(float(np.max(np.abs(array))))
            if len(peaks) >= buckets:
                break
        duration = float(total) / float(rate)

    # 归一化到 0–1：只影响绘制比例，不改变时间轴
    peak_max = max(peaks) if peaks else 0.0
    if peak_max > 0:
        peaks = [min(1.0, value / peak_max) for value in peaks]
    return peaks, duration, rate


@app.get(
    "/api/projects/{pid}/export-file/{filename}",
    summary="取回导出产物（供浏览器保存到本地）",
)
def get_export_file(pid: str, filename: str) -> FileResponse:
    """按文件名返回 outputs/{pid}/ 下的导出产物。

    仅允许该目录内的文件（拒绝路径穿越），文件名由前端从导出结果的绝对路径取末段。

    参数:
        pid: 项目 id。
        filename: 文件名（如 xxx_cut.mp4）。

    返回:
        文件流；MIME 按扩展名推断。

    异常:
        400: 文件名非法；404: 文件不存在。
    """
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail=f"非法文件名：{filename}")

    target = (OUTPUT_DIR / pid / filename).resolve()
    base = (OUTPUT_DIR / pid).resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"非法路径：{filename}") from exc

    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"导出文件不存在：{filename}")

    return FileResponse(str(target), filename=filename)


@app.post(
    "/api/projects/{pid}/silences",
    response_model=SilenceSuggestResponse,
    summary="检测静音段并给出建议删除区间",
)
def detect_project_silences(
    pid: str, payload: Optional[SilenceSuggestRequest] = None
) -> SilenceSuggestResponse:
    """用短时能量检测音频中的静音段，转成建议删除的 DeleteSpan。

    已经与 token 发音区间做过重叠保护：完全落在某个词内部的静音不会被建议删除。

    参数:
        pid: 项目 id。
        payload: ``{min_silence, threshold_db, padding}``；省略时用默认值。

    返回:
        ``{silences, deletes, total_silence, total_deleted}``，时间单位秒。

    异常:
        400: 参数非法；404: 项目不存在。
    """
    options = payload or SilenceSuggestRequest()
    project = _get_project(pid)
    words = _require_words(project)
    samples = _load_samples(project)
    rate = int(project["sample_rate"])

    try:
        tokens = pipeline.load_tokens(words, set())
        silences = silence.detect_silences(
            samples,
            rate,
            min_silence=float(options.min_silence),
            threshold_db=float(options.threshold_db),
            padding=float(options.padding),
        )
        deletes = silence.suggest_pause_deletes(
            samples,
            rate,
            tokens,
            min_silence=float(options.min_silence),
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"静音检测参数非法：{exc}") from exc

    return SilenceSuggestResponse(
        project_id=pid,
        silences=silences,
        deletes=[DeleteSpanOut(**span.to_dict()) for span in deletes],
        total_silence=round(sum(item["duration"] for item in silences), 3),
        total_deleted=round(_total_deleted(deletes), 3),
    )


# ----------------------------------------------------------------------
# 接口：音频后处理参数（增益 / 标准化 / 淡入淡出）
# ----------------------------------------------------------------------
class AudioParamsResponse(BaseModel):
    """音频后处理参数的读写响应。"""

    project_id: str = Field(..., description="项目 id")
    audio_params: Dict[str, Any] = Field(
        ..., description="当前生效的后处理参数（默认值已补齐）"
    )
    limits: Dict[str, Any] = Field(
        ..., description="取值上限，供前端生成控件（增益范围、淡入淡出上限）"
    )


@app.get(
    "/api/projects/{pid}/audio-params",
    response_model=AudioParamsResponse,
    summary="读取音频后处理参数",
)
def get_audio_params(pid: str) -> AudioParamsResponse:
    """读取项目当前的音频后处理参数；未设置过时返回全默认值。

    参数:
        pid: 项目 id。

    返回:
        ``{project_id, audio_params, limits}``。

    异常:
        404: 项目不存在；500: 库里存的参数已损坏。
    """
    project = _get_project(pid)
    raw = project.get("audio_params") or {}
    try:
        params = audio_fx.AudioParams.from_mapping(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=500, detail=f"项目里保存的音频参数非法：{exc}"
        ) from exc
    return AudioParamsResponse(
        project_id=pid, audio_params=params.to_dict(), limits=_audio_param_limits()
    )


@app.post(
    "/api/projects/{pid}/audio-params",
    response_model=AudioParamsResponse,
    summary="保存音频后处理参数（增益 / 标准化 / 淡入淡出）",
)
def set_audio_params(pid: str, payload: Dict[str, Any]) -> AudioParamsResponse:
    """校验并持久化音频后处理参数，随后导出即按新参数生效。

    只影响**导出阶段**的渲染副本：不改变源文件（不变量 1），也不改变 EDL
    与字幕时间轴（后处理只改幅度，不改长度）。

    参数:
        pid: 项目 id。
        payload: ``{gain_db, normalize, fade_in, fade_out, target_dbfs}``，
            缺省字段沿用默认值。

    返回:
        ``{project_id, audio_params, limits}``。

    异常:
        400: 参数非法（越界或类型错误）；404: 项目不存在。
    """
    try:
        params = audio_fx.AudioParams.from_mapping(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"音频参数非法：{exc}") from exc

    project = _get_project(pid)
    project["audio_params"] = params.to_dict()
    _store.put(project)

    return AudioParamsResponse(
        project_id=pid, audio_params=params.to_dict(), limits=_audio_param_limits()
    )


# ----------------------------------------------------------------------
# 接口：音频信息统计
# ----------------------------------------------------------------------
class AudioInfoResponse(BaseModel):
    """音频信息统计响应。"""

    project_id: str = Field(..., description="项目 id")
    source: Dict[str, Any] = Field(..., description="源音频的客观指标（不受参数影响）")
    edited: Optional[Dict[str, Any]] = Field(
        None, description="按当前删除状态 + 当前后处理参数渲染后的成片指标"
    )
    audio_params: Dict[str, Any] = Field(
        default_factory=dict, description="本次统计所用的后处理参数（回显用）"
    )
    token_count: int = Field(0, description="Token 总数")
    deleted_count: int = Field(0, description="被删 Token 数")
    total_deleted: float = Field(0.0, description="删除总时长（秒）")


@app.get(
    "/api/projects/{pid}/audio-info",
    response_model=AudioInfoResponse,
    summary="音频信息统计（源音频与剪辑后）",
)
def get_audio_info(pid: str) -> AudioInfoResponse:
    """统计源音频与"按当前删除状态渲染后"的客观指标。

    用途：界面参数面板展示、论文第 6 章实验数据（时长 / 峰值 / RMS /
    波峰因数 / 静音占比 / 削顶样本数）。

    源音频指标反映素材本身；``edited`` 反映剪辑 + 后处理后的成片，
    因此在调整增益或删减内容后能直接看到响度变化。

    参数:
        pid: 项目 id。

    返回:
        ``{project_id, source, edited, token_count, deleted_count, total_deleted}``。

    异常:
        404: 项目不存在；500: 读取或渲染失败。
    """
    project = _get_project(pid)
    samples = _load_samples(project)
    rate = int(project["sample_rate"])

    try:
        source_stats = audio_fx.measure(samples, rate).to_dict()
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=500, detail=f"统计源音频失败：{exc}"
        ) from exc

    token_count = 0
    deleted_count = 0
    total_deleted = 0.0
    words = project.get("words") or []

    # 未转写时没有删除区间，但仍然要算"源 + 当前参数"的成片指标：
    # 否则用户刚上传、还没转写时点「应用参数」，面板里一个数字都不会变，
    # 看起来就像参数没生效（实际是导出时才生效）。
    deletes: List[DeleteSpan] = []
    if words:
        deleted_ids = _snapshot_deleted_ids(project)
        tokens = _tokens_of(project, deleted_ids)
        deletes = _deletes_of(tokens, deleted_ids)
        token_count = len(tokens)
        deleted_count = len(deleted_ids)
        total_deleted = round(_total_deleted(deletes), 3)

    edited: Optional[Dict[str, Any]] = None
    try:
        rendered = edl_render.render(samples, rate, deletes)
        params = audio_fx.AudioParams.from_mapping(project.get("audio_params") or {})
        try:
            processed = audio_fx.apply(rendered, rate, params)
        except (RuntimeError, OSError):
            # 环境缺 ffmpeg 时降噪会失败：统计不能让整个面板挂掉，
            # 退回"不降噪"的其它后处理，保证峰值/时长等指标仍然可用。
            processed = audio_fx.apply(rendered, rate, params, enable_denoise=False)
        edited = audio_fx.measure(processed, rate).to_dict()
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=500, detail=f"统计剪辑后音频失败：{exc}"
        ) from exc

    return AudioInfoResponse(
        project_id=pid,
        source=source_stats,
        edited=edited,
        token_count=token_count,
        deleted_count=deleted_count,
        total_deleted=total_deleted,
        audio_params=audio_fx.AudioParams.from_mapping(
            project.get("audio_params") or {}
        ).to_dict(),
    )


def _audio_param_limits() -> Dict[str, Any]:
    """后处理参数的取值上限（一次构造，供两个接口共用）。"""
    return {
        "min_gain_db": audio_fx.MIN_GAIN_DB,
        "max_gain_db": audio_fx.MAX_GAIN_DB,
        "max_fade_seconds": audio_fx.MAX_FADE_SECONDS,
        "default_target_dbfs": audio_fx.DEFAULT_TARGET_DBFS,
        "min_denoise_db": audio_fx.DENOISE_MIN_DB,
        "max_denoise_db": audio_fx.DENOISE_MAX_DB,
        "default_denoise_db": audio_fx.DENOISE_DEFAULT_DB,
    }


# ----------------------------------------------------------------------
# 接口：剪辑结果即时试听（删除/恢复后立刻听到效果）
# ----------------------------------------------------------------------
class AuditionRequest(BaseModel):
    """``POST /api/projects/{pid}/audition`` 的请求体。"""

    source_time: float = Field(
        ..., description="源时间轴上的关注点（秒），通常是刚被删/恢复词的起点"
    )
    pad: float = Field(1.2, description="关注点前后各取多少秒（0.1–3）")


#: 试听渲染缓存：key -> (samples, sample_rate)。命中即免去重渲染。
_AUDITION_CACHE: Dict[tuple, Any] = {}
#: 缓存上限（条）；超过按插入顺序淘汰最早的。
_AUDITION_CACHE_LIMIT: int = 6


def _audition_key(project: Dict[str, Any]) -> tuple:
    """缓存键：项目 id + 删除状态 + 后处理参数（任一变化都要重新渲染）。"""
    params = audio_fx.AudioParams.from_mapping(project.get("audio_params") or {})
    return (
        str(project.get("id")),
        tuple(sorted(str(item) for item in (project.get("deleted_ids") or ()))),
        params.gain_db,
        params.normalize,
        params.fade_in,
        params.fade_out,
        params.target_dbfs,
    )


def _rendered_edited_audio(project: Dict[str, Any]) -> np.ndarray:
    """渲染"当前删除状态 + 当前后处理参数"的成片（带缓存）。

    注意：这里**不做降噪**。降噪要起一次 ffmpeg 进程、对整段音频处理，放在
    "每次删除都刷新试听"这条热路径上会明显卡顿；试听只需要判断"剪得对不对"，
    噪声细节不是重点。整段降噪留给导出与统计接口。
    """
    key = _audition_key(project)
    cached = _AUDITION_CACHE.get(key)
    if cached is not None:
        return cached

    samples = _load_samples(project)
    rate = int(project["sample_rate"])
    deleted_ids = _snapshot_deleted_ids(project)
    tokens = _tokens_of(project, deleted_ids)
    deletes = _deletes_of(tokens, deleted_ids)
    rendered = edl_render.render(samples, rate, deletes)
    params = audio_fx.AudioParams.from_mapping(project.get("audio_params") or {})
    processed = audio_fx.apply(rendered, rate, params, enable_denoise=False)

    if len(_AUDITION_CACHE) >= _AUDITION_CACHE_LIMIT:
        _AUDITION_CACHE.pop(next(iter(_AUDITION_CACHE)))
    _AUDITION_CACHE[key] = processed
    return processed


@app.post(
    "/api/projects/{pid}/audition",
    summary="取剪辑后的一小段音频（删除/恢复后即时试听）",
)
def audition_project(pid: str, payload: AuditionRequest) -> FileResponse:
    """按当前编辑状态渲染成片，截取关注点前后一小段返回 WAV，用于即时试听。

    关注点用**源时间轴**的秒数给出（前端持有的是源时间戳），内部经
    :class:`model.TimeMap` 换算到目标时间轴后再切片（不变量 6：时间映射只有
    一个口径，试听与导出必然一致）。

    参数:
        pid: 项目 id。
        payload: ``{source_time, pad}``；``pad`` 夹在 ``[0.1, 3.0]``。

    返回:
        WAV 文件流（16-bit PCM，采样率与项目一致）。

    异常:
        400: 参数非法；404: 项目不存在。
    """
    pad = float(payload.pad)
    if not (0.1 <= pad <= 3.0):
        raise HTTPException(status_code=400, detail=f"pad 必须在 0.1–3.0 秒之间：{pad}")

    project = _get_project(pid)
    try:
        processed = _rendered_edited_audio(project)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400, detail=f"渲染剪辑结果失败：{exc}"
        ) from exc

    rate = int(project["sample_rate"])
    deleted_ids = _snapshot_deleted_ids(project)
    tokens = _tokens_of(project, deleted_ids)
    deletes = _deletes_of(tokens, deleted_ids)
    time_map = TimeMap.from_deletes(deletes)
    target_time = float(time_map.source_to_target(float(payload.source_time)))

    total = int(np.asarray(processed).size)
    center = int(round(target_time * rate))
    half = int(round(pad * rate))
    start = max(0, min(center - half, max(0, total - 1)))
    end = max(start + 1, min(center + half, total))
    window = np.asarray(processed)[start:end]
    if window.size == 0:
        raise HTTPException(status_code=400, detail="试听区间为空")

    # 试听片段若开了降噪，只对这小段跑一次 ffmpeg（约 2 秒音频，几十毫秒）
    params = audio_fx.AudioParams.from_mapping(project.get("audio_params") or {})
    if params.denoise:
        try:
            window = audio_fx.apply(
                window,
                rate,
                audio_fx.AudioParams(denoise=True, denoise_db=params.denoise_db),
            )
        except (RuntimeError, OSError):
            # 降噪失败不该让试听整个不可用：退回未降噪片段
            pass

    handle = tempfile.NamedTemporaryFile(
        suffix=".wav", prefix=f"audition_{pid}_", delete=False, dir=str(UPLOAD_DIR)
    )
    handle.close()
    _write_wav(Path(handle.name), window)
    return FileResponse(handle.name, media_type="audio/wav", filename="audition.wav")


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
