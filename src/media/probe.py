"""媒体探测模块：用 ffprobe 读取容器与流的元信息。

对应 docs/架构预留设计.md 第 2 节的 ``MediaProbe``。

约定：

* 所有时间字段（``duration``）单位为秒（float），见 docs/不变量.md 不变量 2；
* 本模块只读文件，绝不写盘（不变量 1：源文件只读）；
* 子进程统一 ``subprocess.run(..., capture_output=True)``，超时 600 秒。
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .ffmpeg_bin import TOOL_TIMEOUT_SECONDS, MediaToolError, resolve_ffprobe

__all__ = ["MediaStream", "MediaInfo", "MediaToolError", "probe"]

#: 合法的流类型（与 :class:`MediaStream.type` 的取值域一致）。
STREAM_TYPES: tuple = ("audio", "video", "subtitle")

#: 错误信息里保留的 stderr 末尾字符数。
_STDERR_TAIL_CHARS: int = 1500


@dataclass(frozen=True)
class MediaStream:
    """容器中的一路媒体流。

    属性:
        index: 流序号（ffprobe 的 ``index``，从 0 开始）。
        type: 流类型，取值 ``audio`` / ``video`` / ``subtitle``。
        codec: 编码名（如 ``aac`` / ``h264``），缺失时为 ``unknown``。
        sample_rate: 音频采样率（Hz）；非音频流为 ``None``。
        channels: 音频声道数；非音频流为 ``None``。
        width: 视频宽（像素）；非视频流为 ``None``。
        height: 视频高（像素）；非视频流为 ``None``。
        duration: 该流时长（秒）；ffprobe 未给出时为 ``None``。
    """

    index: int
    type: str
    codec: str
    sample_rate: Optional[int] = None
    channels: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    duration: Optional[float] = None

    def __post_init__(self) -> None:
        if not isinstance(self.index, int) or isinstance(self.index, bool):
            raise TypeError(
                f"MediaStream.index 必须是整数，实际为 {type(self.index).__name__}"
            )
        if self.index < 0:
            raise ValueError(f"MediaStream.index 必须 >= 0，实际为 {self.index!r}")
        if self.type not in STREAM_TYPES:
            raise ValueError(
                f"MediaStream.type 必须是 {STREAM_TYPES} 之一，实际为 {self.type!r}"
            )
        if self.duration is not None and self.duration < 0:
            raise ValueError(
                f"MediaStream.duration 必须 >= 0（秒），实际为 {self.duration!r}"
            )

    @property
    def is_audio(self) -> bool:
        """是否音频流。"""
        return self.type == "audio"

    @property
    def is_video(self) -> bool:
        """是否视频流。"""
        return self.type == "video"

    def to_dict(self) -> Dict[str, Any]:
        """转成普通字典（便于日志 / 接口序列化）。"""
        return {
            "index": self.index,
            "type": self.type,
            "codec": self.codec,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "width": self.width,
            "height": self.height,
            "duration": self.duration,
        }


@dataclass(frozen=True)
class MediaInfo:
    """一次 ffprobe 探测的整体结果。

    属性:
        path: 被探测文件的路径（字符串，原样保留调用方传入的形式）。
        duration: 容器总时长（秒）；读不到时为 0.0。
        format_name: 容器格式名（如 ``mov,mp4,m4a,3gp``），读不到时为 ``unknown``。
        streams: 流列表，顺序与容器内顺序一致。
    """

    path: str
    duration: float
    format_name: str
    streams: List[MediaStream] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.duration < 0:
            raise ValueError(f"MediaInfo.duration 必须 >= 0（秒），实际为 {self.duration!r}")

    def audio_streams(self) -> List[MediaStream]:
        """返回所有音频流。"""
        return [stream for stream in self.streams if stream.is_audio]

    def video_streams(self) -> List[MediaStream]:
        """返回所有视频流。"""
        return [stream for stream in self.streams if stream.is_video]

    def to_dict(self) -> Dict[str, Any]:
        """转成普通字典（``streams`` 递归转为字典列表）。"""
        return {
            "path": self.path,
            "duration": self.duration,
            "format_name": self.format_name,
            "streams": [stream.to_dict() for stream in self.streams],
        }


def _decode(raw: Optional[bytes]) -> str:
    """把子进程输出（bytes）解码成文本；``None`` 视作空串。"""
    if not raw:
        return ""
    return raw.decode("utf-8", errors="replace")


def _tail(text: str, limit: int = _STDERR_TAIL_CHARS) -> str:
    """截取文本末尾 ``limit`` 个字符（保留关键报错，避免日志爆炸）。"""
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return "…" + stripped[-limit:]


def _as_int(value: Any) -> Optional[int]:
    """尽力把 JSON 值转成 int；不可转时返回 ``None``（``bool`` 视作非法）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> Optional[float]:
    """尽力把 JSON 值转成有限 float；``NaN`` / ``±inf`` / 非法值返回 ``None``。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return result


def _duration_from_time_base(raw: Dict[str, Any]) -> Optional[float]:
    """用 ``duration_ts * time_base`` 换算时长（秒）；算不出来时返回 ``None``。"""
    duration_ts = _as_float(raw.get("duration_ts"))
    time_base = raw.get("time_base")
    if duration_ts is None or not isinstance(time_base, str) or "/" not in time_base:
        return None
    numerator, _, denominator = time_base.partition("/")
    try:
        num_value = float(numerator)
        den_value = float(denominator)
    except ValueError:
        return None
    if den_value == 0.0:
        return None
    seconds = duration_ts * num_value / den_value
    if seconds != seconds or seconds <= 0.0:
        return None
    return seconds


def _parse_stream(raw: Dict[str, Any]) -> Optional[MediaStream]:
    """把单条 ffprobe stream JSON 转成 :class:`MediaStream`。

    非 audio / video / subtitle 的流（如 ``attachment`` / ``data``）直接跳过，
    返回 ``None``。
    """
    stream_type = str(raw.get("codec_type") or "").strip().lower()
    if stream_type not in STREAM_TYPES:
        return None

    duration = _as_float(raw.get("duration"))
    if duration is None:
        duration = _duration_from_time_base(raw)

    index = _as_int(raw.get("index"))
    return MediaStream(
        index=index if index is not None and index >= 0 else 0,
        type=stream_type,
        codec=str(raw.get("codec_name") or "").strip() or "unknown",
        sample_rate=_as_int(raw.get("sample_rate")),
        channels=_as_int(raw.get("channels")),
        width=_as_int(raw.get("width")),
        height=_as_int(raw.get("height")),
        duration=duration if duration is not None and duration >= 0.0 else None,
    )


def probe(path: Path) -> MediaInfo:
    """用 ffprobe 探测媒体文件，返回容器时长与流信息。

    参数:
        path: 待探测的媒体文件（音频或视频）。只读，不会被修改。

    返回:
        :class:`MediaInfo`，``duration`` 单位为秒（float）。

    异常:
        MediaToolError: 找不到 ffprobe、文件不存在、ffprobe 退出码非 0、
            调用超时，或输出不是合法 JSON。
    """
    ffprobe = resolve_ffprobe()
    source = Path(path)
    if not source.is_file():
        raise MediaToolError(f"待探测的媒体文件不存在：{source}")

    argv: List[str] = [
        ffprobe,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(source),
    ]
    try:
        completed = subprocess.run(
            argv, capture_output=True, timeout=TOOL_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired as exc:
        raise MediaToolError(
            f"ffprobe 探测超时（>{TOOL_TIMEOUT_SECONDS} 秒）：{source}"
        ) from exc
    except OSError as exc:
        raise MediaToolError(f"无法启动 ffprobe（{ffprobe}）：{exc}") from exc

    if completed.returncode != 0:
        raise MediaToolError(
            f"ffprobe 探测失败（退出码 {completed.returncode}）：{source}\n"
            f"{_tail(_decode(completed.stderr))}"
        )

    try:
        payload = json.loads(_decode(completed.stdout) or "{}")
    except json.JSONDecodeError as exc:
        raise MediaToolError(
            f"ffprobe 输出不是合法 JSON：{source}（{exc}）"
        ) from exc
    if not isinstance(payload, dict):
        raise MediaToolError(f"ffprobe 输出结构异常（期望对象）：{source}")

    streams: List[MediaStream] = []
    raw_streams = payload.get("streams")
    if isinstance(raw_streams, list):
        for raw in raw_streams:
            if not isinstance(raw, dict):
                continue
            stream = _parse_stream(raw)
            if stream is not None:
                streams.append(stream)

    raw_format = payload.get("format")
    fmt: Dict[str, Any] = raw_format if isinstance(raw_format, dict) else {}

    duration = _as_float(fmt.get("duration"))
    if duration is None:
        candidates = [
            stream.duration for stream in streams if stream.duration is not None
        ]
        duration = max(candidates) if candidates else 0.0
    if duration < 0.0:
        duration = 0.0

    return MediaInfo(
        path=str(source),
        duration=float(duration),
        format_name=str(fmt.get("format_name") or "").strip() or "unknown",
        streams=streams,
    )
