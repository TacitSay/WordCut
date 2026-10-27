"""媒体转换模块：任意音视频 → 16kHz 单声道 PCM16 WAV。

对应 docs/架构预留设计.md 第 2 节的 ``MediaConverter``。这一层替换掉
``api.py`` 里基于 soundfile 的解码 + 线性插值重采样：

* ffmpeg 自带解码器，MP3 / AAC / FLAC / OGG / MP4 / MKV 等格式自动识别；
* 视频输入用 ``-vn`` 直接丢弃画面，只取音轨；
* 重采样与声道混缩由 ffmpeg 完成，质量优于线性插值。

约定：

* 时间单位秒（不变量 2）；
* 源文件只读（不变量 1）：只写 ``dst``，且拒绝 ``dst`` 与 ``src`` 指向同一文件；
* 子进程统一 ``subprocess.run(..., capture_output=True)``，超时 600 秒。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List, Optional

from .ffmpeg_bin import TOOL_TIMEOUT_SECONDS, MediaToolError, resolve_ffmpeg

__all__ = ["convert_to_wav16k", "DEFAULT_SAMPLE_RATE"]

#: 全项目统一采样率（Hz，单声道），与 ``api.SAMPLE_RATE`` 口径一致。
DEFAULT_SAMPLE_RATE: int = 16_000

#: 错误信息里保留的 stderr 末尾字符数。
_STDERR_TAIL_CHARS: int = 2000


def _decode(raw: Optional[bytes]) -> str:
    """把子进程输出（bytes）解码成文本；``None`` 视作空串。"""
    if not raw:
        return ""
    return raw.decode("utf-8", errors="replace")


def _tail(text: str, limit: int = _STDERR_TAIL_CHARS) -> str:
    """截取文本末尾 ``limit`` 个字符，作为异常里的报错摘要。"""
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return "…" + stripped[-limit:]


def convert_to_wav16k(
    src: Path, dst: Path, sample_rate: int = DEFAULT_SAMPLE_RATE
) -> None:
    """把任意音视频转成单声道 PCM16 WAV（默认 16kHz）。

    实际执行的命令（``{rate}`` 为 ``sample_rate``）::

        ffmpeg -y -i {src} -vn -ac 1 -ar {rate} -c:a pcm_s16le {dst}

    参数:
        src: 输入文件（音频或视频，任意 ffmpeg 支持的格式）。只读，不会被修改。
        dst: 输出 WAV 路径；父目录不存在时自动创建，同名文件会被覆盖。
        sample_rate: 目标采样率（Hz），默认 16000。

    返回:
        ``None``。成功时 ``dst`` 一定是 16bit 单声道 WAV。

    异常:
        MediaToolError: 找不到 ffmpeg、``src`` 不存在、``dst`` 与 ``src`` 是同一
            文件、ffmpeg 退出码非 0（附 stderr 末尾）或调用超时。
        ValueError: ``sample_rate`` 不是正整数。
    """
    if not isinstance(sample_rate, int) or isinstance(sample_rate, bool):
        raise TypeError(
            f"sample_rate 必须是整数，实际为 {type(sample_rate).__name__}"
        )
    if sample_rate <= 0:
        raise ValueError(f"sample_rate 必须是正整数（Hz），实际为 {sample_rate!r}")

    ffmpeg = resolve_ffmpeg()
    source = Path(src)
    target = Path(dst)
    if not source.is_file():
        raise MediaToolError(f"待转换的媒体文件不存在：{source}")

    # 不变量 1：源文件只读，绝不允许把输出覆盖到源文件上。
    try:
        same_file = source.resolve() == target.resolve()
    except OSError:  # pragma: no cover - 路径无法解析时按「不同文件」处理
        same_file = False
    if same_file:
        raise MediaToolError(
            f"输出路径与源文件相同，拒绝覆盖只读源文件（不变量 1）：{source}"
        )

    target.parent.mkdir(parents=True, exist_ok=True)

    argv: List[str] = [
        ffmpeg,
        "-y",
        "-i",
        str(source),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-c:a",
        "pcm_s16le",
        str(target),
    ]
    try:
        completed = subprocess.run(
            argv, capture_output=True, timeout=TOOL_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired as exc:
        raise MediaToolError(
            f"ffmpeg 转码超时（>{TOOL_TIMEOUT_SECONDS} 秒）：{source} -> {target}"
        ) from exc
    except OSError as exc:
        raise MediaToolError(f"无法启动 ffmpeg（{ffmpeg}）：{exc}") from exc

    if completed.returncode != 0:
        raise MediaToolError(
            f"ffmpeg 转码失败（退出码 {completed.returncode}）：{source} -> {target}\n"
            f"{_tail(_decode(completed.stderr))}"
        )
