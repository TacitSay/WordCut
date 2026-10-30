# -*- coding: utf-8 -*-
"""media.denoiser 测试：ffmpeg afftdn 降噪。

覆盖点：

* 纯函数性：入参数组不被修改；
* 长度与采样率保持不变；
* 静音段（纯噪声）电平明显下降，而语音段能量不至于被削光；
* 参数校验（越界 / 类型错误）抛异常；
* 环境缺 ffmpeg 时整体 SKIP（不把环境问题算成代码失败）。

注意：本测试依赖 ffmpeg。若本机没有 ffmpeg，会打印 SKIP 并以 0 退出。
"""
import sys

import numpy as np

sys.path.insert(0, r"E:\WordCut\src")

from media.denoiser import (  # noqa: E402
    MAX_NOISE_FLOOR_DB,
    MIN_NOISE_FLOOR_DB,
    denoise_array,
)
from media.ffmpeg_bin import MediaToolError  # noqa: E402

passed = 0
failed = 0
skipped = False


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"PASS: {name}")
    else:
        failed += 1
        print(f"FAIL: {name}")


def rms(fragment):
    return float(np.sqrt(np.mean(np.square(fragment))))


RATE = 16000
rng = np.random.default_rng(20261030)
seconds = 1.0
t = np.arange(int(seconds * RATE), dtype=np.float64) / RATE

# 前 0.4s 只有底噪，0.4–0.6s 是"人声"（440Hz 正弦），0.6s 之后又是底噪
speech = np.zeros(t.size, dtype=np.float64)
speech[int(0.4 * RATE) : int(0.6 * RATE)] = 0.5 * np.sin(
    2.0 * np.pi * 440.0 * t[int(0.4 * RATE) : int(0.6 * RATE)]
)
noise = rng.normal(0.0, 0.01, t.size)  # 约 -40 dBFS
signal = speech + noise
original = signal.copy()

try:
    cleaned = denoise_array(signal, RATE, -30.0)
except MediaToolError as exc:
    skipped = True
    print(f"SKIP: 本机无法执行 ffmpeg 降噪：{exc}")
    cleaned = None

if cleaned is not None:
    check("降噪不修改入参", np.array_equal(signal, original))
    check("降噪返回新数组", cleaned is not signal)
    check("长度保持不变", cleaned.size == signal.size)
    check("结果为 float64", cleaned.dtype == np.float64)
    check("结果不含 NaN", bool(np.all(np.isfinite(cleaned))))

    quiet_before = rms(signal[: int(0.3 * RATE)])
    quiet_after = rms(cleaned[: int(0.3 * RATE)])
    ratio = quiet_after / quiet_before if quiet_before else 1.0
    print(f"  底噪 RMS: {quiet_before:.5f} -> {quiet_after:.5f}（{ratio:.2%}）")
    check("静音段底噪被压到原来的 70% 以下", ratio < 0.7)

    voice_before = rms(signal[int(0.42 * RATE) : int(0.58 * RATE)])
    voice_after = rms(cleaned[int(0.42 * RATE) : int(0.58 * RATE)])
    keep = voice_after / voice_before if voice_before else 0.0
    print(f"  语音段 RMS: {voice_before:.5f} -> {voice_after:.5f}（保留 {keep:.0%}）")
    check("语音段仍保留 50% 以上能量", keep > 0.5)

    # 边界：噪声底噪可调，收到更严的值不应崩
    try:
        stricter = denoise_array(signal, RATE, -45.0)
        check("更低的噪声底噪也能跑通", stricter.size == signal.size)
    except MediaToolError as exc:  # pragma: no cover
        check(f"更低的噪声底噪也能跑通（{exc}）", False)

    try:
        denoise_array(signal, RATE, MAX_NOISE_FLOOR_DB + 1)
        check("噪声底噪越界应抛 ValueError", False)
    except ValueError:
        check("噪声底噪越界应抛 ValueError", True)

    try:
        denoise_array(signal, RATE, MIN_NOISE_FLOOR_DB - 1)
        check("噪声底噪过低应抛 ValueError", False)
    except ValueError:
        check("噪声底噪过低应抛 ValueError", True)

    try:
        denoise_array(np.zeros((2, 10)), RATE)
        check("二维输入应抛 ValueError", False)
    except ValueError:
        check("二维输入应抛 ValueError", True)

    try:
        denoise_array(signal, 0)
        check("采样率 0 应抛 ValueError", False)
    except ValueError:
        check("采样率 0 应抛 ValueError", True)

    empty = denoise_array(np.zeros(0), RATE)
    check("空音频不崩溃且原样返回", empty.size == 0)

print()
if skipped:
    print("RESULT: SKIPPED（缺 ffmpeg，未计入失败）")
    raise SystemExit(0)
print(f"RESULT: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
