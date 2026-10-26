"""音频剪辑点精修模块。

纯 Python + numpy 实现，提供三个工具函数：

- :func:`find_silence_valley`：在剪辑点附近寻找能量最低的静音谷。
- :func:`find_zero_crossing`：把样本索引吸附到最近的过零点。
- :func:`crossfade`：等功率交叉淡化拼接两段音频。

约定：所有时间参数与返回值均以秒为单位（样本索引等显式说明的除外）。
"""

from __future__ import annotations

import numpy as np


def find_silence_valley(
    samples: np.ndarray,
    cut_time: float,
    sample_rate: int,
    window_ms: float = 80.0,
) -> float:
    """在剪辑点附近的窗口内寻找短时能量最低的位置。

    以 ``cut_time`` 为中心取长度为 ``window_ms`` 毫秒的搜索窗口，
    将窗口按 10 ms 分帧（帧间不重叠），计算每帧的短时能量
    （帧内样本平方和），返回能量最低帧的中心时间（秒）。

    参数:
        samples: 一维 numpy float32 数组，音频样本。
        cut_time: 剪辑点时间（秒）。
        sample_rate: 采样率（Hz）。
        window_ms: 搜索窗口长度（毫秒），默认为 80 ms。

    返回:
        能量最低帧的中心时间（秒）。若窗口内没有任何完整帧，
        则原样返回 ``cut_time``。

    异常:
        ValueError: ``sample_rate`` 非正或 ``samples`` 不是一维数组。
    """
    if sample_rate <= 0:
        raise ValueError("sample_rate 必须为正数")

    signal = np.asarray(samples)
    if signal.ndim != 1:
        raise ValueError("samples 必须是一维数组")
    if signal.size == 0:
        return float(cut_time)

    half_window_s = (window_ms / 1000.0) / 2.0
    start_s = max(cut_time - half_window_s, 0.0)
    end_s = min(cut_time + half_window_s, signal.size / sample_rate)

    start_idx = int(round(start_s * sample_rate))
    end_idx = int(round(end_s * sample_rate))
    start_idx = int(np.clip(start_idx, 0, signal.size))
    end_idx = int(np.clip(end_idx, 0, signal.size))

    if end_idx - start_idx <= 0:
        return float(cut_time)

    frame_len = max(1, int(round(0.010 * sample_rate)))  # 10 ms 帧长
    segment = signal[start_idx:end_idx].astype(np.float64)
    frame_count = segment.size // frame_len

    if frame_count <= 0:
        return float(cut_time)

    frames = segment[: frame_count * frame_len].reshape(frame_count, frame_len)
    energies = np.sum(frames * frames, axis=1)

    best_frame = int(np.argmin(energies))
    center_idx = start_idx + best_frame * frame_len + frame_len // 2
    return float(center_idx / sample_rate)


def find_zero_crossing(
    samples: np.ndarray,
    cut_sample: int,
    max_offset: int = 20,
) -> int:
    """在给定样本索引附近寻找最近的过零点。

    过零点定义为相邻样本符号发生变化的位置（即取后一个样本的索引，
    使得 ``samples[i - 1] * samples[i] < 0``）。若 ``cut_sample`` 本身
    即为过零点则直接返回；否则向两侧按距离由近及远查找并返回第一个
    找到的过零点。若在 ``±max_offset`` 范围内找不到过零点，则返回
    ``cut_sample`` 的截断值。

    参数:
        samples: 一维 numpy 数组，音频样本。
        cut_sample: 起始样本索引（int）。
        max_offset: 搜索半径（样本数），默认为 20。

    返回:
        过零点所对应样本的索引（int）。

    异常:
        ValueError: ``samples`` 不是一维数组。
    """
    signal = np.asarray(samples)
    if signal.ndim != 1:
        raise ValueError("samples 必须是一维数组")

    size = int(signal.size)
    if size == 0:
        return 0

    center = int(np.clip(int(cut_sample), 0, size - 1))
    radius = max(0, int(max_offset))

    def is_zero_crossing(index: int) -> bool:
        """判断 ``index`` 是否为过零点（与前一样本符号相反）。"""
        if index <= 0 or index >= size:
            return False
        prev_sample = float(signal[index - 1])
        curr_sample = float(signal[index])
        return (prev_sample < 0.0 < curr_sample) or (prev_sample > 0.0 > curr_sample)

    for offset in range(radius + 1):
        for candidate in (center - offset, center + offset):
            if is_zero_crossing(candidate):
                return int(candidate)

    return int(center)


def crossfade(
    samples_a: np.ndarray,
    samples_b: np.ndarray,
    fade_samples: int = 480,
) -> np.ndarray:
    """使用等功率交叉淡化拼接两段音频。

    取 ``samples_a`` 的尾部与 ``samples_b`` 的头部各 ``fade_samples``
    个样本作为重叠区，分别乘以 ``cos`` / ``sin`` 型等功率权重
    （``w_a = cos(t * pi / 2)``，``w_b = sin(t * pi / 2)``，
    ``t`` 在 [0, 1] 上线性分布，满足 ``w_a^2 + w_b^2 = 1``），
    重叠区前后分别直接拼接两段音频的非重叠部分。

    若任一段音频长度不足以提供完整的重叠区，则按两段中较短的
    可用长度（不超过 ``fade_samples``）进行淡化；若任一段为空，
    则直接返回另一段（或空数组）的副本。

    参数:
        samples_a: 第一段音频（一维 numpy 数组）。
        samples_b: 第二段音频（一维 numpy 数组）。
        fade_samples: 淡化样本数（默认 480，即 48 kHz 下的 10 ms）。

    返回:
        拼接后的新数组，dtype 为 ``np.result_type`` 的结果。

    异常:
        ValueError: 输入不是一维数组或 ``fade_samples`` 为负。
    """
    array_a = np.asarray(samples_a)
    array_b = np.asarray(samples_b)
    if array_a.ndim != 1 or array_b.ndim != 1:
        raise ValueError("samples_a 与 samples_b 必须是一维数组")
    if fade_samples < 0:
        raise ValueError("fade_samples 不能为负数")

    out_dtype = np.result_type(array_a.dtype, array_b.dtype)

    if array_a.size == 0:
        return array_b.astype(np.float64, copy=True).astype(out_dtype)
    if array_b.size == 0:
        return array_a.astype(np.float64, copy=True).astype(out_dtype)

    fade_len = int(min(int(fade_samples), array_a.size, array_b.size))
    if fade_len <= 0:
        return np.concatenate(
            (array_a.astype(np.float64), array_b.astype(np.float64))
        ).astype(out_dtype)

    head_a = array_a[:-fade_len].astype(np.float64)
    tail_a = array_a[-fade_len:].astype(np.float64)
    head_b = array_b[:fade_len].astype(np.float64)
    tail_b = array_b[fade_len:].astype(np.float64)

    # 等功率权重：t 取 [0, 1] 上的 fade_len 个等分点（不含终点）
    t = np.arange(fade_len, dtype=np.float64) / float(fade_len)
    weight_a = np.cos(t * np.pi / 2.0)
    weight_b = np.sin(t * np.pi / 2.0)

    blended = tail_a * weight_a + head_b * weight_b
    return np.concatenate((head_a, blended, tail_b)).astype(out_dtype)
