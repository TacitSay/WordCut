"""model.py 不变量测试"""
from model import Token, DeleteSpan, EDL, TimeMap

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

# 1. 时间戳校验
try:
    Token(id='t1', text='字', start=1.0, end=0.5)
    check("start<end 校验", False)
except ValueError:
    check("start<end 校验", True)

try:
    Token(id='t1', text='字', start=-1.0, end=0.5)
    check("start>=0 校验", False)
except ValueError:
    check("start>=0 校验", True)

# 2. Token.id 不可变
t = Token(id='t1', text='字', start=0.0, end=0.2)
try:
    t.id = 't2'
    check("id 不可变", False)
except Exception:
    check("id 不可变", True)

# 3. TimeMap 往返
tm = TimeMap.from_deletes([DeleteSpan(2.0, 3.0)])
err = max(abs(tm.target_to_source(tm.source_to_target(x)) - x) for x in [i*0.1 for i in range(100)])
check(f"TimeMap 往返误差<1e-6 (实际{err:.2e})", err < 1e-6)

# 4. EDL 合并删除
edl = EDL(source='a.wav', sample_rate=16000)
edl.add_delete(1.0, 2.0, cause='word')
edl.add_delete(1.5, 2.5, cause='pause')
merged = edl.merged_deletes
check("EDL 合并重叠删除", len(merged) == 1 and merged[0].start == 1.0 and merged[0].end == 2.5)

# 5. cause 校验
try:
    DeleteSpan(1.0, 2.0, cause='invalid')
    check("cause 校验", False)
except ValueError:
    check("cause 校验", True)

# 6. TimeMap 无删除=恒等
tm2 = TimeMap.from_deletes([])
check("TimeMap 恒等映射", tm2.source_to_target(5.0) == 5.0)

print(f"\n结果: {passed} 通过, {failed} 失败")
exit(1 if failed else 0)
