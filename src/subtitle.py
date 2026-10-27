"""字幕重定时模块：把源时间轴上的 Token 序列换算成目标时间轴的 SRT 字幕。

纯 Python 标准库实现（不依赖 numpy、不依赖 ffmpeg），只与
:class:`model.Token` / :class:`model.TimeMap` 协作，不做任何音频处理。

对外两个函数：

- :func:`rebuild_srt`：剔除被删除的 Token，把剩下的 Token 换算到目标
  时间轴，聚合成字幕条目并输出 SRT 文本。
- :func:`tokens_to_srt`：不删除任何 Token 的便捷版本。

遵守的不变量（见 docs/不变量.md）：

* 不变量 2：所有时间戳单位为秒（float），毫秒只在写 SRT 文本时出现。
* 不变量 4：Token.id 只用于判定是否被删除，绝不重排、绝不改写。
* 不变量 6：时间换算一律走 TimeMap 的 ``source_to_target``，
  本模块不自己推算删除累计量，保证波形 / 字幕 / 播放头三者一致。
* 不变量 7：输出条目的时间戳保证 ``start < end`` 且 ``start >= 0``。
"""

from __future__ import annotations

import unicodedata
from typing import AbstractSet, List, Optional, Sequence, Tuple

from model import TimeMap, Token

__all__ = ["rebuild_srt", "tokens_to_srt", "GAP_BREAK_S", "MAX_LINE_WIDTH"]

# 相邻 Token 在目标时间轴上的间隔超过该值（秒）即切分出下一条字幕。
GAP_BREAK_S: float = 0.6

# 每条字幕最多 2 行，每行最多 16 个汉字（半角字符按 0.5 个汉字计）。
MAX_LINE_WIDTH: float = 16.0
MAX_LINES_PER_CUE: int = 2

# 一条字幕最少占 1 毫秒，避免四舍五入后出现 start == end。
_MIN_CUE_S: float = 0.001

# 断句标点：Token 文本以这些字符结尾即视为一个分句结束。
_BREAK_PUNCT = frozenset("。？！，、；：…—．,.!?;:")

# 收尾标点：紧贴在前一个字符后面，不单独成行。
_CLOSING_PUNCT = frozenset("”’」』）】》〉)］｝]}")


# ----------------------------------------------------------------------
# 对外接口
# ----------------------------------------------------------------------
def rebuild_srt(
    tokens: Sequence[Token],
    time_map: TimeMap,
    deleted_ids: AbstractSet[str],
) -> str:
    """把 Token 序列重建为目标时间轴上的 SRT 字幕文本。

    处理步骤：

    1. 按 ``token.start`` 排序后遍历（Token 已是时间序，排序只是兜底）；
    2. 跳过 ``token.id`` 落在 ``deleted_ids`` 里的 Token；
    3. 剩下的 Token 用 ``time_map.source_to_target`` 把 ``start`` / ``end``
       换算到目标时间轴；
    4. 聚合成分句：Token 文本以中文句号 / 问号 / 感叹号 / 逗号等标点结尾，
       或与下一个 Token 在目标时间轴上的间隔超过 0.6 秒，即切分；
    5. 每条分句按每行 16 个汉字折行，最多 2 行；超长的分句按字符占比
       拆分到多条字幕，不丢字；
    6. 输出标准 SRT：序号从 1 开始，时间格式 ``HH:MM:SS,mmm``，
       条目之间空一行。

    参数:
        tokens: Token 列表，按时间排序；每个 Token 具有 ``id`` / ``text`` /
            ``start`` / ``end`` 属性，时间单位是秒。
        time_map: TimeMap 实例，负责源时间轴到目标时间轴的换算。
        deleted_ids: 被删除的 Token id 集合。

    返回:
        SRT 格式字符串；没有任何可输出内容时返回空字符串 ``""``。
    """
    excluded_ids = set(deleted_ids) if deleted_ids else set()
    cues = _build_cues(tokens, time_map, excluded_ids)

    blocks: List[str] = []
    previous_end_ms = 0
    for index, (cue_start, cue_end, lines) in enumerate(cues, start=1):
        if not lines:
            continue
        start_ms = _to_milliseconds(cue_start)
        end_ms = _to_milliseconds(cue_end)
        # 保证条目按时间递增、互不重叠，且严格满足 start < end（不变量 7）。
        start_ms = max(start_ms, previous_end_ms, 0)
        if end_ms <= start_ms:
            end_ms = start_ms + 1
        blocks.append(
            "{index}\n{start} --> {end}\n{text}".format(
                index=index,
                start=_format_timecode(start_ms),
                end=_format_timecode(end_ms),
                text="\n".join(lines),
            )
        )
        previous_end_ms = end_ms

    if not blocks:
        return ""
    return "\n\n".join(blocks) + "\n"


def tokens_to_srt(tokens: Sequence[Token], time_map: Optional[TimeMap] = None) -> str:
    """不删除任何 Token 的 SRT 生成便捷函数。

    参数:
        tokens: Token 列表，按时间排序。
        time_map: TimeMap 实例；为 ``None`` 时使用恒等映射
            （源时间轴与目标时间轴重合）。

    返回:
        SRT 格式字符串；没有任何可输出内容时返回空字符串 ``""``。
    """
    active_map = TimeMap.identity() if time_map is None else time_map
    return rebuild_srt(tokens, active_map, frozenset())


# ----------------------------------------------------------------------
# 分句与字幕条目
# ----------------------------------------------------------------------
def _build_cues(
    tokens: Sequence[Token],
    time_map: TimeMap,
    excluded_ids: AbstractSet[str],
) -> List[Tuple[float, float, List[str]]]:
    """生成 (起始秒, 结束秒, 文本行列表) 的字幕条目序列。"""
    cues: List[Tuple[float, float, List[str]]] = []
    for group_start, group_end, group_text in _group_tokens(
        tokens, time_map, excluded_ids
    ):
        cues.extend(_split_cue(group_start, group_end, group_text))
    return cues


def _group_tokens(
    tokens: Sequence[Token],
    time_map: TimeMap,
    excluded_ids: AbstractSet[str],
) -> List[Tuple[float, float, str]]:
    """把保留的 Token 聚合成分句，返回 (起始秒, 结束秒, 文本) 序列。"""
    groups: List[Tuple[float, float, str]] = []

    current_start = 0.0
    current_end = 0.0
    current_text = ""
    has_current = False
    previous_end: Optional[float] = None

    for token in sorted(tokens, key=lambda item: (item.start, item.end)):
        # 不变量 4：只按 id 判定删除，不改写 id、不重排引用。
        if token.id in excluded_ids:
            continue

        text = _normalize_text(token.text)
        if not text:
            continue

        start = float(time_map.source_to_target(token.start))
        end = float(time_map.source_to_target(token.end))
        if end < start:
            start, end = end, start

        if has_current and previous_end is not None and start - previous_end > GAP_BREAK_S:
            groups.append((current_start, current_end, current_text))
            has_current = False
            current_text = ""

        if not has_current:
            current_start = start
            current_end = end
            current_text = text
            has_current = True
        else:
            current_text = _join_texts(current_text, text)
            current_end = max(current_end, end)

        previous_end = end

        if _ends_with_break_punct(text):
            groups.append((current_start, current_end, current_text))
            has_current = False
            current_text = ""

    if has_current:
        groups.append((current_start, current_end, current_text))
    return groups


def _split_cue(
    start: float,
    end: float,
    text: str,
) -> List[Tuple[float, float, List[str]]]:
    """把一条分句拆成不超过 2 行的字幕条目，超长文本按字符占比分时。"""
    lines = _wrap_text(text)
    if not lines:
        return []

    chunks = [
        lines[offset : offset + MAX_LINES_PER_CUE]
        for offset in range(0, len(lines), MAX_LINES_PER_CUE)
    ]
    if len(chunks) == 1:
        return [(start, end, chunks[0])]

    weights = [max(_text_width("".join(chunk)), 1e-6) for chunk in chunks]
    total_weight = sum(weights)
    span = max(end - start, 0.0)

    cues: List[Tuple[float, float, List[str]]] = []
    consumed = 0.0
    cursor = start
    for index, (chunk, weight) in enumerate(zip(chunks, weights)):
        is_last = index == len(chunks) - 1
        chunk_start = start + span * (consumed / total_weight)
        consumed += weight
        chunk_end = end if is_last else start + span * (consumed / total_weight)

        chunk_start = max(chunk_start, cursor)
        if chunk_end <= chunk_start:
            chunk_end = chunk_start + _MIN_CUE_S
        cues.append((chunk_start, chunk_end, chunk))
        cursor = chunk_end
    return cues


# ----------------------------------------------------------------------
# 文本折行
# ----------------------------------------------------------------------
def _normalize_text(text: str) -> str:
    """去掉换行 / 制表符等控制字符，并压缩多余空白。"""
    if not isinstance(text, str):
        return ""
    cleaned = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    return " ".join(cleaned.split())


def _join_texts(left: str, right: str) -> str:
    """拼接相邻 Token 的文本；两侧都是半角字母数字时补一个空格。"""
    if not left:
        return right
    if not right:
        return left
    if _needs_space(left[-1], right[0]):
        return left + " " + right
    return left + right


def _needs_space(left_char: str, right_char: str) -> bool:
    """判断两个相邻字符之间是否需要空格（英文 Token 之间）。"""
    return _char_width(left_char) < 1.0 and _char_width(right_char) < 1.0


def _ends_with_break_punct(text: str) -> bool:
    """判断文本是否以断句标点结尾（忽略其后的收尾引号 / 括号）。"""
    for char in reversed(text):
        if char in _CLOSING_PUNCT or char.isspace():
            continue
        return char in _BREAK_PUNCT
    return False


def _wrap_text(text: str, max_width: float = MAX_LINE_WIDTH) -> List[str]:
    """按显示宽度折行，每行不超过 ``max_width`` 个汉字。"""
    lines: List[str] = []
    current = ""
    current_width = 0.0

    for piece in _flatten_pieces(_text_pieces(text), max_width):
        piece_width = _text_width(piece)
        if current and current_width + piece_width > max_width:
            stripped = current.rstrip()
            if stripped:
                lines.append(stripped)
            current = piece
            current_width = piece_width
        else:
            current += piece
            current_width += piece_width

    stripped = current.rstrip()
    if stripped:
        lines.append(stripped)
    return [line for line in lines if line]


def _text_pieces(text: str) -> List[str]:
    """把文本切成不可再分的排版块：整字、半角单词、收尾标点。"""
    pieces: List[str] = []
    ascii_buffer = ""

    for char in text:
        if char in _CLOSING_PUNCT and pieces:
            pieces[-1] += char
        elif _is_wide(char):
            if ascii_buffer:
                pieces.append(ascii_buffer)
                ascii_buffer = ""
            pieces.append(char)
        else:
            ascii_buffer += char

    if ascii_buffer:
        pieces.append(ascii_buffer)
    return pieces


def _flatten_pieces(pieces: List[str], max_width: float) -> List[str]:
    """把宽度超过一行上限的排版块按字符再切开。"""
    flattened: List[str] = []
    for piece in pieces:
        if _text_width(piece) <= max_width:
            flattened.append(piece)
            continue
        current = ""
        current_width = 0.0
        for char in piece:
            char_width = _char_width(char)
            if current and current_width + char_width > max_width:
                flattened.append(current)
                current = char
                current_width = char_width
            else:
                current += char
                current_width += char_width
        if current:
            flattened.append(current)
    return flattened


def _is_wide(char: str) -> bool:
    """判断字符是否为全角（汉字、全角标点等）。"""
    return unicodedata.east_asian_width(char) in ("W", "F")


def _char_width(char: str) -> float:
    """单个字符的显示宽度：全角算 1 个汉字，半角算 0.5 个。"""
    if unicodedata.east_asian_width(char) in ("W", "F", "A"):
        return 1.0
    return 0.5


def _text_width(text: str) -> float:
    """整段文本的显示宽度，单位是汉字。"""
    return sum(_char_width(char) for char in text)


# ----------------------------------------------------------------------
# 时间格式化
# ----------------------------------------------------------------------
def _to_milliseconds(seconds: float) -> int:
    """秒 -> 毫秒（四舍五入，负数按 0 处理）。"""
    if seconds != seconds:  # NaN
        return 0
    return int(round(max(float(seconds), 0.0) * 1000.0))


def _format_timecode(milliseconds: int) -> str:
    """毫秒 -> SRT 时间戳 ``HH:MM:SS,mmm``。"""
    value = max(int(milliseconds), 0)
    hours, remainder = divmod(value, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    seconds, millis = divmod(remainder, 1000)
    return "{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}".format(
        hours=hours,
        minutes=minutes,
        seconds=seconds,
        millis=millis,
    )
