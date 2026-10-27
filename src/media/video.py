"""视频剪辑模块（插件挂载点，未实现）。

对应 docs/架构预留设计.md 第 2 节的 ``VideoCut`` / ``AVConcat``：本轮只固定
接口形状。将来实现时仍然遵守：

* 输入视频只读（不变量 1），剪辑结果写到新的 ``dst``；
* 删除区间一律是 :class:`model.DeleteSpan`，时间单位秒（不变量 2）；
* 优先 remux（``-c copy``）不重编码，必要时才回退到重编码。

注意：本模块当前**不读取也不写入**任何文件，函数一律抛 ``NotImplementedError``。
"""

from __future__ import annotations

from pathlib import Path
from typing import List

try:  # 兼容两种导入方式：src 在 sys.path 上（api.py 的扁平导入）/ 作为包导入
    from model import DeleteSpan
except ImportError:  # pragma: no cover - 仅在 ``import src.media.video`` 时走到
    from ..model import DeleteSpan  # type: ignore[no-redef]

__all__ = ["cut_video", "concat_av"]


def cut_video(src: Path, deletes: List[DeleteSpan], dst: Path) -> None:
    """按 EDL 的删除区间切视频（预留接口，当前未实现）。

    将来的实现约定：

    * ``src`` 只读，结果写到 ``dst``（不变量 1）；
    * 按 ``deletes``（每项 ``start`` / ``end`` 单位为秒）删除区间，保留其余片段，
      段间用 ffmpeg concat 拼接；
    * 时间段必须满足 ``0 <= start < end``（不变量 7），越界区间应被裁剪到视频时长内。

    参数:
        src: 源视频路径（只读）。
        deletes: 删除区间列表（:class:`model.DeleteSpan`，秒）。
        dst: 输出视频路径。

    返回:
        ``None``。

    异常:
        NotImplementedError: 当前恒抛出。
    """
    raise NotImplementedError(
        "视频按 EDL 剪辑待实现：作为插件挂载点，见 docs/架构预留设计.md 第 2 节"
    )


def concat_av(video: Path, audio: Path, dst: Path) -> None:
    """把视频轨与剪辑后的音频轨合流（预留接口，当前未实现）。

    将来的实现约定：

    * ``video`` / ``audio`` 只读，结果写到 ``dst``（不变量 1）；
    * 音频一律以 ``audio`` 为准（它是剪辑后的目标音频），视频轨按需截断 / 补静音；
    * 输出时长与 ``audio`` 时长一致（差值应在 1 帧 / 1 个音频包之内）。

    参数:
        video: 源视频路径（只读，通常已按 EDL 切好）。
        audio: 剪辑后的目标音频路径（只读）。
        dst: 输出音视频合流文件路径。

    返回:
        ``None``。

    异常:
        NotImplementedError: 当前恒抛出。
    """
    raise NotImplementedError(
        "音视频合流待实现：作为插件挂载点，见 docs/架构预留设计.md 第 2 节"
    )
