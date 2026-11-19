# -*- coding: utf-8 -*-
"""段落标题生成：从"摘一句"改为"概括"。

用户反馈：标题过于频繁、不是总结。两个根因：

1. **段落太小**（180 字就切段）→ 段多标题多 → 已把目标调到 800 字；
2. **标题照抄首句**，只是截断 → 读起来不像标题。

实现要点：

* 不做字级 n-gram 滑窗——那会产出"们十分""言触动"这种跨词边界的碎片；
* 改为**以标点切出的短语为候选单位**，短语天然是语义单元；
* 关键词候选按"出现次数 × 长度"排序，并用虚词表过滤；
* 兜底只取第一个短语，且压到很短，绝不整句照抄。

所有路径只产出标题文本，不改段落内容。
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence

__all__ = ["make_titles", "extract_keywords", "split_phrases"]

#: 口语填充词：标题里出现会显得啰嗦。
_FILLERS = (
    "就是说",
    "所以说",
    "我觉得",
    "反正",
    "其实",
    "然后",
    "那么",
    "这个",
    "那个",
    "就是",
    "的话",
    "的时候",
    "啊",
    "呀",
    "嘛",
    "吧",
    "呢",
    "哦",
    "嗯",
)

#: 停用短语：作为候选直接排除。
_STOP_PHRASES = frozenset(
    """
    我们 你们 他们 咱们 大家 自己 什么 怎么 这样 那样 这些 那些
    可以 可能 应该 需要 非常 特别 比较 十分 确实 真的 还是 虽然
    就算 再次 很多 一些 一点 一下 之后 之前 事情 东西 时候 地方
    因为 所以 但是 而且 如果 然后 就是 这个 那个 这样 那样 能够
    等等 甚至 不管 或者 除非 一种 一次 一个 咱们 跟你 跟我 跟他
    个人 方式 情况 方面 问题 内容 部分 意思 感觉 状态 因素
    """.split()
)

#: 短语不宜以这些字开头（虚词/代词）。
_WEAK_HEADS = frozenset("的了是在和与或但而就也都很把被让给对从到这那其之我你他她它")

#: 短语不宜以这些字结尾。
_WEAK_TAILS = frozenset("的了着过们是在和与或但而就也都很把被让给对从到个些样么啊呀嘛吧呢哦嗯")


#: 这些字出现在双字里时基本是助动词/趋向动词，不构成主题词。
_WEAK_VERBS = frozenset("够去来了着过想要会能可需要去做说得看给让把被和与")

#: 双字候选的黑名单：这些组合即使高频也没有主题信息。
_BAD_BIGRAMS = frozenset(
    """
    一起 前那 前几 前面 后面 里面 上面 下面 时候 这样 那样 什么 怎么
    我们 你们 他们 咱们 自己 大家 一个 一些 一点 一下 一直 一定 一样
    可以 可能 应该 需要 非常 特别 比较 十分 确实 真的 还是 虽然 就是
    因为 所以 但是 而且 如果 然后 或者 除非 不管 甚至 能够 等等 一种
    一次 这个 那个 这些 那些 东西 事情 地方 方面 情况 问题 部分 意思
    感觉 状态 因素 方式 个人 跟你 跟我 跟他 对你 对我 对他 在你 在我
    """.split()
)


def split_phrases(text: str) -> List[str]:
    """按标点把段落切成短语（语义单元）。

    转写文本的标点是 ``，。！？；…、：`` 与英文对应符号。没有标点时
    退化为整段作为一个短语。

    参数:
        text: 段落全文。

    返回:
        短语列表（已去空白，保留原顺序）。
    """
    parts = re.split(r"[。！？；…，,、：:\n]+", text)
    return [part.strip() for part in parts if part.strip()]


def _strip_fillers(text: str) -> str:
    """去掉口语填充词与残留标点。"""
    cleaned = text
    for filler in _FILLERS:
        cleaned = cleaned.replace(filler, "")
    for punct in "。！？；…，,、：:（）()【】[]“”\"'·":
        cleaned = cleaned.replace(punct, "")
    return " ".join(cleaned.split())


def extract_keywords(text: str, top_k: int = 2) -> List[str]:
    """从段落里挑出关键词短语。

    做法：以标点切出的短语为候选，剔除停用短语与虚词边界，再按
    "出现次数优先、长度次之"排序。同一主题的短语常会反复出现
    （"情绪"在讲情绪的段落里出现很多次），这是最可靠的信号。

    参数:
        text: 段落全文。
        top_k: 取几个关键词。

    返回:
        关键词短语列表（按重要度降序，互不包含）。
    """
    phrases = split_phrases(text)
    counts: Dict[str, int] = {}

    for phrase in phrases:
        cleaned = _strip_fillers(phrase)
        if not cleaned:
            continue
        # 过长的短语不是"词"，截取前 8 字作为候选
        candidate = cleaned[:10]
        if len(candidate) < 2 or candidate in _STOP_PHRASES:
            continue
        counts[candidate] = counts.get(candidate, 0) + 1

    # 也统计"短语里出现的双字组合"，但只在短语内部切，不跨短语滑窗
    for phrase in phrases:
        cleaned = _strip_fillers(phrase)
        if len(cleaned) < 4:
            continue
        for index in range(len(cleaned) - 1):
            bigram = cleaned[index : index + 2]
            if bigram in _STOP_PHRASES or bigram in _BAD_BIGRAMS:
                continue
            if bigram[0] in _WEAK_HEADS or bigram[1] in _WEAK_TAILS:
                continue
            # 动词/助词搭配（"够去" "能够"）不是主题词
            if bigram[0] in _WEAK_VERBS or bigram[1] in _WEAK_VERBS:
                continue
            counts[bigram] = counts.get(bigram, 0) + 1

    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], -len(kv[0])))
    picked: List[str] = []
    for phrase, count in ordered:
        # 只接受重复出现的短语：一次出现的多半是偶然搭配
        if count < 2:
            break
        if any(phrase in item or item in phrase for item in picked):
            continue
        picked.append(phrase)
        if len(picked) >= top_k:
            break
    return picked


def _title_from_keywords(text: str, max_chars: int) -> str:
    """用关键词拼标题。

    只取**一个**主关键词作为标题：两个词用「·」拼接常得到"因为·情绪"
    这类生硬组合，读起来不像标题。单关键词干净且好认。
    """
    keywords = extract_keywords(text, top_k=1)
    if not keywords:
        return ""
    title = keywords[0]
    if max_chars and len(title) > max_chars:
        title = title[:max_chars].rstrip()
    return title


#: 口语里常见的"句内停顿点"，用来在无标点时切出合适的兜底标题。
_CLAUSE_BREAKERS = (
    "然后",
    "就是",
    "其实",
    "所以",
    "但是",
    "因为",
    "而且",
    "或者说",
    "也就是说",
    "比如说",
    "哪怕",
    "甚至",
    "如果",
    "虽然",
)


def _title_from_lead(text: str, max_chars: int) -> str:
    """兜底标题：取第一个短语，压到很短。

    早期实现是"整句截断"，结果标题和正文几乎一样。现在只取第一个短语
    （第一个逗号之前），并且硬性压到 ``max_chars`` 或 16 字。
    """
    phrases = split_phrases(text)
    lead = phrases[0] if phrases else text
    cleaned = _strip_fillers(lead)
    cap = 16 if max_chars <= 0 else min(max_chars, 16)

    # 没有标点时 split_phrases 会把整段当一个短语，直接截断会留下半句。
    # 这时按"口语中断词"再切一刀（这些词后面常是自然停顿）。
    if len(cleaned) > cap:
        for breaker in _CLAUSE_BREAKERS:
            position = cleaned.find(breaker)
            if cap // 2 <= position <= cap + 6:
                cleaned = cleaned[:position]
                break

    if len(cleaned) > cap:
        cleaned = cleaned[:cap].rstrip()

    # 如果兜底结果仍然"不像主题词"（虚词占比过高），宁可不给标题，
    # 让界面用「第 N 段」占位。半句话当标题比没有标题更糟。
    if not _looks_like_topic(cleaned):
        return ""
    return cleaned


def _looks_like_topic(text: str) -> bool:
    """粗判一段文字是否像"主题词"而非半句话。

    判据：长度 2–16、不包含成句的虚词对（"或者…" "不管…"）、
    且不全由弱字组成。
    """
    if not (2 <= len(text) <= 16):
        return False
    # 以虚词启头基本是半句话的残片
    if text[0] in _WEAK_HEADS:
        return False
    sentence_markers = ("或者", "不管", "除非", "甚至", "如果", "虽然", "但是", "而且")
    if any(text.startswith(marker) for marker in sentence_markers):
        return False
    # 弱字占比过高
    weak = sum(1 for char in text if char in _WEAK_HEADS or char in _WEAK_TAILS)
    if weak / max(1, len(text)) > 0.34:
        return False
    return True


def make_titles(
    texts: Sequence[str],
    api_call: Optional[object] = None,
    max_chars: int = 0,
    use_keywords: bool = True,
    log: Optional[object] = None,
) -> List[str]:
    """为每个段落生成标题。

    参数:
        texts: 段落全文列表。
        api_call: 可选 ``(texts, max_chars) -> list[str]``，由调用方注入
            （把网络细节挡在外面）。传了就用大模型概括。
        max_chars: 标题字数上限；``0`` 或负值表示**不限制**。
        use_keywords: 是否用关键词概括；False 则只用首短语兜底。
        log: 调试回调。

    返回:
        与 ``texts`` 等长的标题列表。任何异常都退化为规则标题，不抛出。
    """
    say = log or (lambda *_a, **_k: None)
    if not texts:
        return []

    limit = int(max_chars) if max_chars and max_chars > 0 else 0

    if api_call is not None:
        try:
            titles = list(api_call(list(texts), limit))
            if len(titles) == len(texts):
                say(f"标题生成（API）：{sum(1 for t in titles if t)}/{len(texts)}")
                return [str(item or "").strip() for item in titles]
            say("API 返回数量不符，改走本地策略")
        except Exception as exc:  # noqa: BLE001 - 标题失败不影响段落
            say(f"API 标题失败，改走本地策略：{exc}")

    result: List[str] = []
    for text in texts:
        title = ""
        if use_keywords:
            title = _title_from_keywords(text, limit)
        if not title:
            title = _title_from_lead(text, limit)
        if limit and len(title) > limit:
            title = title[:limit].rstrip()
        result.append(title)

    say(f"标题生成（本地）：{sum(1 for item in result if item)}/{len(texts)}")
    return result
