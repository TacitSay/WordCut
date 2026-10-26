"""cut_refine.py 冒烟测试"""
import numpy as np
from cut_refine import find_silence_valley, find_zero_crossing, crossfade

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

sr = 16000

# 1. find_silence_valley: 有静音谷的音频
t = np.arange(int(1.0 * sr)) / sr
sig = np.zeros(int(1.0 * sr), dtype=np.float32)
sig[:int(0.4*sr)] = 0.5 * np.sin(2*np.pi*440*t[:int(0.4*sr)])
sig[int(0.5*sr):] = 0.5 * np.sin(2*np.pi*440*t[int(0.5*sr):])
valley = find_silence_valley(sig, 0.45, sr, window_ms=80)
check(f"谷底定位在静音区(实际{valley:.3f}s)", 0.40 <= valley <= 0.50)

# 2. find_zero_crossing: 验证返回的是真过零点(符号变化)
sine = np.sin(2*np.pi*440*np.arange(100)/sr)
zc = find_zero_crossing(sine, 0, max_offset=20)
is_zc = (sine[zc-1] < 0 < sine[zc]) or (sine[zc-1] > 0 > sine[zc])
check(f"返回的是真过零点(实际{zc})", is_zc)

# 3. crossfade 长度
a = np.ones(1000, dtype=np.float32)
b = np.ones(1000, dtype=np.float32) * 0.5
fade = 480
out = crossfade(a, b, fade)
check(f"crossfade 长度正确(实际{len(out)})", len(out) == 1000 + 1000 - fade)

# 4. crossfade 空输入
check("crossfade 空输入", len(crossfade(np.array([]), b, fade)) == len(b))

# 5. 谷底越界保护(窗口超出音频)
valley2 = find_silence_valley(sig, 0.01, sr, window_ms=200)
check(f"谷底越界保护(实际{valley2:.3f}s, 应>=0)", valley2 >= 0.0)

print(f"\n结果: {passed} 通过, {failed} 失败")
exit(1 if failed else 0)
