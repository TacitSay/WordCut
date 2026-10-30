"""媒体处理包：探测 / 转换 / 人声分离 / 视频剪辑的统一入口。

对应 docs/架构预留设计.md 第 2 节。本模块只做导出，不含任何逻辑：

* :func:`probe` / :class:`MediaInfo` / :class:`MediaStream` —— ffprobe 探测（已实现）；
* :func:`convert_to_wav16k` —— 任意音视频 → 16kHz 单声道 PCM16 WAV（已实现）；
* :func:`denoise_array` —— ffmpeg ``afftdn`` 降噪（已实现，纲领 P2 简单版）；
* :func:`separate_vocals` —— 人声分离（插件挂载点，抛 ``NotImplementedError``）；
* :func:`cut_video` / :func:`concat_av` —— 视频剪辑 / 音视频合流（已实现）。

另导出 :class:`MediaToolError`（所有媒体工具类错误的统一类型）与
:func:`resolve_ffmpeg` / :func:`resolve_ffprobe`（工具路径解析）。
"""

from __future__ import annotations

from .converter import convert_to_wav16k
from .denoiser import denoise_array
from .ffmpeg_bin import (
    MediaToolError,
    resolve_ffmpeg,
    resolve_ffprobe,
)
from .probe import MediaInfo, MediaStream, probe
from .separator import separate_vocals
from .video import concat_av, cut_video

__all__ = [
    "probe",
    "MediaInfo",
    "MediaStream",
    "convert_to_wav16k",
    "denoise_array",
    "separate_vocals",
    "cut_video",
    "concat_av",
    "MediaToolError",
    "resolve_ffmpeg",
    "resolve_ffprobe",
]
