# -*- coding: utf-8 -*-
"""segmenter 测试"""
import sys
sys.path.insert(0, r'E:\WordCut\src')
from model import Token
from segmenter import RuleSegmenter, segment_tokens, Segmenter

passed=0; failed=0
def check(n,c):
    global passed,failed
    if c: passed+=1; print(f'PASS: {n}')
    else: failed+=1; print(f'FAIL: {n}')

def tk(i, text, start, end):
    return Token(id=f't{i:05d}', text=text, start=start, end=end)

# 1. 空输入
check('空输入返回空', segment_tokens([]) == [])

# 2. 句末标点断段
tokens1 = [tk(0,'今天','0.0','0.0') if False else tk(0,'今天',0.0,0.3), tk(1,'天气',0.3,0.6), tk(2,'很好。',0.6,1.0), tk(3,'我们',1.1,1.4), tk(4,'出发',1.4,1.7), tk(5,'吧。',1.7,2.0)]
segs1 = segment_tokens(tokens1)
check(f'句末标点断成2段 (实际{len(segs1)})', len(segs1)==2)
check(f'第1段文本 ({segs1[0].text})', segs1[0].text=='今天天气很好。')
check(f'第2段文本 ({segs1[1].text})', segs1[1].text=='我们出发吧。')

# 3. 时间间隔断段
tokens2 = [tk(0,'第一句',0.0,0.5), tk(1,'第二句',2.0,2.5)]
segs2 = segment_tokens(tokens2)
check(f'间隔2s断成2段 (实际{len(segs2)})', len(segs2)==2)

# 4. 间隔小于阈值不断段
tokens3 = [tk(0,'连续',0.0,0.5), tk(1,'说话',0.6,1.0)]
segs3 = segment_tokens(tokens3)
check(f'间隔0.1s不断段 (实际{len(segs3)})', len(segs3)==1)

# 5. 硬上限强制断段
seg_hard = RuleSegmenter(hard_max_seconds=2.0)
tokens4 = [tk(i, f'词{i}', i*0.5, i*0.5+0.5) for i in range(10)]
segs4 = segment_tokens(tokens4, seg_hard)
check(f'硬上限2s切出多段 (实际{len(segs4)}段)', len(segs4)>=3)

# 6. 标点带引号
tokens5 = [tk(0,'他说',0.0,0.3), tk(1,'好。”',0.3,0.6), tk(2,'然后',0.7,1.0)]
segs5 = segment_tokens(tokens5)
check(f'引号后标点仍断段 (实际{len(segs5)}段)', len(segs5)==2)

# 7. Segment 字段完整性
s = segs1[0]
check('Segment 有 index', s.index==0)
check('Segment 有 start/end', s.start==0.0 and s.end==1.0)
check('Segment 有 token_ids', len(s.token_ids)==3)
check('Segment.duration', abs(s.duration-1.0)<1e-9)
check('to_dict 可序列化', isinstance(s.to_dict(), dict))

# 8. 实现 Protocol
check('RuleSegmenter 满足 Protocol', isinstance(RuleSegmenter(), Segmenter))

# 9. 空文本 token 被过滤
tokens6 = [tk(0,'','0.0','0.0') if False else tk(0,'   ',0.0,0.1), tk(1,'有效',0.1,0.5)]
segs6 = segment_tokens(tokens6)
check(f'空白token被过滤 (实际{len(segs6)}段)', len(segs6)==1 and segs6[0].text=='有效')

print(); print(f'结果: {passed} 通过, {failed} 失败')
sys.exit(1 if failed else 0)