# -*- coding: utf-8 -*-
"""标点恢复：给无标点的 ASR 词流补上标点。

为什么需要：中文 ASR 的产出几乎不带标点（实测某 2 小时素材 656 个词里只有
11 个带标点，占 1.7%）。没有标点时，"哪里是一句话"只能靠停顿猜，而中文正常
语流的换气间隔经常是 0，于是整段糊成一块——这正是"句子没有被分开"的根因。

这里提供两级策略：

* **规则法**（默认，零依赖）：用停顿长度 + 语气词 + 连词做判断，离线、瞬时；
* **可扩展**：预留 ``restore_api`` 走大模型，质量更好但需要网络。

两种都只**在词后追加标点**（写进 ``word`` 字段的文本里），
**不改时间戳、不改词序**，因此 EDL / 字幕 / 导出全部不受影响。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence

__all__ = [
    "PunctuationConfig",
    "restore_punctuation",
    "DEFAULT_CONFIG",
    "SENTENCE_PROMPT",
]

#: 转写时给 whisper 的提示词：诱导它输出中文标点。
#:
#: 实测有效：同一段音频，含标点的词从 62 个增加到 68 个，句号位置也更合理。
#: 但它只是"引导"，不能保证每句都有标点，所以仍需下面的规则兜底。
SENTENCE_PROMPT = "以下是普通话访谈的转写，请使用中文标点符号（，。？！）断句。"

#: 句末语气词：这些词后面大概率是一句话的结束。
_END_PARTICLES = frozenset(
    [
        "啊",
        "呀",
        "嘛",
        "吧",
        "呢",
        "哦",
        "噢",
        "啦",
        "咯",
        "哈",
        "嗯",
        "对",
        "是的",
        "好",
    ]
)

#: 常见连词/副词：这些词前面往往是一句话的开头，前面该断。
_LEADING_WORDS = frozenset(
    [
        "所以",
        "因为",
        "但是",
        "然后",
        "而且",
        "不过",
        "另外",
        "其实",
        "如果",
        "虽然",
        "因此",
        "另外",
        "总之",
        "比如说",
        "也就是说",
        "也就是说",
        "接下来",
        "那么",
        "这时候",
        "首先",
        "其次",
        "最后",
    ]
)

#: 已有标点（任意一种）：不再重复添加。
_EXISTING_PUNCT = re.compile(r"[。？！；…，、,;:：!?]$")


@dataclass(frozen=True)
class PunctuationConfig:
    """标点恢复的参数。

    属性:
        sentence_gap: 停顿超过该值（秒）判定为句末，补句号。
        clause_gap: 停顿超过该值（秒）判定为分句，补逗号。
        max_chars: 单句超过该字数，即使没停顿也补逗号（避免长句无标点）。
        enable_particles: 是否用语气词判断句末。
        enable_leading: 是否用连词判断句首。
        sentence_mark: 句末标点符号。
        clause_mark: 分句标点符号。
    """

    sentence_gap: float = 0.42
    clause_gap: float = 0.24
    max_chars: int = 28
    enable_particles: bool = True
    enable_leading: bool = True
    sentence_mark: str = "。"
    clause_mark: str = "，"


#: 默认配置。
DEFAULT_CONFIG = PunctuationConfig()


def _has_punct(text: str) -> bool:
    """该词是否已经带标点。"""
    return bool(_EXISTING_PUNCT.search(str(text or "")))


def restore_punctuation(
    words: Sequence[Dict[str, object]],
    config: Optional[PunctuationConfig] = None,
    sentence_prompt: bool = True,
) -> List[Dict[str, object]]:
    """给词序列补标点，返回**新的**词字典列表。

    参数:
        words: 原始词字典（``word`` / ``start`` / ``end`` 等字段）。
        config: 参数，默认 :data:`DEFAULT_CONFIG`。
        sentence_prompt: 预留开关（是否启用语气词/连词判断），当前由
            ``config.enable_particles`` / ``enable_leading`` 控制。

    返回:
        与输入等长的词列表，``word`` 字段可能被追加标点；
        **不改动** ``start`` / ``end`` / ``seg`` 等其它字段。

    说明:
        已经带标点的词保持原样——ASR 给出的标点是模型判断，通常比规则更准。
    """
    cfg = config or DEFAULT_CONFIG
    if not words:
        return []

    result: List[Dict[str, object]] = []
    run_chars = 0  # 当前"未断句"的累计字数

    for index, raw in enumerate(words):
        item = dict(raw)
        text = str(item.get("word") or "")
        if not text.strip():
            result.append(item)
            continue

        run_chars += len(text)

        # 已自带标点：尊重原样，重置累计
        if _has_punct(text):
            run_chars = 0
            result.append(item)
            continue

        # 没有下一个词 → 结尾补句号
        if index == len(words) - 1:
            item["word"] = text + cfg.sentence_mark
            result.append(item)
            continue

        nxt = words[index + 1]
        gap = float(nxt.get("start", 0.0) or 0.0) - float(item.get("end", 0.0) or 0.0)

        mark = ""

        # 1) 停顿判断（最强信号）
        if gap >= cfg.sentence_gap:
            mark = cfg.sentence_mark
        elif gap >= cfg.clause_gap:
            mark = cfg.clause_mark

        # 2) 语气词收尾 → 倾向句号
        #    只在停顿也偏长时采用，避免"啊"出现在句中就被断成句
        if (
            not mark
            and cfg.enable_particles
            and text in _END_PARTICLES
            and gap >= cfg.clause_gap
        ):
            mark = cfg.sentence_mark

        # 3) 下一个词是连词 → 这里该断
        if not mark and cfg.enable_leading:
            nxt_text = str(nxt.get("word") or "").strip()
            if nxt_text in _LEADING_WORDS:
                mark = cfg.clause_mark

        # 4) 长句兜底：**不做**按字数补标点。
        #
        # 中文 ASR 常把词切成单字（"第" "三" "节" "课"），按字数兜底会得到
        # "第三，节，课" 这种碎片。真正该兜底的是"句长"，而那属于切句层
        # （sentencer）的职责——那里有 stopword 与停顿可以参考。

        if mark:
            item["word"] = text + mark
            run_chars = 0 if mark == cfg.sentence_mark else run_chars

        result.append(item)

    return result


def restore_with_prompt_check(words: Sequence[Dict[str, object]]) -> Dict[str, object]:
    """统计标点覆盖率，用于判断是否真的需要规则兜底。

    返回:
        ``{"total": n, "with_punct": m, "ratio": r}``。
    """
    total = len(words)
    with_punct = sum(1 for w in words if _has_punct(str(w.get("word") or "")))
    return {
        "total": total,
        "with_punct": with_punct,
        "ratio": round(with_punct / total, 4) if total else 0.0,
    }
