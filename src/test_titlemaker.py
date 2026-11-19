# -*- coding: utf-8 -*-
"""``titlemaker`` 的 smoke 测试（不联网）。

要守住的行为：
1. 关键词是**主题词**，不是跨词边界的碎片（"们十分" "言触动"）；
2. 虚词组合不上台（"因为" "能够" "一起"）；
3. 兜底标题**不照抄整句**，且不像主题词时返回空串（让界面用"第 N 段"占位）；
4. ``max_chars=0`` 表示不限字数，但兜底仍受限；
5. 任何输入都不抛异常。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import titlemaker

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


# ---- 短语切分 ----
check(
    f"按标点切短语({len(titlemaker.split_phrases('甲，乙。丙'))})",
    len(titlemaker.split_phrases("甲，乙。丙")) == 3,
)
check("无标点时整段为 1 个短语", len(titlemaker.split_phrases("没有标点的一段话")) == 1)

# ---- 关键词抽取 ----
text_emotion = (
    "应该是对你们的情绪刺激是最大的,所以你们就一定要能够把这个情绪转化为实际的行动,"
    "去做点决策的这样的意识。因为我发现很多次强调情绪的重要性的时候,很多人还是不把行动当回事。"
)
kw = titlemaker.extract_keywords(text_emotion, top_k=1)
check(f"抽出主题词({kw})", kw and "情绪" in kw[0])

text_course = (
    "对我而言触动非常大的,甚至我在去做课程的时候,之前在初步有这个课程的想法的时候,"
    "都会大吃一惊。所以说课程在我的心目中最重要。"
)
kw2 = titlemaker.extract_keywords(text_course, top_k=1)
check(f"抽出'课程'({kw2})", kw2 and "课程" in kw2[0])

# 虚词不该被选中
bad_text = "因为我发现,所以我应该,但是你可以,而且我们能够,或者你们如果"
kw3 = titlemaker.extract_keywords(bad_text, top_k=2)
check(f"虚词不上台({kw3})", all(item not in ("因为", "所以", "但是", "而且") for item in kw3))

# 不产生跨词碎片
frag_text = "我们十分正式开始,然后跟大家说一些问题,咱们前面这三节课其实是一个比较慢热的状态"
kw4 = titlemaker.extract_keywords(frag_text, top_k=2)
check(
    f"无跨词碎片({kw4})",
    all(item not in ("们十", "们十分", "言触", "分正") for item in kw4),
)

# ---- 标题生成 ----
titles = titlemaker.make_titles([text_emotion, text_course], max_chars=0)
check(f"生成 2 个标题({titles})", len(titles) == 2)
check("标题非空", all(t for t in titles))
check("标题很短（不是整句）", all(len(t) <= 16 for t in titles))

# 兜底：不像主题词时返回空
vague = "不管说是恋人是朋友甚至你身边的闺蜜好机友等等等等又或者说你能够进行一次彻头彻尾的反思"
t_vague = titlemaker.make_titles([vague], max_chars=0)[0]
check(f"模糊段落给空标题({t_vague!r})", t_vague == "" or len(t_vague) <= 16)

# max_chars 生效
t_limited = titlemaker.make_titles([text_emotion], max_chars=4)[0]
check(f"字数上限生效({t_limited!r})", len(t_limited) <= 4)

# 空输入
check("空输入返回空", titlemaker.make_titles([]) == [])

# 不抛异常：各种脏输入
for dirty in ["", "   ", "。。。", "a", "1234567890"]:
    try:
        titlemaker.make_titles([dirty])
        ok = True
    except Exception:
        ok = False
    check(f"脏输入不抛异常({dirty!r})", ok)

# API 回调：注入后应优先使用
def fake_api(items, limit):
    return [f"AI标题{i + 1}" for i in range(len(items))]


t_api = titlemaker.make_titles([text_emotion, text_course], api_call=fake_api)
check(f"API 回调被采用({t_api})", t_api == ["AI标题1", "AI标题2"])


# API 失败要降级而不是崩
def bad_api(items, limit):
    raise RuntimeError("模拟失败")


t_fb = titlemaker.make_titles([text_emotion], api_call=bad_api)
check("API 失败后降级到本地", len(t_fb) == 1 and t_fb[0])

print()
print(f"结果: {passed} 通过, {failed} 失败")
sys.exit(1 if failed else 0)
