"""edl_render.py 冒烟测试"""
import numpy as np
from model import DeleteSpan, EDL
from edl_render import compute_keep_segments, render, render_edl

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
rng = np.random.RandomState(0)
sig = rng.randn(sr).astype(np.float32)
before = sig.copy()

# 1. 合并重叠删除 + 取补集
d = [DeleteSpan(1.5, 2.5), DeleteSpan(1.0, 2.0), DeleteSpan(2.4, 3.0), DeleteSpan(9.0, 20.0)]
check("合并重叠后取补集", compute_keep_segments(d, 10.0) == [(0.0, 1.0), (3.0, 9.0)])
check("无删除即整段保留", compute_keep_segments([], 10.0) == [(0.0, 10.0)])
check("全部删除返回空", compute_keep_segments([DeleteSpan(0.0, 10.0)], 10.0) == [])
check("零时长返回空", compute_keep_segments([], 0.0) == [])
check("越界删除被裁剪", compute_keep_segments([DeleteSpan(5.0, 20.0)], 1.0) == [(0.0, 1.0)])

# 2. 渲染长度 = 源长 - 删除时长 + 每个剪辑点因交叉淡化缩短的样本数
out = render(sig, sr, [DeleteSpan(0.25, 0.5)], fade_samples=480)
check(f"渲染长度正确(实际{len(out)})", len(out) == sr - int(0.25 * sr) - 480)
check("输出 float32", out.dtype == np.float32)
check("源样本未被修改", np.array_equal(sig, before))
check("输出不共享源内存", not np.shares_memory(out, sig))

# 3. 无删除 -> 逐样本等于源
check("无删除逐样本一致", np.array_equal(render(sig, sr, []), sig))

# 4. 连续片段（1 样本删除）应硬接，不产生淡化/不缩短
one = render(sig, sr, [DeleteSpan(0.1, 0.1 + 1.0 / sr)], fade_samples=480)
check(f"连续片段硬接(实际{len(one)})", len(one) == sr - 1)

# 5. fade_samples=0 硬切
check("硬切长度正确",
      len(render(sig, sr, [DeleteSpan(0.25, 0.5)], fade_samples=0)) == sr - int(0.25 * sr))

# 6. 全部删光 -> 等长静音
all_deleted = render(sig, sr, [DeleteSpan(0.0, 1.0)])
check("全删输出等长静音", len(all_deleted) == sr and not np.any(all_deleted))

# 7. 空输入
check("空输入输出空", len(render(np.zeros(0, np.float32), sr, [DeleteSpan(0.1, 0.2)])) == 0)

# 8. render_edl 与 render 一致，且可复现
edl = EDL(source="a.wav", sample_rate=sr,
          deletes=[DeleteSpan(0.25, 0.5), DeleteSpan(0.7, 0.8)])
r1 = render_edl(sig, sr, edl)
check("render_edl == render", np.array_equal(r1, render(sig, sr, edl.deletes)))
check("渲染可复现", np.array_equal(r1, render_edl(sig, sr, edl)))

# 9. 采样率不一致要报错
try:
    render_edl(sig, 48000, edl)
    check("采样率不一致报错", False)
except ValueError:
    check("采样率不一致报错", True)

# 10. 非法输入
for label, fn in [
    ("非一维输入", lambda: render(sig.reshape(2, -1), sr, [])),
    ("采样率非正", lambda: render(sig, 0, [])),
    ("fade 为负", lambda: render(sig, sr, [], fade_samples=-1)),
    ("时长为负", lambda: compute_keep_segments([], -1.0)),
    ("edl 类型错误", lambda: render_edl(sig, sr, "not-edl")),
]:
    try:
        fn()
        check(f"{label}报错", False)
    except (ValueError, TypeError):
        check(f"{label}报错", True)

print(f"\n结果: {passed} 通过, {failed} 失败")
exit(1 if failed else 0)
