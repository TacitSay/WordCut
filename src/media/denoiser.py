# -*- coding: utf-8 -*-
"""降噪模块：基于 ffmpeg ``afftdn`` 的简单降噪（纲领 P2「克制」项）。

设计取舍（为什么不在 numpy 里自己写频谱门限）：

* 朴素频谱减法 / 门限容易产生"音乐噪声"——残留的随机频点在安静段听起来像
  水声，答辩演示时反而显得效果差；
* ``afftdn`` 是 ffmpeg 里成熟的自适应频域降噪，带噪声底噪跟踪（``tn``），
  不需要自己调参就能得到"不刺耳"的结果；
* 纲领对 P2 的定位就是"只做简单版、不追求效果"，用现成滤镜比自研算法更符合
  这一取舍，也少一个需要论文论证的算法黑箱。

实现方式：numpy 数组 → 临时 WAV → ffmpeg ``-af afftdn`` → 读回 numpy。
用临时文件而不是管道，是为了在 Windows 上稳定（管道在部分受限环境下不可用）。

不变量：

* 不变量 1：源音频只读。本模块只处理传入数组的副本，临时文件放在系统临时目录并
  自动清理；
* 不变量 2：时间单位秒（本模块不涉及时间，仅保持采样率一致）。
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import numpy as np
import soundfile as sf

from .ffmpeg_bin import TOOL_TIMEOUT_SECONDS, MediaToolError, resolve_ffmpeg

__all__ = [
    "DEFAULT_NOISE_FLOOR_DB",
    "MIN_NOISE_FLOOR_DB",
    "MAX_NOISE_FLOOR_DB",
    "denoise_array",
]

#: 默认噪声底噪（dB）：低于该电平的内容被视为噪声并被压制。
DEFAULT_NOISE_FLOOR_DB: float = -30.0

#: 允许的噪声底噪范围（dB）。太高会削掉气声，太低则降噪不明显。
MIN_NOISE_FLOOR_DB: float = -80.0
MAX_NOISE_FLOOR_DB: float = -5.0

#: 错误信息里保留的 stderr 末尾字符数。
_STDERR_TAIL_CHARS: int = 1500


def _as_signal(samples: object) -> np.ndarray:
    """校验入参是一维数值数组，返回 float64 副本。"""
    array = np.asarray(samples)
    if array.ndim != 1:
        raise ValueError(f"samples 必须是一维数组，收到 {array.ndim} 维")
    if array.dtype.kind not in "fiu":
        raise TypeError(f"samples 必须是数值数组，收到 dtype={array.dtype}")
    return array.astype(np.float64, copy=True)


def _as_rate(sample_rate: object) -> int:
    """校验采样率为正整数。"""
    if isinstance(sample_rate, bool):
        raise TypeError("sample_rate 必须是正整数，不能是布尔值")
    try:
        rate = int(sample_rate)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise TypeError(f"sample_rate 必须是整数：{sample_rate!r}") from exc
    if rate <= 0:
        raise ValueError(f"sample_rate 必须为正数：{rate}")
    return rate


def denoise_array(
    samples: np.ndarray,
    sample_rate: int,
    noise_floor_db: Optional[float] = DEFAULT_NOISE_FLOOR_DB,
) -> np.ndarray:
    """用 ffmpeg ``afftdn`` 对音频降噪，返回新数组（入参不变）。

    参数:
        samples: 一维音频样本（只读，不会被修改）。
        sample_rate: 采样率（Hz），正整数。
        noise_floor_db: 噪声底噪（dBFS），``None`` 表示用 ffmpeg 默认值；
            给定时会传入 ``afftdn=nf=<值>``。

    返回:
        float64 新数组，长度与入参一致（ffmpeg 可能因滤波引入极微小差异，
        这里按原长度对齐，短了补零、长了截断）。

    异常:
        MediaToolError: 找不到 ffmpeg，或 ffmpeg 执行失败 / 超时。
        TypeError / ValueError: 入参类型或取值范围非法。
    """
    signal = _as_signal(samples)
    rate = _as_rate(sample_rate)

    if noise_floor_db is not None:
        try:
            floor = float(noise_floor_db)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"noise_floor_db 必须是数字：{noise_floor_db!r}") from exc
        if not (MIN_NOISE_FLOOR_DB <= floor <= MAX_NOISE_FLOOR_DB):
            raise ValueError(
                f"noise_floor_db 超出范围 [{MIN_NOISE_FLOOR_DB}, {MAX_NOISE_FLOOR_DB}]：{floor}"
            )
    else:
        floor = None

    # 空音频没有可降噪的内容，直接返回（也避免 ffmpeg 处理 0 帧文件出错）
    if signal.size == 0:
        return signal

    ffmpeg = resolve_ffmpeg()
    # 时长不足 50ms 的碎片同样跳过：afftdn 需要足够的帧才能估计噪声谱
    if signal.size < max(64, int(rate * 0.05)):
        return signal

    filter_expr = "afftdn=tn=1" if floor is None else f"afftdn=nf={floor:.2f}:tn=1"

    with tempfile.TemporaryDirectory(prefix="wordcut_denoise_") as tmp:
        src = Path(tmp) / "in.wav"
        dst = Path(tmp) / "out.wav"
        sf.write(str(src), np.clip(signal, -1.0, 1.0).astype(np.float32), rate, subtype="FLOAT")
        argv = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(src),
            "-af",
            filter_expr,
            "-c:a",
            "pcm_f32le",
            str(dst),
        ]
        try:
            completed = subprocess.run(
                argv, capture_output=True, timeout=TOOL_TIMEOUT_SECONDS
            )
        except subprocess.TimeoutExpired as exc:
            raise MediaToolError(
                f"ffmpeg 降噪超时（>{TOOL_TIMEOUT_SECONDS} 秒）"
            ) from exc
        except OSError as exc:
            raise MediaToolError(f"无法启动 ffmpeg（{ffmpeg}）：{exc}") from exc

        if completed.returncode != 0:
            detail = (completed.stderr or b"").decode("utf-8", errors="replace").strip()
            if len(detail) > _STDERR_TAIL_CHARS:
                detail = "…" + detail[-_STDERR_TAIL_CHARS:]
            raise MediaToolError(
                f"ffmpeg 降噪失败（退出码 {completed.returncode}）：{detail}"
            )

        if not dst.is_file():
            raise MediaToolError("ffmpeg 未生成降噪输出文件")

        data, out_rate = sf.read(str(dst), dtype="float64", always_2d=False)
        if int(out_rate) != rate:
            raise MediaToolError(
                f"降噪输出采样率异常：期望 {rate}，实际 {out_rate}"
            )

    cleaned = np.asarray(data, dtype=np.float64).reshape(-1)
    # 长度对齐：极少数情况下滤镜链会差一两个样本
    if cleaned.size < signal.size:
        cleaned = np.pad(cleaned, (0, signal.size - cleaned.size))
    elif cleaned.size > signal.size:
        cleaned = cleaned[: signal.size]
    return cleaned
