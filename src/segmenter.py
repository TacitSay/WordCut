"""分段策略模块：把 Token 序列切成段落，供前端分行显示。

设计目标（见 docs/架构预留设计.md 第 1 节）：

- 分段是**可替换策略**：`Segmenter` 是协议，`RuleSegmenter` 是当前实现，
  将来可以用 `LLMSegmenter` 做语义分段而不改其他任何代码。
- 分段只影响**显示**，不影响 EDL / 时间映射 / 导出（不变量 1、3、6 不受影响）。

不变量：

- 不变量 4：Token.id 只读不改，分段只做分组，不重排、不改写。
- 不变量 2：时间单位秒（float）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Protocol, Sequence, runtime_checkable

from model import Token

__all__ = [
    "Segmenter",
    "RuleSegmenter",
    "Segment",
    "DEFAULT_SEGMENTER",
    "segment_tokens",
]

#: 中文断句标点（出现即视为段落结束，标点本身留在上一段）。
_BREAK_PUNCT: frozenset[str] = frozenset("。？！；…")

#: 次级断句标点（逗号类：仅在段落过长时才切）。
_SOFT_PUNCT: frozenset[str] = frozenset("，、,;:")

#: 收尾标点（紧跟前一字，不单独成段）。
_CLOSING_PUNCT: frozenset[str] = frozenset("”’」』）】》〉)]}")


@dataclass(frozen=True)
class Segment:
    """一个段落的显示信息。

    属性：
        index: 段落序号（0 起）。
        start: 段落起始时间（秒，源时间轴）。
        end: 段落结束时间（秒，源时间轴）。
        text: 段落拼接后的文本。
        token_ids: 段落包含的 Token id（有序，只读引用）。
    """

    index: int
    start: float
    end: float
    text: str
    token_ids: tuple[str, ...]

    @property
    def duration(self) -> float:
        """段落时长（秒）。"""
        return self.end - self.start

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "start": self.start,
            "end": self.end,
            "duration": self.duration,
            "text": self.text,
            "token_ids": list(self.token_ids),
        }


@runtime_checkable
class Segmenter(Protocol):
    """分段策略协议。

    任何实现只要提供 `segment(tokens) -> list[list[Token]]` 即可被替换使用。
    新增策略（如语义分段）只需实现本协议并注册，不改动调用方。
    """

    def segment(self, tokens: Sequence[Token]) -> List[List[Token]]:
        """把 Token 序列切成若干段落（每段是 Token 列表，顺序与输入一致）。"""
        ...


class RuleSegmenter:
    """基于规则的段落切分。

    三条切分依据（满足任一即换段）：

    1. **segment 变化**：faster-whisper 的句子级 segment 变了（最稳的信号）；
    2. **硬标点**：句末标点（。？！；…），标点留在上一段；
    3. **时间间隔**：相邻 Token 间隔超过 `gap_threshold` 秒（默认 0.6）；
    4. **段落过长**：当前段累计时长超过 `max_segment_seconds`（默认 15 秒）
       且遇到逗号类软标点，或累计超过 `hard_max_seconds`（默认 30 秒）强制切。

    实现说明：本类**不依赖 token 的 seg 字段**（Token 数据模型里没有 seg），
    因此依据 1 实际通过"句末标点 + 间隔"组合近似；若将来 Token 加上 seg 字段，
    可在 `segment` 里加一条判断而不影响外部调用。

    参数:
        gap_threshold: 间隔阈值（秒），超过即断段。
        max_segment_seconds: 软上限（秒），超过且遇软标点时断段。
        hard_max_seconds: 硬上限（秒），超过强制断段。
    """

    def __init__(
        self,
        gap_threshold: float = 0.6,
        max_segment_seconds: float = 15.0,
        hard_max_seconds: float = 30.0,
    ) -> None:
        if gap_threshold < 0:
            raise ValueError("gap_threshold 必须 >= 0")
        if max_segment_seconds <= 0 or hard_max_seconds <= 0:
            raise ValueError("max_segment_seconds / hard_max_seconds 必须 > 0")
        self.gap_threshold = float(gap_threshold)
        self.max_segment_seconds = float(max_segment_seconds)
        self.hard_max_seconds = float(hard_max_seconds)

    def segment(self, tokens: Sequence[Token]) -> List[List[Token]]:
        """按规则把 Token 序列切段。

        参数:
            tokens: Token 序列（不必有序，内部按 start 排序）。

        返回:
            段落列表；输入为空时返回空列表。
        """
        ordered = sorted(
            (token for token in tokens if token.text and token.text.strip()),
            key=lambda token: (token.start, token.end),
        )
        if not ordered:
            return []

        groups: List[List[Token]] = []
        current: List[Token] = []
        seg_start = 0.0
        prev_end = 0.0

        for token in ordered:
            if not current:
                current = [token]
                seg_start = token.start
                prev_end = token.end
                continue

            gap = token.start - prev_end
            text = token.text.strip()
            hard_break = bool(text) and _ends_with_hard_punct(text)
            soft_break = bool(text) and _ends_with_soft_punct(text)

            # 间隔过大：当前 token 另起一段，不并入前段。
            if gap >= self.gap_threshold:
                groups.append(current)
                current = [token]
                seg_start = token.start
                prev_end = token.end
                if hard_break:
                    groups.append(current)
                    current = []
                continue

            current.append(token)
            prev_end = max(prev_end, token.end)
            elapsed = token.end - seg_start

            should_break = (
                elapsed >= self.hard_max_seconds
                or (elapsed >= self.max_segment_seconds and soft_break)
            )

            if hard_break or should_break:
                groups.append(current)
                current = []

        if current:
            groups.append(current)
        return groups


def _strip_trailing_closing(text: str) -> str:
    """去掉末尾的收尾引号/括号，便于判断前一个字符是否为标点。"""
    while text and text[-1] in _CLOSING_PUNCT:
        text = text[:-1]
    return text


def _ends_with_hard_punct(text: str) -> bool:
    """判断文本是否以句末标点结尾。"""
    stripped = _strip_trailing_closing(text.strip())
    return bool(stripped) and stripped[-1] in _BREAK_PUNCT


def _ends_with_soft_punct(text: str) -> bool:
    """判断文本是否以逗号类软标点结尾。"""
    stripped = _strip_trailing_closing(text.strip())
    return bool(stripped) and stripped[-1] in _SOFT_PUNCT


#: 默认分段器（全局单例，无状态，可安全共享）。
DEFAULT_SEGMENTER: Segmenter = RuleSegmenter()


def segment_tokens(
    tokens: Sequence[Token],
    segmenter: Segmenter | None = None,
) -> List[Segment]:
    """把 Token 序列切成段落并包装成 `Segment` 列表。

    参数:
        tokens: Token 序列。
        segmenter: 分段策略；为 None 时使用 `DEFAULT_SEGMENTER`。

    返回:
        段落列表；输入为空时返回空列表。
    """
    active = segmenter if segmenter is not None else DEFAULT_SEGMENTER
    groups = active.segment(tokens)

    segments: List[Segment] = []
    for index, group in enumerate(groups):
        if not group:
            continue
        text = "".join(token.text for token in group).strip()
        segments.append(
            Segment(
                index=index,
                start=float(min(token.start for token in group)),
                end=float(max(token.end for token in group)),
                text=text,
                token_ids=tuple(token.id for token in group),
            )
        )
    return segments
