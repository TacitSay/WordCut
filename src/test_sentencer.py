# -*- coding: utf-8 -*-
"""``sentencer`` + ``punctuator`` 的 smoke 测试（不联网、不加载模型）。

覆盖：
1. 标点恢复：只改文本不改时间戳、已有标点不重复加、结束补句号；
2. 断句：标点优先、停顿次之、长句强制拆、**不产出碎片**（最小句长约束）；
3. 句子结构：token 覆盖完整、时间单调、id 不重复。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import punctuator
import sentencer
from model import Token

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


def make_words(specs):
    """specs: [(text, start, end), ...] -> 词字典列表"""
    return [
        {"word": text, "start": start, "end": end, "seg": 0, "dur": end - start, "prob": 0.9}
        for text, start, end in specs
    ]


# ----------------------------------------------------------------------
# 1. 标点恢复
# ----------------------------------------------------------------------
words = make_words(
    [
        ("今天", 0.0, 0.3),
        ("我们", 0.3, 0.6),
        ("聊聊", 0.6, 0.9),
        ("剪辑", 0.9, 1.4),  # 后面停顿 0.5s → 句号
        ("然后", 1.9, 2.2),
        ("再说", 2.2, 2.5),
    ]
)
restored = punctuator.restore_punctuation(words)

check("返回等长", len(restored) == len(words))
check(
    "时间戳未被修改",
    all(a["start"] == b["start"] and a["end"] == b["end"] for a, b in zip(words, restored)),
)
check("原列表未被就地修改", words[3]["word"] == "剪辑")

long_gap_idx = 3
check(
    f"长停顿后补句号({restored[long_gap_idx]['word']!r})",
    str(restored[long_gap_idx]["word"]).endswith("。"),
)
check(
    f"结尾补句号({restored[-1]['word']!r})",
    str(restored[-1]["word"]).endswith("。"),
)
check(
    "有标点的词不再重复加",
    sum(1 for w in restored if str(w["word"]).endswith(("。。", "，，"))) == 0,
)


# 已带标点：保持原样
with_punct = make_words([("你好。", 0.0, 0.5), ("再见", 1.5, 2.0)])
r2 = punctuator.restore_punctuation(with_punct)
check("已有句号保持原样", r2[0]["word"] == "你好。")

# 统计函数
stat = punctuator.restore_with_prompt_check(restored)
check(f"统计带标点数({stat['with_punct']}/{stat['total']})", stat["with_punct"] >= 2)


# ----------------------------------------------------------------------
# 2. 断句
# ----------------------------------------------------------------------
def tokens_of(words_list):
    return [
        Token(
            id=f"t{i:05d}",
            text=w["word"],
            start=w["start"],
            end=w["end"],
            is_deleted=False,
            confidence=1.0,
        )
        for i, w in enumerate(words_list)
    ]


# 2a. 标点优先断句
punct_words = make_words(
    [
        ("第一句。", 0.0, 1.0),
        ("第二句。", 1.0, 2.0),
        ("第三句", 2.0, 3.0),
    ]
)
s = sentencer.build_sentences(tokens_of(punct_words))
check(f"按标点分成 3 句({len(s)})", len(s) == 3)
check("句子文本正确", s[0].text == "第一句。" and s[1].text == "第二句。")

# 2b. 停顿断句（无标点）
gap_words = make_words(
    [
        ("你好", 0.0, 0.4),
        ("我是", 0.4, 0.8),
        ("小明", 0.8, 1.2),
        # 停顿 0.6s > 0.32 → 断句
        ("今天", 1.8, 2.2),
        ("天气", 2.2, 2.6),
        ("不错", 2.6, 3.0),
    ]
)
s2 = sentencer.build_sentences(tokens_of(gap_words))
check(f"按停顿分成 2 句({len(s2)})", len(s2) == 2)
check("第一句含 3 词", len(s2[0].tokens) == 3)

# 2c. 最小句长：不允许切出碎片
many_single = make_words(
    [(f"字{i}", i * 0.15, i * 0.15 + 0.12) for i in range(30)]
)
s3 = sentencer.build_sentences(tokens_of(many_single))
lengths = [len(x.text) for x in s3]
check(f"无 1-2 字碎片(最短 {min(lengths)})", min(lengths) >= sentencer.MIN_SENTENCE_CHARS)

# 2d. 超长句会被拆
long_words = make_words(
    [(f"词{i}", i * 0.3, i * 0.3 + 0.28) for i in range(60)]
)
s4 = sentencer.build_sentences(tokens_of(long_words))
check(f"长句被拆成多句({len(s4)})", len(s4) > 1)
check(
    f"没有超长句(最长 {max(len(x.text) for x in s4)} 字)",
    max(len(x.text) for x in s4) <= sentencer.DEFAULT_MAX_CHARS + 10,
)

# 2e. 句子覆盖全部 token，时间单调
all_tokens = tokens_of(punct_words + gap_words)
s5 = sentencer.build_sentences(all_tokens)
covered = [tid for sentence in s5 for tid in sentence.token_ids]
check("token 全覆盖", len(covered) == len(all_tokens))
check("句子按时间有序", all(a.start <= b.start for a, b in zip(s5, s5[1:])))

# 2f. 空输入
check("空输入返回空", sentencer.build_sentences([]) == [])
blank = [
    Token(id="t0", text="  ", start=0.0, end=1.0, is_deleted=False, confidence=1.0)
]
check("全空白返回空", sentencer.build_sentences(blank) == [])

# 2g. Sentence 序列化
d = s5[0].to_dict() if s5 else {}
check("to_dict 含必需字段", all(k in d for k in ("index", "start", "end", "text", "token_ids")))

# 2h. find_sentence_index
if s5:
    first_id = s5[0].token_ids[0]
    check("能用 token id 定位句子", sentencer.find_sentence_index(s5, first_id) == 0)

print()
print(f"结果: {passed} 通过, {failed} 失败")
sys.exit(1 if failed else 0)
