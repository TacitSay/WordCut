"""ffmpeg / ffprobe 可执行文件解析模块。

顺序：环境变量（``WORDCUT_FFMPEG`` / ``WORDCUT_FFPROBE``）→ ``shutil.which``
→ 抛 :class:`MediaToolError`。

同时定义整个 ``media`` 包共用的异常 :class:`MediaToolError`：所有「工具缺失 /
调用失败 / 输出不可解析」的错误都收敛到这一个类型，调用方只需 ``except``
一种异常。

不涉及任何文件写入（不变量 1：源文件只读），也不产生任何时间值。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

__all__ = [
    "MediaToolError",
    "FFMPEG_ENV",
    "FFPROBE_ENV",
    "TOOL_TIMEOUT_SECONDS",
    "resolve_ffmpeg",
    "resolve_ffprobe",
]

#: 指定 ffmpeg 可执行文件路径的环境变量名。
FFMPEG_ENV: str = "WORDCUT_FFMPEG"

#: 指定 ffprobe 可执行文件路径的环境变量名。
FFPROBE_ENV: str = "WORDCUT_FFPROBE"

#: 各子进程调用的统一超时时间（秒）。
TOOL_TIMEOUT_SECONDS: int = 600


class MediaToolError(RuntimeError):
    """媒体工具（ffmpeg / ffprobe）缺失、调用失败或输出不可解析。

    继承 :class:`RuntimeError`，方便上层按「运行时环境问题」统一处理。
    """


def _resolve_tool(env_name: str, exe_name: str, description: str) -> str:
    """按「环境变量 → PATH」的顺序解析工具路径。

    参数:
        env_name: 环境变量名，其值可以是绝对路径，也可以是命令名。
        exe_name: 在 PATH 中查找的命令名（如 ``ffmpeg``）。
        description: 出现在错误信息里的中文工具说明（如 ``ffmpeg 可执行文件``）。

    返回:
        工具的可执行文件路径（字符串）。

    异常:
        MediaToolError: 环境变量指向不存在的路径，或 PATH 中也找不到该工具。
    """
    raw = os.environ.get(env_name, "").strip()
    if raw:
        candidate = Path(raw).expanduser()
        if candidate.is_file():
            return str(candidate)
        located = shutil.which(raw)
        if located:
            return located
        raise MediaToolError(
            f"环境变量 {env_name} 指向的{description}不存在或不可执行：{raw!r}；"
            f"请修正 {env_name}，或删除该变量让程序在 PATH 中自动查找 {exe_name}"
        )

    located = shutil.which(exe_name)
    if located:
        return located
    raise MediaToolError(
        f"未找到{description}（{exe_name}）：请先安装 ffmpeg 并加入系统 PATH，"
        f"或用环境变量 {env_name} 指定其绝对路径"
    )


def resolve_ffmpeg() -> str:
    """解析 ffmpeg 可执行文件路径。

    返回:
        ffmpeg 的绝对路径（字符串）。

    异常:
        MediaToolError: 环境变量非法，且 PATH 中也没有 ffmpeg。
    """
    return _resolve_tool(FFMPEG_ENV, "ffmpeg", " ffmpeg 可执行文件")


def resolve_ffprobe() -> str:
    """解析 ffprobe 可执行文件路径。

    返回:
        ffprobe 的绝对路径（字符串）。

    异常:
        MediaToolError: 环境变量非法，且 PATH 中也没有 ffprobe。
    """
    return _resolve_tool(FFPROBE_ENV, "ffprobe", " ffprobe 可执行文件")
