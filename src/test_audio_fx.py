# -*- coding: utf-8 -*-
"""audio_fx 测试：增益 / 峰值标准化 / 淡入淡出 / 信息统计。

覆盖点：

* 纯函数性：入参数组不被修改（不变量 1 的模块级保证）；
* 增益换算正确（±6 dB 约等于 ×2 / ×0.5）；
* 峰值标准化后峰值精确落在目标电平；
* 淡入首样本为 0、淡出末样本为 0，中段不受影响；
* 淡入 + 淡出超长时等比压缩且不重叠；
* 参数校验（越界 / 类型错误）抛异常而不是静默忽略；
* 统计指标口径（峰值、RMS、静音占比、削顶计数）。
"""
import json
import math

import numpy as np
from audio_fx import (
    DEFAULT_TARGET_DBFS,
    MAX_FADE_SECONDS,
    MAX_GAIN_DB,
    AudioParams,
    apply,
    apply_fade,
    apply_gain,
    measure,
    peak_normalize,
)

passed = 0
failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"PASS: {name}")
    else:
        failed += 1
        print(f"FAIL: {name}")


def sine(freq=440.0, seconds=1.0, rate=16000, amp=0.5):
    t = np.arange(int(seconds * rate), dtype=np.float64) / rate
    return amp * np.sin(2.0 * np.pi * freq * t)


# ----------------------------------------------------------------------
# 增益
# ----------------------------------------------------------------------
base = sine(amp=0.25)
original = base.copy()

gained = apply_gain(base, 6.0)
check("增益 +6dB 约为 ×1.995", abs(float(np.max(np.abs(gained))) - 0.25 * 10 ** (6 / 20)) < 1e-9)
check("增益不修改入参", np.array_equal(base, original))

minus = apply_gain(base, -6.0)
check("增益 -6dB 约为 ×0.501", abs(float(np.max(np.abs(minus))) - 0.25 * 10 ** (-6 / 20)) < 1e-9)

zero = apply_gain(base, 0.0)
check("增益 0dB 返回等值副本但非同一对象", np.array_equal(zero, base) and zero is not base)
check("增益结果 dtype 为 float64", apply_gain(np.zeros(4, dtype=np.int16), 3.0).dtype == np.float64)

try:
    apply_gain(base, MAX_GAIN_DB + 1)
    check("增益越界应抛 ValueError", False)
except ValueError:
    check("增益越界应抛 ValueError", True)

try:
    apply_gain(np.zeros((2, 2)), 1.0)
    check("二维输入应抛 ValueError", False)
except ValueError:
    check("二维输入应抛 ValueError", True)

# ----------------------------------------------------------------------
# 峰值标准化
# ----------------------------------------------------------------------
quiet = sine(amp=0.05)
normalized = peak_normalize(quiet, -1.0)
peak_after = float(np.max(np.abs(normalized)))
check("标准化后峰值 == -1 dBFS", abs(peak_after - 10 ** (-1 / 20)) < 1e-9)
check("标准化不修改入参", abs(float(np.max(np.abs(quiet))) - 0.05) < 1e-12)

loud = sine(amp=0.9)
normalized_loud = peak_normalize(loud, -3.0)
check("标准化对更响的音频同样生效（衰减）", abs(float(np.max(np.abs(normalized_loud))) - 10 ** (-3 / 20)) < 1e-9)

silence = np.zeros(100, dtype=np.float64)
check("全静音标准化不放大（保持全零）", float(np.max(np.abs(peak_normalize(silence)))) == 0.0)

try:
    peak_normalize(quiet, 0.0)
    check("目标电平 >= 0 应抛 ValueError", False)
except ValueError:
    check("目标电平 >= 0 应抛 ValueError", True)

# ----------------------------------------------------------------------
# 淡入淡出
# ----------------------------------------------------------------------
rate = 16000
tone = sine(seconds=1.0, rate=rate, amp=0.8)
faded = apply_fade(tone, rate, fade_in=0.1, fade_out=0.1)
in_samples = int(round(0.1 * rate))
check("淡入首样本为 0", abs(float(faded[0])) < 1e-12)
check("淡出末样本为 0", abs(float(faded[-1])) < 1e-12)
check("淡入结束处恢复到原幅度", abs(float(faded[in_samples])) - abs(float(tone[in_samples])) < 1e-9)
mid = slice(in_samples + 10, len(tone) - in_samples - 10)
check("中段不受淡入淡出影响", np.allclose(faded[mid], tone[mid]))
check("淡入淡出不修改入参", np.array_equal(tone, sine(seconds=1.0, rate=rate, amp=0.8)))

short = sine(seconds=0.1, rate=rate, amp=0.8)
overlapping = apply_fade(short, rate, fade_in=0.5, fade_out=0.5)
check("淡入淡出总长超音频时仍输出等长数组", overlapping.size == short.size)
check("超长淡入淡出在中间不产生 NaN", bool(np.all(np.isfinite(overlapping))))

try:
    apply_fade(tone, rate, fade_in=-0.1)
    check("负淡入时长应抛 ValueError", False)
except ValueError:
    check("负淡入时长应抛 ValueError", True)

try:
    apply_fade(tone, rate, fade_in=MAX_FADE_SECONDS + 1)
    check("淡入超上限应抛 ValueError", False)
except ValueError:
    check("淡入超上限应抛 ValueError", True)

# ----------------------------------------------------------------------
# 参数对象与组合处理
# ----------------------------------------------------------------------
default_params = AudioParams()
check("默认参数 is_noop 为真", default_params.is_noop())

params = AudioParams.from_mapping({"gain_db": 3.0, "fade_in": 0.05})
check("from_mapping 读取数值字段", params.gain_db == 3.0 and params.fade_in == 0.05)
check("from_mapping 缺省字段用默认值", params.normalize is False and params.target_dbfs == DEFAULT_TARGET_DBFS)
check("to_dict 可直接 JSON 序列化", isinstance(params.to_dict(), dict) and params.to_dict()["gain_db"] == 3.0)
check("非 noop 参数判定正确", params.is_noop() is False)

try:
    AudioParams.from_mapping({"normalize": "false"})
    check("normalize 传字符串应抛 ValueError", False)
except ValueError:
    check("normalize 传字符串应抛 ValueError", True)

try:
    AudioParams.from_mapping({"gain_db": 99})
    check("gain_db 越界应抛 ValueError", False)
except ValueError:
    check("gain_db 越界应抛 ValueError", True)

# ---- 降噪参数的校验（不依赖 ffmpeg，只测参数层）----
denoise_params = AudioParams.from_mapping({"denoise": True, "denoise_db": -45})
check("denoise 字段可读取", denoise_params.denoise is True and denoise_params.denoise_db == -45)
check("开启降噪后 is_noop 为假", denoise_params.is_noop() is False)
check("默认不降噪", AudioParams().denoise is False)

try:
    AudioParams.from_mapping({"denoise": "yes"})
    check("denoise 传字符串应抛 ValueError", False)
except ValueError:
    check("denoise 传字符串应抛 ValueError", True)

try:
    AudioParams.from_mapping({"denoise_db": 0})
    check("denoise_db 越界应抛 ValueError", False)
except ValueError:
    check("denoise_db 越界应抛 ValueError", True)

# 降噪走 ffmpeg，这里只验证"关掉降噪时不会触碰外部进程"
no_fx = apply(tone, rate, AudioParams(denoise=True), enable_denoise=False)
check("enable_denoise=False 时跳过降噪且不报错", no_fx.size == tone.size)

combined = apply(tone, rate, AudioParams(gain_db=-6.0, normalize=True, fade_in=0.1, fade_out=0.1))
check("组合处理不修改入参", np.array_equal(tone, sine(seconds=1.0, rate=rate, amp=0.8)))
check("组合处理后峰值仍为目标电平（淡入淡出区之外）", abs(float(np.max(np.abs(combined))) - 10 ** (-1 / 20)) < 1e-6)
check("组合处理淡入起点为 0", abs(float(combined[0])) < 1e-12)
check("apply 传 None 返回等值副本", np.array_equal(apply(tone, rate, None), tone))

# ----------------------------------------------------------------------
# 音频信息统计
# ----------------------------------------------------------------------
stats = measure(tone, rate)
check("统计时长正确", abs(stats.duration - 1.0) < 1e-9)
check("统计样本数正确", stats.sample_count == rate)
check("统计峰值约 -1.94 dBFS (0.8)", abs(stats.peak_dbfs - 20 * math.log10(0.8)) < 0.01)
check("统计 RMS 约 -4.95 dBFS", abs(stats.rms_dbfs - (20 * math.log10(0.8 / math.sqrt(2)))) < 0.01)
check("波峰因数约 3.01 dB", abs(stats.crest_factor_db - 3.01) < 0.02)
check("正弦无静音帧", stats.silence_ratio == 0.0)
check("正弦无削顶", stats.clipped_samples == 0)

half_silent = np.concatenate([np.zeros(rate // 2), sine(seconds=0.5, rate=rate, amp=0.5)])
half_stats = measure(half_silent, rate)
check("前半静音时静音占比约 0.5", abs(half_stats.silence_ratio - 0.5) < 0.05)

clipped = np.ones(100, dtype=np.float64)
check("满幅样本计为削顶", measure(clipped, rate).clipped_samples == 100)

empty = measure(np.zeros(0), rate)
check("空音频统计不崩溃且时长为 0", empty.duration == 0.0 and empty.silence_ratio == 1.0)

empty_dict = empty.to_dict()
check("全静音的 -inf 在 to_dict 后变成 None（JSON 可序列化）", empty_dict["peak_dbfs"] is None and empty_dict["rms_dbfs"] is None)

silent_dict = measure(np.zeros(rate), rate).to_dict()
check("全零音频同样输出 None 而不是 -inf", silent_dict["peak_dbfs"] is None)
check("to_dict 结果可被严格 JSON 序列化", json.dumps(empty_dict, allow_nan=False) != "")

print(f"\nRESULT: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
