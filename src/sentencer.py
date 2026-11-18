# -*- coding: utf-8 -*-
"""把词级时间戳聚成"句子"。

为什么需要这一层：词级对齐之后，最自然的阅读与操作单位是**句子**，不是词、
也不是段落。删改按句子走、选中按句子走、批量处理也按句子走，都比按词顺手。

难点在于中文 ASR 的产出几乎没有标点（实测某 2 小时素材 656 个词只有 11 个
带标点，占 1.7%），所以不能只靠标点断句。这里用三个信号联合判断：

1. **标点**：有句末标点（。？！；…）就地断句——最强信号，有就一定断；
2. **停顿**：相邻词间隔超过阈值时断句。阈值按"相对显著"取，避免把正常
   语流里的换气切成句；
3. **长度兜底**：句子太久没断（超过 ``max_seconds`` 或 ``max_chars``）时，
   在最近一个软标点或最长停顿处强行断开，避免出现整段没有一个分隔。

这一层**只决定边界，不改词、不改时间戳**，因此不影响 EDL 与导出。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

from model import Token
from segmenter import _ends_with_hard_punct

__all__ = [
    "Sentence",
    "build_sentences",
    "DEFAULT_GAP_SECONDS",
    "DEFAULT_MAX_SECONDS",
    "MIN_SENTENCE_CHARS",
    "SPLIT_GAP",
]

#: 断句的停顿阈值（秒）。
#:
#: 中文正常语流的换气常见 0.1–0.3s，而"一句话说完"的停顿通常 ≥0.35s。
#: 取 0.32 是实测折中：再小会把词组切碎，再大则长句粘连。
DEFAULT_GAP_SECONDS = 0.32

#: 单句最长时长（秒）。超过就强制断开。
#:
#: 口语里一句话通常 3–8 秒；10 秒已偏长，15 秒以上读起来会"没有停顿感"。
DEFAULT_MAX_SECONDS = 10.0

#: 单句最长字数。中文书面一句约 30–40 字，口语略短。
DEFAULT_MAX_CHARS = 42

#: 拆长句时认可的"真实停顿"阈值（秒）。
#:
#: 比断句阈值低一些：即使不足以断句，也足以作为拆分点。0.18s 覆盖了
#: 绝大多数换气位置，又不会落在字与字之间。
SPLIT_GAP = 0.18


@dataclass(frozen=True)
class Sentence:
    """一个句子：句内连续的 Token。

    属性:
        index: 句子序号（0 起，按时间顺序）。
        tokens: 句内 Token（有序，只读引用）。
        start: 句首时间（秒），取首词起点。
        end: 句末时间（秒），取末词终点。
        text: 句子文本（句内词直接拼接，保留原有标点）。
        ends_sentence: 是否以句末标点收尾（用于判断"这句本身是完整的"）。
    """

    index: int
    tokens: Tuple[Token, ...]
    start: float
    end: float
    text: str
    ends_sentence: bool

    @property
    def duration(self) -> float:
        """句子时长（秒）。"""
        return self.end - self.start

    @property
    def token_ids(self) -> Tuple[str, ...]:
        """句内 Token id（有序）。"""
        return tuple(token.id for token in self.tokens)

    def to_dict(self) -> dict:
        """转成可 JSON 序列化的字典。"""
        return {
            "index": self.index,
            "start": self.start,
            "end": self.end,
            "duration": self.duration,
            "text": self.text,
            "token_ids": list(self.token_ids),
            "ends_sentence": self.ends_sentence,
        }


def _text_of(tokens: Sequence[Token]) -> str:
    """句内文本：直接拼接（词里可能自带标点与空格）。"""
    return "".join(str(token.text) for token in tokens).strip()


def _pick_break_index(
    tokens: Sequence[Token],
    gaps: Sequence[float],
    max_chars: int,
) -> int:
    """在超长句里挑一个最合适的位置断开，返回"断开点的前一个下标"。

    策略：在**靠近中点**的候选里挑最好的，而不是在全句里挑最好的——
    后者会把断点推到句尾，导致前半句仍然过长。

    候选打分（越大越好）：
      * 该词以软标点结尾：+2.0（断在逗号后最自然）
      * 停顿长度：直接加上去（0–1 秒级，权重很高）
      * 越靠近理想断点（按字数中位）：额外加分
    """
    if len(tokens) <= 1:
        return 0

    texts = [str(token.text or "").strip() for token in tokens]
    prefix = [0]
    for text in texts:
        prefix.append(prefix[-1] + max(1, len(text)))
    total = prefix[-1]

    best_index = max(0, len(tokens) // 2 - 1)
    best_score = -1e9

    for index in range(0, len(tokens) - 1):
        # 左侧字数应接近 max_chars（不要太短也不要超过太多）
        left_chars = prefix[index + 1]
        if left_chars < 4:
            continue
        over = max(0, left_chars - max_chars)
        score = -0.35 * abs(left_chars - max_chars) - 3.0 * over

        if texts[index].endswith(("，", "、", ",", ";", "：", ":")):
            score += 2.0
        if index < len(gaps):
            score += min(1.0, gaps[index]) * 3.0

        if score > best_score:
            best_score = score
            best_index = index

    return best_index


#: 一个句子至少要有的字数。低于它的碎片不并回前一句。
MIN_SENTENCE_CHARS = 6


def _split_long(
    tokens: List[Token],
    max_seconds: float,
    max_chars: int,
    min_chars: int = MIN_SENTENCE_CHARS,
) -> List[List[Token]]:
    """把过长的句子拆到阈值以内。

    优先级（这是关键，按字数硬切会切出"十" "分" 这种碎片）：

    1. **在真实停顿处切**：找一个间隔 ≥ ``SPLIT_GAP`` 的位置。这类位置是
       说话人自己换气的地方，切在那里语义最完整；
    2. **其次在软标点后切**；
    3. 都没有时才按字数接近中点切——且仍受 ``min_chars`` 约束。

    找不到任何合规断点时**不拆**：宁可一句偏长，也不产出碎片。
    """
    if len(tokens) <= 1:
        return [tokens]

    span = tokens[-1].end - tokens[0].start
    chars = len(_text_of(tokens))
    if span <= max_seconds and chars <= max_chars:
        return [tokens]

    lengths = [max(1, len(str(token.text or "").strip())) for token in tokens]
    total = sum(lengths)
    prefix = [0]
    for length in lengths:
        prefix.append(prefix[-1] + length)

    def allowed(index: int) -> bool:
        """断在 index 之后是否合规（两侧都不低于 min_chars）。"""
        return prefix[index + 1] >= min_chars and (total - prefix[index + 1]) >= min_chars

    gaps = [
        max(0.0, tokens[i + 1].start - tokens[i].end) for i in range(len(tokens) - 1)
    ]

    cut = -1

    # ---- 1) 真实停顿：取 ≥ SPLIT_GAP 的候选里最靠近中点的 ----
    pause_candidates = [
        index for index in range(len(tokens) - 1) if gaps[index] >= SPLIT_GAP and allowed(index)
    ]
    if pause_candidates:
        cut = min(pause_candidates, key=lambda i: abs(prefix[i + 1] - max_chars))

    # ---- 2) 软标点后 ----
    if cut < 0:
        punct_candidates = [
            index
            for index in range(len(tokens) - 1)
            if allowed(index)
            and str(tokens[index].text or "").strip().endswith(("，", "、", ",", ";", "：", ":"))
        ]
        if punct_candidates:
            cut = min(punct_candidates, key=lambda i: abs(prefix[i + 1] - max_chars))

    # ---- 3) 按字数接近中点（仍受 min_chars 约束）----
    if cut < 0:
        by_chars = [index for index in range(len(tokens) - 1) if allowed(index)]
        if by_chars:
            cut = min(by_chars, key=lambda i: abs(prefix[i + 1] - max_chars))

    if cut < 0:
        return [tokens]

    left, right = tokens[: cut + 1], tokens[cut + 1 :]
    return _split_long(left, max_seconds, max_chars, min_chars) + _split_long(
        right, max_seconds, max_chars, min_chars
    )


def build_sentences(
    tokens: Iterable[Token],
    gap_seconds: float = DEFAULT_GAP_SECONDS,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> List[Sentence]:
    """把 Token 序列切成句子。

    参数:
        tokens: Token 序列（不要求有序，内部按 start 排序）。
        gap_seconds: 停顿断句阈值（秒）。
        max_seconds: 单句最长时长，超过则强制断开。
        max_chars: 单句最长字数。

    返回:
        `Sentence` 列表；输入为空或全是空文本时返回空列表。

    说明:
        **不修改任何 Token**，只决定分组边界。
    """
    ordered = sorted(
        (token for token in tokens if str(token.text or "").strip()),
        key=lambda token: (token.start, token.end),
    )
    if not ordered:
        return []

    # ---- 第一遍：按标点 + 停顿切出"自然句" ----
    groups: List[List[Token]] = []
    current: List[Token] = [ordered[0]]

    for previous, token in zip(ordered, ordered[1:]):
        gap = token.start - previous.end
        # 前一个词以句末标点收尾 → 一定断句
        broke_by_punct = _ends_with_hard_punct(str(previous.text or ""))
        # 停顿足够长 → 断句
        broke_by_gap = gap >= gap_seconds
        if broke_by_punct or broke_by_gap:
            groups.append(current)
            current = [token]
        else:
            current.append(token)
    if current:
        groups.append(current)

    # ---- 第二遍：把过长/过久的句子再拆开 ----
    final: List[List[Token]] = []
    for group in groups:
        final.extend(_split_long(group, max_seconds, max_chars))

    # ---- 组装 ----
    sentences: List[Sentence] = []
    for index, group in enumerate(final):
        text = _text_of(group)
        if not text:
            continue
        sentences.append(
            Sentence(
                index=len(sentences),
                tokens=tuple(group),
                start=float(group[0].start),
                end=float(group[-1].end),
                text=text,
                ends_sentence=_ends_with_hard_punct(text),
            )
        )
    return sentences


def sentences_text(sentences: Sequence[Sentence]) -> str:
    """把句子按行拼成纯文本（一句一行）。"""
    return "\n".join(sentence.text for sentence in sentences)


def find_sentence_index(sentences: Sequence[Sentence], token_id: str) -> Optional[int]:
    """给定 Token id，返回它所在句子的下标；找不到返回 ``None``。"""
    for sentence in sentences:
        if token_id in sentence.token_ids:
            return sentence.index
    return None
