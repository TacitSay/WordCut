# -*- coding: utf-8 -*-
"""音频后处理模块：增益、峰值标准化、首尾淡入淡出，以及音频信息统计。

定位（见 ``docs/项目纲领.md`` 第 4 节 P1「剪辑必需」）：

* **音量标准化 / 增益 / 静音** —— 本模块负责增益与峰值标准化；
* **淡入淡出** —— 本模块负责成片首尾的淡入淡出（剪辑点之间的爆音控制由
  :func:`cut_refine.crossfade` 负责，两者不重叠）。

设计原则：

1. **纯函数**：全部函数接收 ``np.ndarray`` 并返回**新数组**，绝不原地修改入参；
   因此不变量 1（源音频只读）在渲染阶段依然成立——后处理作用在
   :func:`edl_render.render` 产出的副本上；
2. **只在渲染后生效**：删除决定音频的"长度"，增益 / 淡入淡出只改变"幅度"，
   不影响 EDL 与 :class:`model.TimeMap`，所以字幕时间轴完全不受影响；
3. **单位统一为秒与分贝**：淡入淡出时长用秒（不变量 2），增益用 dB。

处理顺序（:func:`apply`）：**增益 → 峰值标准化 → 淡入 → 淡出**。

* 开启标准化时先加增益再归一，峰值会被拉到 ``target_dbfs``，因此增益对
  最终响度不再有影响（这是刻意的：标准化就是"不管原来多响，都拉到目标"）；
* 淡入淡出放在最后，保证淡入起始与淡出结束一定是 0，不会被前面的增益抬高。

用法::

    params = AudioParams.from_mapping({"normalize": True, "fade_in": 0.3})
    out = apply(rendered, sample_rate, params)
    stats = measure(rendered, sample_rate)
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Dict, Mapping, Optional

import numpy as np

__all__ = [
    "AudioParams",
    "AudioStats",
    "DEFAULT_TARGET_DBFS",
    "SILENCE_DBFS",
    "MAX_GAIN_DB",
    "MIN_GAIN_DB",
    "MAX_FADE_SECONDS",
    "apply_gain",
    "peak_normalize",
    "apply_fade",
    "apply",
    "measure",
]

#: 峰值标准化的默认目标电平（dBFS）。留 1 dB 余量，避免转有符号整型时削顶。
DEFAULT_TARGET_DBFS: float = -1.0

#: 判定"静音"的电平阈值（dBFS）：低于该值的分析帧计为静音帧。
SILENCE_DBFS: float = -50.0

#: 允许的增益范围（dB）。超出范围视为参数非法，而不是静默截断。
MAX_GAIN_DB: float = 24.0
MIN_GAIN_DB: float = -24.0

#: 允许的单侧淡入 / 淡出时长上限（秒）。
MAX_FADE_SECONDS: float = 5.0

#: 静音占比统计的分析帧长（秒），20 ms 是语音活动检测的常用帧长。
_FRAME_SECONDS: float = 0.02

#: 视为削顶的样本绝对幅度阈值（float 归一化口径下满幅为 1.0）。
_CLIP_THRESHOLD: float = 0.999

#: 小于该峰值（线性幅度）视为全静音，标准化时直接原样返回，避免除零放大噪声。
_SILENT_PEAK: float = 1e-9


@dataclass(frozen=True)
class AudioParams:
    """导出阶段的音频后处理参数（可持久化，随工程文件保存）。

    字段:
        gain_db: 增益（dB），取值 ``[MIN_GAIN_DB, MAX_GAIN_DB]``。
        normalize: 是否做峰值标准化；为 ``True`` 时以 ``target_dbfs`` 为目标。
        fade_in: 淡入时长（秒），``0`` 表示不淡入，上限 ``MAX_FADE_SECONDS``。
        fade_out: 淡出时长（秒），``0`` 表示不淡出，上限 ``MAX_FADE_SECONDS``。
        target_dbfs: 峰值标准化目标电平（dBFS），必须为负值（不能 >= 0）。
    """

    gain_db: float = 0.0
    normalize: bool = False
    fade_in: float = 0.0
    fade_out: float = 0.0
    target_dbfs: float = DEFAULT_TARGET_DBFS

    # ------------------------------------------------------------------
    # 构造与序列化
    # ------------------------------------------------------------------
    @classmethod
    def from_mapping(cls, value: Optional[Mapping[str, Any]]) -> "AudioParams":
        """由字典（工程文件 / 请求体）构造，缺省字段取默认值。

        参数:
            value: 可能为 ``None`` 的映射；``None`` 返回全部默认值。

        返回:
            :class:`AudioParams` 实例。

        异常:
            ValueError: 字段类型不对，或数值越界。
        """
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise ValueError(f"audio_params 必须是对象，收到 {type(value).__name__}")

        gain_db = _as_float(value.get("gain_db"), 0.0, "gain_db")
        if not (MIN_GAIN_DB <= gain_db <= MAX_GAIN_DB):
            raise ValueError(
                f"gain_db 超出范围 [{MIN_GAIN_DB}, {MAX_GAIN_DB}]：{gain_db}"
            )

        fade_in = _as_float(value.get("fade_in"), 0.0, "fade_in")
        fade_out = _as_float(value.get("fade_out"), 0.0, "fade_out")
        for name, seconds in (("fade_in", fade_in), ("fade_out", fade_out)):
            if not (0.0 <= seconds <= MAX_FADE_SECONDS):
                raise ValueError(
                    f"{name} 超出范围 [0, {MAX_FADE_SECONDS}] 秒：{seconds}"
                )

        target_dbfs = _as_float(
            value.get("target_dbfs"), DEFAULT_TARGET_DBFS, "target_dbfs"
        )
        if target_dbfs >= 0.0:
            raise ValueError(f"target_dbfs 必须是负值（dBFS），收到 {target_dbfs}")

        return cls(
            gain_db=gain_db,
            normalize=_as_bool(value.get("normalize"), False, "normalize"),
            fade_in=fade_in,
            fade_out=fade_out,
            target_dbfs=target_dbfs,
        )

    def to_dict(self) -> Dict[str, Any]:
        """转成可 JSON 序列化的字典（写入 SQLite 与 .wcproj）。"""
        return asdict(self)

    def is_noop(self) -> bool:
        """判断是否完全不需要处理（全默认值时渲染链路可整段跳过）。"""
        return (
            self.gain_db == 0.0
            and not self.normalize
            and self.fade_in == 0.0
            and self.fade_out == 0.0
        )


@dataclass(frozen=True)
class AudioStats:
    """音频信息统计结果（用于界面展示与论文第 6 章数据）。"""

    duration: float
    sample_rate: int
    sample_count: int
    peak_dbfs: float
    rms_dbfs: float
    crest_factor_db: float
    silence_ratio: float
    clipped_samples: int

    def to_dict(self) -> Dict[str, Any]:
        """转成可 JSON 序列化的字典。"""
        return asdict(self)


# ----------------------------------------------------------------------
# 内部工具
# ----------------------------------------------------------------------
def _as_float(value: Any, default: float, name: str) -> float:
    """把请求字段转成有限浮点数；``None`` / 缺省时用默认值。"""
    if value is None:
        return float(default)
    if isinstance(value, bool):
        raise ValueError(f"{name} 必须是数字，不能是布尔值")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是数字：{value!r}") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} 必须是有限数字：{value!r}")
    return result


def _as_bool(value: Any, default: bool, name: str) -> bool:
    """把请求字段转成布尔值（只接受真正的 bool，避免 "false" 被当成 True）。"""
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    raise ValueError(f"{name} 必须是布尔值：{value!r}")


def _as_signal(samples: Any, name: str = "samples") -> np.ndarray:
    """校验入参是一维数值数组，并转成 float64 副本。"""
    array = np.asarray(samples)
    if array.ndim != 1:
        raise ValueError(f"{name} 必须是一维数组，收到 {array.ndim} 维")
    if array.dtype.kind not in "fiu":
        raise TypeError(f"{name} 必须是数值数组，收到 dtype={array.dtype}")
    return array.astype(np.float64, copy=True)


def _as_rate(sample_rate: Any) -> int:
    """校验采样率是正整数。"""
    if isinstance(sample_rate, bool):
        raise TypeError("sample_rate 必须是正整数，不能是布尔值")
    try:
        rate = int(sample_rate)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"sample_rate 必须是整数：{sample_rate!r}") from exc
    if rate <= 0:
        raise ValueError(f"sample_rate 必须为正数：{rate}")
    return rate


def _to_dbfs(linear: float) -> float:
    """线性幅度转 dBFS；0 及以下返回 ``-inf`` 表示全静音。"""
    if linear <= 0.0:
        return float("-inf")
    return float(20.0 * math.log10(linear))


# ----------------------------------------------------------------------
# 增益 / 标准化 / 淡入淡出
# ----------------------------------------------------------------------
def apply_gain(samples: np.ndarray, gain_db: float) -> np.ndarray:
    """对音频施加固定增益。

    参数:
        samples: 一维音频样本（只读，不会被修改）。
        gain_db: 增益（dB）；正数放大、负数衰减，``0`` 返回等值副本。

    返回:
        float64 新数组；不在此处削顶（削顶与否交给导出编码器与
        :func:`measure` 的 ``clipped_samples`` 统计）。

    异常:
        TypeError / ValueError: 入参类型或维度非法。
    """
    signal = _as_signal(samples)
    gain_db = _as_float(gain_db, 0.0, "gain_db")
    if not (MIN_GAIN_DB <= gain_db <= MAX_GAIN_DB):
        raise ValueError(
            f"gain_db 超出范围 [{MIN_GAIN_DB}, {MAX_GAIN_DB}]：{gain_db}"
        )
    if gain_db == 0.0 or signal.size == 0:
        return signal
    return signal * (10.0 ** (gain_db / 20.0))


def peak_normalize(
    samples: np.ndarray,
    target_dbfs: float = DEFAULT_TARGET_DBFS,
) -> np.ndarray:
    """把音频峰值拉到指定电平（峰值标准化）。

    只做"峰值对齐"，不做响度（LUFS）均衡——这是刻意的取舍：峰值标准化
    结果可预测、可复现，也不需要额外的响度测量依赖；论文中作为
    "音量标准化"的基线方案说明即可。

    参数:
        samples: 一维音频样本（只读）。
        target_dbfs: 目标峰值电平（dBFS），必须为负值。

    返回:
        float64 新数组。原音频峰值不大于 ``_SILENT_PEAK``（近似全静音）时
        原样返回，避免把底噪放大成巨响。

    异常:
        TypeError / ValueError: 入参非法，或 ``target_dbfs`` 非负。
    """
    signal = _as_signal(samples)
    target_dbfs = _as_float(target_dbfs, DEFAULT_TARGET_DBFS, "target_dbfs")
    if target_dbfs >= 0.0:
        raise ValueError(f"target_dbfs 必须是负值（dBFS），收到 {target_dbfs}")
    if signal.size == 0:
        return signal

    peak = float(np.max(np.abs(signal)))
    if peak <= _SILENT_PEAK:
        return signal

    target = 10.0 ** (target_dbfs / 20.0)
    return signal * (target / peak)


def apply_fade(
    samples: np.ndarray,
    sample_rate: int,
    fade_in: float = 0.0,
    fade_out: float = 0.0,
) -> np.ndarray:
    """在成片首尾施加淡入 / 淡出。

    使用升余弦（Hann 半窗）权重 ``w(t) = 0.5 - 0.5 * cos(pi * t)``，
    ``t`` 在 ``[0, 1]`` 上取 ``n`` 个等分点：起点为 0、终点趋近 1，
    一阶导连续，听感上比线性淡变更平滑，也不会在淡入起点引入咔哒声。

    若淡入 + 淡出总时长超过音频长度，按两者比例等比压缩到总长，
    保证淡入区与淡出区不重叠。

    参数:
        samples: 一维音频样本（只读）。
        sample_rate: 采样率（Hz），正整数。
        fade_in: 淡入时长（秒），``0`` 表示不淡入。
        fade_out: 淡出时长（秒），``0`` 表示不淡出。

    返回:
        float64 新数组。

    异常:
        TypeError / ValueError: 入参非法，或淡入淡出时长为负 / 超上限。
    """
    signal = _as_signal(samples)
    rate = _as_rate(sample_rate)
    fade_in = _as_float(fade_in, 0.0, "fade_in")
    fade_out = _as_float(fade_out, 0.0, "fade_out")
    for name, seconds in (("fade_in", fade_in), ("fade_out", fade_out)):
        if not (0.0 <= seconds <= MAX_FADE_SECONDS):
            raise ValueError(f"{name} 超出范围 [0, {MAX_FADE_SECONDS}] 秒：{seconds}")

    size = int(signal.size)
    if size == 0 or (fade_in == 0.0 and fade_out == 0.0):
        return signal

    in_samples = int(round(fade_in * rate))
    out_samples = int(round(fade_out * rate))
    in_samples = max(0, min(in_samples, size))
    out_samples = max(0, min(out_samples, size))

    # 淡入与淡出重叠时按比例压缩，保证两段不重叠
    if in_samples + out_samples > size and (in_samples + out_samples) > 0:
        scale = size / float(in_samples + out_samples)
        in_samples = int(in_samples * scale)
        out_samples = min(size - in_samples, int(out_samples * scale))

    result = signal
    if in_samples > 0:
        # t 取 [0, 1) 上的 in_samples 个等分点：首样本权重为 0
        t = np.arange(in_samples, dtype=np.float64) / float(in_samples)
        result[:in_samples] = result[:in_samples] * (0.5 - 0.5 * np.cos(np.pi * t))
    if out_samples > 0:
        # 反向：末样本权重为 0
        t = np.arange(out_samples, dtype=np.float64)[::-1] / float(out_samples)
        result[size - out_samples :] = result[size - out_samples :] * (
            0.5 - 0.5 * np.cos(np.pi * t)
        )
    return result


def apply(
    samples: np.ndarray,
    sample_rate: int,
    params: Optional[AudioParams] = None,
) -> np.ndarray:
    """按处理顺序一次性施加全部后处理：增益 → 标准化 → 淡入 → 淡出。

    参数:
        samples: 一维音频样本（只读）。
        sample_rate: 采样率（Hz）。
        params: 后处理参数；``None`` 或全默认值时返回等值副本。

    返回:
        float64 新数组；入参永远不会被修改（不变量 1）。

    异常:
        TypeError / ValueError: 入参或参数非法。
    """
    signal = _as_signal(samples)
    rate = _as_rate(sample_rate)
    if params is None:
        return signal
    if not isinstance(params, AudioParams):
        raise TypeError(f"params 必须是 AudioParams，收到 {type(params).__name__}")
    if params.is_noop() or signal.size == 0:
        return signal

    result = signal
    if params.gain_db != 0.0:
        result = apply_gain(result, params.gain_db)
    if params.normalize:
        result = peak_normalize(result, params.target_dbfs)
    if params.fade_in > 0.0 or params.fade_out > 0.0:
        result = apply_fade(result, rate, params.fade_in, params.fade_out)
    return result


# ----------------------------------------------------------------------
# 音频信息统计
# ----------------------------------------------------------------------
def measure(
    samples: np.ndarray,
    sample_rate: int,
    silence_dbfs: float = SILENCE_DBFS,
) -> AudioStats:
    """统计音频的客观指标，供界面展示与论文实验数据使用。

    指标口径:

    * ``peak_dbfs``：全片样本绝对值的最大值，换算成 dBFS；
    * ``rms_dbfs``：全片均方根电平；
    * ``crest_factor_db``：峰值与 RMS 的差值（波峰因数），越大越"尖"，可反映
      动态范围是否被压缩过度；
    * ``silence_ratio``：按 20 ms 分帧，帧 RMS 低于 ``silence_dbfs`` 的帧占比；
    * ``clipped_samples``：``|x| >= 0.999`` 的样本数（削顶检测）。

    参数:
        samples: 一维音频样本（只读）。
        sample_rate: 采样率（Hz）。
        silence_dbfs: 静音判定阈值（dBFS），必须为负值。

    返回:
        :class:`AudioStats` 实例。

    异常:
        TypeError / ValueError: 入参非法。
    """
    signal = _as_signal(samples)
    rate = _as_rate(sample_rate)
    silence_dbfs = _as_float(silence_dbfs, SILENCE_DBFS, "silence_dbfs")
    if silence_dbfs >= 0.0:
        raise ValueError(f"silence_dbfs 必须是负值（dBFS），收到 {silence_dbfs}")

    size = int(signal.size)
    duration = size / float(rate) if size else 0.0
    if size == 0:
        return AudioStats(
            duration=0.0,
            sample_rate=rate,
            sample_count=0,
            peak_dbfs=float("-inf"),
            rms_dbfs=float("-inf"),
            crest_factor_db=0.0,
            silence_ratio=1.0,
            clipped_samples=0,
        )

    peak = float(np.max(np.abs(signal)))
    rms = float(np.sqrt(np.mean(np.square(signal))))
    peak_dbfs = _to_dbfs(peak)
    rms_dbfs = _to_dbfs(rms)
    crest = (
        float(peak_dbfs - rms_dbfs)
        if math.isfinite(peak_dbfs) and math.isfinite(rms_dbfs)
        else 0.0
    )

    frame = max(1, int(round(_FRAME_SECONDS * rate)))
    frame_count = int(math.ceil(size / float(frame)))
    padded = np.zeros(frame_count * frame, dtype=np.float64)
    padded[:size] = signal
    frames = padded.reshape(frame_count, frame)
    frame_rms = np.sqrt(np.mean(np.square(frames), axis=1))
    silence_threshold = 10.0 ** (silence_dbfs / 20.0)
    silent_frames = int(np.count_nonzero(frame_rms < silence_threshold))
    silence_ratio = silent_frames / float(frame_count)

    clipped = int(np.count_nonzero(np.abs(signal) >= _CLIP_THRESHOLD))

    return AudioStats(
        duration=round(duration, 6),
        sample_rate=rate,
        sample_count=size,
        peak_dbfs=round(peak_dbfs, 2) if math.isfinite(peak_dbfs) else float("-inf"),
        rms_dbfs=round(rms_dbfs, 2) if math.isfinite(rms_dbfs) else float("-inf"),
        crest_factor_db=round(crest, 2),
        silence_ratio=round(silence_ratio, 4),
        clipped_samples=clipped,
    )
