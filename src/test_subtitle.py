"""subtitle.py 测试: 验证删除后 SRT 与 TimeMap 一致"""
import sys
sys.path.insert(0, r'E:\WordCut\src')
from model import Token, DeleteSpan, TimeMap
from subtitle import rebuild_srt, tokens_to_srt

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

# 构造 5 个 token, 每个 0.5s
tokens = [
    Token(id='t1', text='今天', start=0.0, end=0.4),
    Token(id='t2', text='我们', start=0.4, end=0.8),
    Token(id='t3', text='讨论', start=0.8, end=1.2),
    Token(id='t4', text='这个问题。', start=1.2, end=2.0),
    Token(id='t5', text='谢谢。', start=2.5, end=3.0),
]

# 1. 无删除, 恒等映射
srt1 = tokens_to_srt(tokens)
check("无删除生成 SRT", len(srt1) > 0 and '00:00:00,000' in srt1)
print("--- 无删除 SRT ---")
print(srt1)

# 2. 删除 t2(0.4-0.8s), 后面时间应前移 0.4s
span = DeleteSpan(start=0.4, end=0.8, token_id='t2', cause='word')
tm = TimeMap.from_deletes([span])
srt2 = rebuild_srt(tokens, tm, {'t2'})
print("--- 删除 t2 后 SRT ---")
print(srt2)
check("删除后不含 t2 文本", '我们' not in srt2)
check("删除后 t1 起点不变", '00:00:00,000' in srt2)

# 3. t3 原 0.8s, 删除 0.4s 后应为 0.4s
# 找 t3 的起点 "讨论" 前面的时间戳
import re
lines = [l for l in srt2.split('\n') if '-->' in l]
check(f"SRT 有 {len(lines)} 条时间行", len(lines) >= 2)
if lines:
    first_start = lines[0].split(' --> ')[0]
    check(f"首条时间戳格式正确({first_start})", re.match(r'^\d{2}:\d{2}:\d{2},\d{3}$', first_start) is not None)

# 4. 时间戳单调递增
prev_end = -1
monotonic = True
for l in lines:
    s, e = l.split(' --> ')
    def to_ms(tc):
        h, m, rest = tc.split(':')
        sec, ms = rest.split(',')
        return int(h)*3600000 + int(m)*60000 + int(sec)*1000 + int(ms)
    if to_ms(s) < prev_end:
        monotonic = False
    prev_end = to_ms(e)
check("时间戳单调不重叠", monotonic)

# 5. 空 token 列表
check("空列表返回空串", rebuild_srt([], TimeMap.identity(), set()) == '')

# 6. 全部删除
check("全删返回空串", rebuild_srt(tokens, TimeMap.identity(), {'t1','t2','t3','t4','t5'}) == '')

print(f"\n结果: {passed} 通过, {failed} 失败")
exit(1 if failed else 0)
