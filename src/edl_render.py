"""音频剪辑渲染模块。

纯 Python + numpy + soundfile 实现，把 :class:`model.EDL` 中的删除决策
应用到 PCM 样本上，生成剪辑后的新音频：

- :func:`compute_keep_segments`：由删除区间求保留区间（删除区间的补集）。
- :func:`render`：按保留区间切片样本，相邻保留区间用等功率交叉淡化拼接。
- :func:`render_edl`：接受 ``model.EDL``，内部调用 :func:`render`。

设计约定（对应 docs/不变量.md）：

* **源样本只读**：三个函数都不修改传入的 ``samples``，返回的是新数组；
  调用方持有的 ``samples`` 与返回数组不共享可写内存。
* **时间单位为秒**（float），仅在内部按 ``sample_rate`` 换算成样本索引。
* **边界取整是幂等的**：同一秒值换算出的样本索引恒定，同一份 EDL
  重复渲染得到逐样本一致的结果（可复现）。
"""

from __future__ import annotations

from typing import List, Sequence, Tuple

import numpy as np

from cut_refine import crossfade
from model import DeleteSpan, EDL

__all__ = [
    "DEFAULT_FADE_SAMPLES",
    "compute_keep_segments",
    "render",
    "render_edl",
]

# 默认交叉淡化长度（样本数）：48 kHz 下约 10 ms。
DEFAULT_FADE_SAMPLES: int = 480

# 浮点比较容差：样本索引换算、区间端点比较都用它吸收误差。
_EPS = 1e-9


# ----------------------------------------------------------------------
# 1. 保留区间
# ----------------------------------------------------------------------
def compute_keep_segments(
    deletes: Sequence[DeleteSpan],
    total_duration: float,
) -> List[Tuple[float, float]]:
    """求保留区间：删除区间在 ``[0, total_duration]`` 上的补集。

    流程：先把删除区间按起点排序，合并重叠或首尾相接的区间，再取补集。
    超出音频范围的删除区间会被裁剪到 ``[0, total_duration]`` 内；
    完全落在范围外的区间不会产生任何保留结果。

    参数:
        deletes: 删除区间序列，每项需有 ``.start`` 与 ``.end`` 属性（单位：秒）。
            可以是 :class:`model.DeleteSpan`，也可以是任何同名属性的对象。
        total_duration: 源音频总时长（秒），必须为非负有限数。

    返回:
        保留区间列表，每项为 ``(start, end)``（秒），满足
        ``0 <= start < end <= total_duration``，按时间升序、
        互不重叠且首尾不相接。没有可保留内容时返回空列表；
        ``total_duration == 0`` 时返回空列表。

    异常:
        TypeError: ``total_duration`` 不是数值，或某一项缺少 ``start``/``end``。
        ValueError: ``total_duration`` 为负或非有限；某个区间 ``start >= end``。
    """
    duration = _as_finite_float(total_duration, "total_duration")
    if duration < 0.0:
        raise ValueError(f"total_duration 必须 >= 0，实际为 {duration!r}")

    spans: List[Tuple[float, float]] = []
    for index, span in enumerate(deletes):
        start = _as_finite_float(_span_attr(span, "start", index), f"deletes[{index}].start")
        end = _as_finite_float(_span_attr(span, "end", index), f"deletes[{index}].end")
        if not start < end:
            raise ValueError(
                f"deletes[{index}] 必须满足 start < end，实际为 start={start!r}, end={end!r}"
            )
        # 裁剪到音频范围内。
        clipped_start = max(start, 0.0)
        clipped_end = min(end, duration)
        if clipped_start < clipped_end:
            spans.append((clipped_start, clipped_end))

    if not spans or duration <= 0.0:
        return [(0.0, duration)] if duration > 0.0 else []

    spans.sort()

    # 合并重叠 / 首尾相接的删除区间。
    merged: List[Tuple[float, float]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1] + _EPS:
            prev_start, prev_end = merged[-1]
            if end > prev_end:
                merged[-1] = (prev_start, end)
        else:
            merged.append((start, end))

    # 取补集：删除区间之间的空隙就是保留区间。
    keep: List[Tuple[float, float]] = []
    cursor = 0.0
    for start, end in merged:
        if start > cursor:
            keep.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration:
        keep.append((cursor, duration))
    return keep


# ----------------------------------------------------------------------
# 2. 渲染
# ----------------------------------------------------------------------
def render(
    samples: np.ndarray,
    sample_rate: int,
    deletes: Sequence[DeleteSpan],
    fade_samples: int = DEFAULT_FADE_SAMPLES,
) -> np.ndarray:
    """按删除区间渲染剪辑后的音频。

    把 ``samples`` 切成保留区间对应的片段，再用
    :func:`cut_refine.crossfade` 把相邻片段等功率交叉淡化拼接起来。
    传入的 ``samples`` 不会被修改。

    细节:

    * 时间 -> 样本索引用 ``round(t * sample_rate)``，并裁剪到 ``[0, len(samples)]``；
      总时长即 ``len(samples) / sample_rate``，因此最后一个保留区间恰好覆盖到末尾。
    * 交叉淡化只在**真实的剪辑点**（两段保留区间之间隔了一次删除）生效；
      若相邻保留区间在源轴上本是连续的（中间只删了几个样本，小于一个淡化窗），
      就直接接回来，避免留下无意义的淡化痕迹。
    * 每个剪辑点会让输出比"源长 - 删除时长"再短 ``fade_samples`` 个样本
      （交叉淡化把两个淡化窗叠成一个），需要精确时长时请调用方自行补偿。
    * 若删除后没有任何保留区间，返回与输入等长的全零数组，而不是空数组，
      以免下游把"全删了"误当成"没有音频"。

    参数:
        samples: 一维 float32 音频样本数组（只读）。
        sample_rate: 采样率（Hz），必须为正整数。
        deletes: 删除区间序列，每项需有 ``.start`` 与 ``.end`` 属性（单位：秒）。
        fade_samples: 交叉淡化长度（样本数），默认 480。传 0 表示硬切。

    返回:
        拼接后的新数组，一维 ``np.float32``，与 ``samples`` 不共享内存。

    异常:
        ValueError: ``samples`` 不是一维数组、``sample_rate`` 非正、
            或 ``fade_samples`` 为负。
    """
    source = np.asarray(samples)
    if source.ndim != 1:
        raise ValueError("samples 必须是一维数组")
    if sample_rate <= 0:
        raise ValueError(f"sample_rate 必须为正数，实际为 {sample_rate!r}")
    if fade_samples < 0:
        raise ValueError(f"fade_samples 不能为负数，实际为 {fade_samples!r}")

    total_samples = int(source.size)
    if total_samples == 0:
        return np.zeros(0, dtype=np.float32)

    rate = int(sample_rate)
    fade = int(fade_samples)
    total_duration = total_samples / float(rate)
    keep_segments = compute_keep_segments(deletes, total_duration)

    # 时间区间 -> 样本索引区间（连续且非空）。
    # 相邻保留区间若在源轴上连续（只差取整误差，或中间只删了几个样本），
    # 就按连续处理、直接接回；真正隔了一段删除的才是剪辑点，需要交叉淡化。
    pieces: List[Tuple[int, int]] = []
    joins: List[bool] = []  # joins[i] 表示 pieces[i] 与 pieces[i-1] 是否连续
    for start_s, end_s in keep_segments:
        start_idx = _time_to_index(start_s, rate, total_samples)
        end_idx = _time_to_index(end_s, rate, total_samples)
        # 端点由 round() 各自取整，首尾可能相差一两个样本；
        # 缝隙短于一个淡化窗即视为"源轴上连续"。
        contiguous = bool(pieces) and start_idx - pieces[-1][1] < max(1, fade)
        if pieces and start_idx < pieces[-1][1]:
            # 理论上不会发生（保留区间互不重叠），保险起见夹住。
            start_idx = pieces[-1][1]
        if end_idx > start_idx:
            pieces.append((start_idx, end_idx))
            if len(pieces) > 1:
                joins.append(contiguous)

    if not pieces:
        # 全部删光：输出与源等长的静音，保持时长语义不退化。
        return np.zeros(total_samples, dtype=np.float32)

    result: np.ndarray = np.zeros(0, dtype=np.float32)
    for piece_index, (start_idx, end_idx) in enumerate(pieces):
        piece = np.asarray(source[start_idx:end_idx], dtype=np.float32)
        if piece_index == 0:
            result = piece.copy()
            continue

        prev_end = pieces[piece_index - 1][1]
        if joins[piece_index - 1] or start_idx <= prev_end:
            # 源轴上连续（只是被取整切开了），直接接回去。
            result = np.concatenate((result, piece))
        else:
            result = np.asarray(
                crossfade(result, piece, fade_samples=fade), dtype=np.float32
            )

    return np.ascontiguousarray(result, dtype=np.float32)


def render_edl(
    samples: np.ndarray,
    sample_rate: int,
    edl: EDL,
    fade_samples: int = DEFAULT_FADE_SAMPLES,
) -> np.ndarray:
    """按 :class:`model.EDL` 渲染剪辑后的音频。

    是对 :func:`render` 的薄封装：取出 ``edl.deletes`` 交给渲染器，
    并把 ``edl.sample_rate`` 与传入的 ``sample_rate`` 做一致性校验
    （两者不符说明样本与 EDL 不是同一份素材，此时按传入值渲染会得到错误时长）。

    参数:
        samples: 一维 float32 音频样本数组（只读）。
        sample_rate: 样本的实际采样率（Hz）。
        edl: 编辑决策列表，取其中的 ``deletes``。
        fade_samples: 交叉淡化长度（样本数），默认 480。

    返回:
        拼接后的新数组，一维 ``np.float32``，与 ``samples`` 不共享内存。

    异常:
        TypeError: ``edl`` 不是 :class:`model.EDL`。
        ValueError: ``edl.sample_rate`` 与 ``sample_rate`` 不一致，
            或底层 :func:`render` 的参数校验失败。
    """
    if not isinstance(edl, EDL):
        raise TypeError(f"edl 必须是 model.EDL，实际为 {type(edl).__name__}")
    if int(edl.sample_rate) != int(sample_rate):
        raise ValueError(
            f"edl.sample_rate={edl.sample_rate!r} 与 sample_rate={sample_rate!r} 不一致"
        )
    return render(samples, sample_rate, edl.deletes, fade_samples=fade_samples)


# ----------------------------------------------------------------------
# 内部工具
# ----------------------------------------------------------------------
def _span_attr(span: object, name: str, index: int) -> object:
    """读取删除区间的 ``start`` / ``end`` 属性；缺失时抛 TypeError。"""
    try:
        return getattr(span, name)
    except AttributeError:
        raise TypeError(
            f"deletes[{index}] 缺少属性 {name!r}（需要带 .start/.end 的区间对象，"
            f"实际为 {type(span).__name__}）"
        ) from None


def _as_finite_float(value: object, label: str) -> float:
    """把 int/float 统一成 float，拒绝 bool、非数值、NaN 与无穷大。"""
    if isinstance(value, bool) or not isinstance(value, (int, float, np.integer, np.floating)):
        raise TypeError(f"{label} 必须是 int/float，实际为 {type(value).__name__}")
    result = float(value)
    if not np.isfinite(result):
        raise ValueError(f"{label} 必须是有限数，实际为 {result!r}")
    return result


def _time_to_index(time_s: float, sample_rate: int, total_samples: int) -> int:
    """秒 -> 样本索引，四舍五入后裁剪到 ``[0, total_samples]``。"""
    index = int(round(time_s * sample_rate))
    return max(0, min(index, total_samples))
