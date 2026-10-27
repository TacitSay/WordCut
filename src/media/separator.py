"""人声 / 伴奏分离模块（插件挂载点，未实现）。

对应 docs/架构预留设计.md 第 2 节的 ``StemSeparator``：本轮只固定接口形状，
将来接入 demucs（或其它分离模型）时只改本文件，不动 API / 数据模型。

注意：本模块当前**不读取也不写入**任何文件，函数一律抛 ``NotImplementedError``。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict

__all__ = ["separate_vocals", "DEFAULT_MODEL"]

#: 默认分离模型名（demucs 的 htdemucs）。
DEFAULT_MODEL: str = "htdemucs"


def separate_vocals(
    src: Path, out_dir: Path, model: str = DEFAULT_MODEL
) -> Dict[str, Path]:
    """把 ``src`` 分离成人声与伴奏两条音轨（预留接口，当前未实现）。

    将来的实现约定（据此实现即可，无需改动调用方）：

    * 输入 ``src`` 只读，分离结果写入 ``out_dir``（不变量 1）；
    * 返回 ``{"vocals": Path, "instrumental": Path}``，路径指向 ``out_dir`` 下的新文件；
    * 时间轴与 ``src`` 完全对齐（同样的采样率与时长），便于后续按 Token 剪辑。

    参数:
        src: 源音频路径（只读）。
        out_dir: 分离结果的输出目录。
        model: 分离模型名，默认 ``htdemucs``。

    返回:
        ``{"vocals": Path, "instrumental": Path}``。

    异常:
        NotImplementedError: 当前恒抛出。
    """
    raise NotImplementedError(
        "人声分离待实现：作为插件挂载点，见 docs/架构预留设计.md 第 2 节"
    )
