"""编排模块：把「对齐 → 删除 → 渲染 → 字幕 → 导出」串成一条流水线。

本模块只做编排，不实现算法：

* :func:`load_tokens`：faster-whisper 的 word dict 列表 -> :class:`model.Token` 列表。
* :func:`build_deletes`：被删 Token 集合 -> :class:`model.DeleteSpan` 列表。
* :func:`run_pipeline`：一次跑完整条链路，产出音频 / 字幕 / EDL 三个文件。

遵守的不变量（见 docs/不变量.md）：

* 不变量 1：源样本只读。本模块从不就地改动 ``samples``，切片与拼接全部由
  :func:`edl_render.render` 在新数组上完成，本模块只把新数组交给导出层。
* 不变量 2：所有时间戳单位为秒（float）；本模块不做毫秒换算。
* 不变量 4：``token.id`` 由词序一次性生成（``t00000`` 起），之后绝不重排。
* 不变量 6：字幕时间一律经 :class:`model.TimeMap` 换算，本模块不自己
  推算删除累计量，保证波形 / 字幕 / 播放头三者一致。
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Union

import numpy as np

import audio_fx
import edl_render
import exporter
import subtitle
from model import DeleteSpan, EDL, TimeMap, Token

__all__ = [
    "TOKEN_ID_PREFIX",
    "load_tokens",
    "build_deletes",
    "run_pipeline",
]

#: Token id 前缀，后接 5 位以上十进制序号（``t00000``、``t00001``……）。
TOKEN_ID_PREFIX: str = "t"

#: 连片删除的合并阈值（秒）：词序上连续的两个被删词，间隔不超过它就并成一段。
#:
#: 为什么需要：删除整句时若逐词各删一段，词间停顿（常见 0.05–0.3 秒）会留在
#: 成片里，听感上是一串"碎空隙"。0.6 秒能覆盖正常语流内的停顿，又不会把真正的
#: 长静默（>0.6s，通常是换段/换人）一并吞掉。
DEFAULT_MERGE_GAPS: float = 0.6

#: 报错时最多列举的 id 个数，避免几千个 id 撑爆错误信息。
_MAX_LISTED_IDS: int = 50

#: 单一源素材的默认标识（``EDL.source`` 需要一个非空字符串）。
_DEFAULT_SOURCE_NAME: str = "source"


# ----------------------------------------------------------------------
# 1. 词表 -> Token
# ----------------------------------------------------------------------
def load_tokens(
    words: Sequence[Mapping[str, object]],
    deleted_ids: Optional[Iterable[str]] = None,
) -> List[Token]:
    """把 faster-whisper 输出的 word dict 列表转成 :class:`model.Token` 列表。

    映射规则：

    * ``id``：按词序生成，格式 ``f"t{index:05d}"``（如 ``t00000``），
      同一份 ``words`` 重复调用得到完全相同的 id（不变量 4，可复现）；
    * ``text``：取 ``word`` 字段并 ``strip()``；
    * ``start`` / ``end``：取 ``start`` / ``end`` 字段（秒）；``end`` 缺失时
      用 ``start + dur`` 补出；
    * ``confidence``：取 ``prob`` 字段，缺失或为 ``None`` 时按 1.0 处理，
      并夹到 ``[0, 1]`` 内（:class:`model.Token` 的取值范围约束）；
    * ``is_deleted``：一律为 ``False``——删除是编辑决策，由
      :func:`build_deletes` 与 EDL 表达，不写回词表本身。

    参数:
        words: faster-whisper 的 word dict 序列，每项含 ``seg`` / ``start`` /
            ``end`` / ``dur`` / ``word`` / ``prob`` 字段（``seg`` 与 ``dur``
            在本模块中不使用）。
        deleted_ids: 被删除的 Token id 集合。此处**不处理**，仅为与其他
            环节保持同一套调用签名而保留；删除判定请看 :func:`build_deletes`。

    返回:
        Token 列表，顺序与 ``words`` 一一对应。``words`` 为空时返回空列表。

    异常:
        TypeError: 某个 word 不是 mapping，或时间 / 置信度字段类型非法。
        ValueError: 某个 word 缺少 ``start``（且无法由 ``end`` / ``dur`` 补齐），
            或时间戳非法（``start < 0``、``end <= start``、NaN、无穷大）。
    """
    del deleted_ids  # 明确不使用：删除语义不写回 Token（见 docstring）。

    tokens: List[Token] = []
    for index, word in enumerate(words):
        if not isinstance(word, Mapping):
            raise TypeError(
                f"words[{index}] 必须是 dict/mapping，实际为 {type(word).__name__}"
            )

        start = _time_field(word, "start", index)
        end = _optional_time_field(word, "end", index)
        if end is None:
            duration = _optional_time_field(word, "dur", index)
            if duration is None:
                raise ValueError(
                    f"words[{index}] 缺少 end（也没有 dur 可用于补齐），"
                    f"实际字段：{sorted(word.keys())!r}"
                )
            end = start + duration
        if not start < end:
            raise ValueError(
                f"words[{index}] 必须满足 start < end，"
                f"实际为 start={start!r}, end={end!r}"
            )

        tokens.append(
            Token(
                id=f"{TOKEN_ID_PREFIX}{index:05d}",
                text=_word_text(word, index),
                start=start,
                end=end,
                is_deleted=False,
                confidence=_word_confidence(word, index),
            )
        )
    return tokens


# ----------------------------------------------------------------------
# 2. 删除决策 -> DeleteSpan
# ----------------------------------------------------------------------
def build_deletes(
    tokens: Sequence[Token],
    deleted_ids: Iterable[str],
    pause_merge: float = 0.0,
    merge_gaps: float = DEFAULT_MERGE_GAPS,
) -> List[DeleteSpan]:
    """把被删 Token 转成删除区间，并按需合并被短停顿隔开的相邻删除。

    对每个被删 Token 生成 ``DeleteSpan(start=token.start, end=token.end,
    token_id=token.id, cause="word")``；若它与前一个删除区间的时间间隔
    小于 ``pause_merge`` 秒，则把两者合并成一个区间（起点取前者、终点取
    两者较大者、``token_id`` 保留前者的 id），使中间那段停顿一并剪掉。

    参数:
        tokens: Token 序列（不要求有序，内部会排序）。
        deleted_ids: 被删除的 Token id 集合；``None`` 视作空集合。
            集合里不存在于 ``tokens`` 的 id 会被静默忽略（是否报错由
            :func:`run_pipeline` 决定）。
        pause_merge: 合并阈值（秒），默认 0.0 表示**不额外合并**——只有
            ``间隔 < pause_merge`` 才合并，故 0.0 时相邻但不重叠的区间保持
            独立；若两个被删 Token 本身在时间上重叠（间隔为负），仍会合并。
            必须是非负有限数。

    返回:
        按 ``(start, end)`` 升序排列的 DeleteSpan 列表；没有被删 Token 时
        返回空列表。

    异常:
        TypeError: ``pause_merge`` 不是数值，或 ``tokens`` 中混入非 Token 对象。
        ValueError: ``pause_merge`` 为负、NaN 或无穷大。
    """
    pause = _as_non_negative_float(pause_merge, "pause_merge")
    merge = _as_non_negative_float(merge_gaps, "merge_gaps")
    ids: Set[str] = set(deleted_ids) if deleted_ids else set()
    if not ids:
        return []

    selected: List[Token] = []
    for index, token in enumerate(tokens):
        if not isinstance(token, Token):
            raise TypeError(
                f"tokens[{index}] 必须是 model.Token，实际为 {type(token).__name__}"
            )
        if token.id in ids:
            selected.append(token)

    # 按时间排序后再合并，保证「相邻」指的是时间上的相邻。
    selected.sort(key=lambda item: (item.start, item.end))

    # ------------------------------------------------------------------
    # 第一遍：连片合并
    #
    # 删除一整句话时，逐词生成的区间之间夹着词间停顿（每处几十到几百毫秒）。
    # 如果只按 token 区间删，成片里会留下一串"碎空隙"，听起来像被剪碎的。
    # 这里把**词序上连续、且时间间隔 ≤ merge_gaps** 的被删词并成一段
    # [首个词起点, 末个词终点]，中间停顿一并剪掉。
    #
    # 只在"中间没有被保留的词"时合并——否则会把没删的词连声一起剪掉。
    # ------------------------------------------------------------------
    ordered = sorted(tokens, key=lambda item: (item.start, item.end))
    spans: List[DeleteSpan] = []
    previous_deleted = False
    for token in ordered:
        if token.id not in ids:
            previous_deleted = False
            continue
        if (
            previous_deleted
            and spans
            and merge > 0.0
            and token.start - spans[-1].end <= merge
        ):
            last = spans[-1]
            spans[-1] = DeleteSpan(
                start=last.start,
                end=max(last.end, token.end),
                token_id=last.token_id,
                cause="word",
            )
        else:
            spans.append(
                DeleteSpan(
                    start=token.start,
                    end=token.end,
                    token_id=token.id,
                    cause="word",
                )
            )
        previous_deleted = True

    # ------------------------------------------------------------------
    # 第二遍：可选的"按时间间隔合并"（pause_merge）
    #
    # 与第一遍的区别：这里**不要求词序相邻**，只要两段间隔小于阈值就合并。
    # 用于"把删掉内容后面那段短停顿也一起剪掉"这类更强硬的清理；默认关闭。
    # ------------------------------------------------------------------
    if pause > 0.0 and len(spans) > 1:
        merged: List[DeleteSpan] = [spans[0]]
        for span in spans[1:]:
            last = merged[-1]
            if span.start - last.end < pause:
                merged[-1] = DeleteSpan(
                    start=last.start,
                    end=max(last.end, span.end),
                    token_id=last.token_id,
                    cause="word",
                )
            else:
                merged.append(span)
        spans = merged

    spans.sort(key=lambda span: (span.start, span.end))
    return spans


# ----------------------------------------------------------------------
# 3. 全链路编排
# ----------------------------------------------------------------------
def run_pipeline(
    words: Sequence[Mapping[str, object]],
    samples: np.ndarray,
    sample_rate: int,
    deleted_ids: Iterable[str],
    output_dir: Union[str, Path],
    basename: str,
    audio_format: str = "wav",
    audio_params: Optional[Mapping[str, object]] = None,
) -> Dict[str, object]:
    """跑完整链路：对齐词表 → 建 EDL → 渲染音频 → 重建字幕 → 导出三件套。

    流程：

    1. :func:`load_tokens`：word dict -> Token；
    2. 校验 ``deleted_ids`` 全部存在于 Token 中，否则抛 ``ValueError``；
    3. :func:`build_deletes`：被删 Token -> DeleteSpan；
    4. 构造 :class:`model.EDL`（``source`` 用 ``basename`` 作为逻辑标识，
       ``sample_rate`` 用传入采样率）；
    5. :meth:`model.TimeMap.from_deletes`：由删除区间构造源 ↔ 目标映射；
    6. :func:`edl_render.compute_keep_segments` 预检，若删除后没有任何保留
       区间（全删光）则抛 ``ValueError``；
    7. :func:`edl_render.render`：渲染剪辑后的音频（新数组，不动 ``samples``）；
    7.5. :func:`audio_fx.apply`：在渲染副本上施加增益 / 标准化 / 淡入淡出
       （只改幅度，不改长度，不影响 EDL 与 TimeMap）；
    8. :func:`subtitle.rebuild_srt`：按目标时间轴重建 SRT；
    9. :func:`exporter.export_all`：写出 ``{basename}.wav`` /
       ``{basename}.srt`` / ``{basename}.edl.json``。

    **不变量 1**：``samples`` 全程只读，函数不返回也不修改它的任何视图。

    参数:
        words: faster-whisper 的 word dict 列表（字段见 :func:`load_tokens`）。
        samples: 源音频样本，一维数值数组（只读）；建议 float32 单声道。
        sample_rate: 采样率（Hz），正整数，必须与 ``samples`` 一致。
        deleted_ids: 被删除的 Token id 集合，可迭代；为 ``None`` 时视作空集合。
        output_dir: 输出目录，不存在时自动创建。
        basename: 输出文件主名（不含扩展名与路径分隔符）。
        audio_format: 导出音频格式（``wav`` / ``mp3`` / ``flac`` / ``m4a``）。
        audio_params: 可选音频后处理参数（增益 / 峰值标准化 / 首尾淡入淡出），
            形如 ``{"gain_db": 3.0, "normalize": true, "fade_in": 0.2}``；
            为 ``None`` 或全默认值时不做任何后处理。

    返回:
        字典，包含以下键：

        * ``token_count`` (int)：Token 总数；
        * ``deleted_count`` (int)：实际被删除的 Token 数（按 id 命中计）；
        * ``output_duration`` (float)：输出音频时长（秒，来自渲染后样本数）；
        * ``paths`` (Dict[str, Path])：``export_all`` 返回的三条绝对路径
          （``audio_path`` / ``srt_path`` / ``edl_path``）；
        * ``edl`` (EDL)：本次使用的编辑决策列表。

    异常:
        ValueError: ``deleted_ids`` 中有 id 不存在于 ``tokens``（错误信息列出
            缺失项）；删除后没有任何保留区间（输出音频为空）；或
            ``sample_rate`` / ``basename`` / word 时间戳等参数非法。
        TypeError: ``samples`` / ``edl`` / 文本等类型非法（由底层模块抛出）。
        OSError / RuntimeError: 落盘或转码失败（由 :mod:`exporter` 抛出）。
    """
    rate = _as_sample_rate(sample_rate)

    tokens = load_tokens(words, deleted_ids)
    ids: Set[str] = set(deleted_ids) if deleted_ids else set()

    # ---- 校验：deleted_ids 必须全部是已知 Token ----
    known_ids = {token.id for token in tokens}
    missing = sorted(ids - known_ids)
    if missing:
        raise ValueError(
            "deleted_ids 中有 {count} 个 id 不存在于 tokens：{ids}".format(
                count=len(missing), ids=_format_ids(missing)
            )
        )

    deleted_count = sum(1 for token in tokens if token.id in ids)

    # ---- 删除区间与 EDL ----
    deletes = build_deletes(tokens, ids)
    edl = EDL(source=_source_label(basename), sample_rate=rate, deletes=deletes)
    time_map = TimeMap.from_deletes(edl.deletes)

    # ---- 预检：全删光时输出没有有效音频，直接报错而不是写出静音 ----
    total_duration = _source_duration(samples, rate)
    if not edl_render.compute_keep_segments(edl.deletes, total_duration):
        raise ValueError(
            "删除后没有任何保留区间，输出音频为空："
            f"源时长 {total_duration:.3f}s，删除 {edl.total_deleted:.3f}s"
            f"（共 {deleted_count} 个 Token 被删）"
        )

    # ---- 渲染（只读 samples，返回新数组）----
    rendered = edl_render.render(samples, rate, edl.deletes)
    if np.asarray(rendered).size == 0:
        raise ValueError("渲染结果为空音频，无法导出")

    # ---- 音频后处理（增益 / 标准化 / 淡入淡出）----
    # 只改幅度、不改长度：EDL 与 TimeMap 不受影响，字幕时间轴保持一致。
    params = audio_fx.AudioParams.from_mapping(audio_params)
    processed = audio_fx.apply(rendered, rate, params) if not params.is_noop() else rendered
    output_duration = float(np.asarray(processed).size) / float(rate)

    # ---- 字幕（一律走 TimeMap，见不变量 6）----
    srt_text = subtitle.rebuild_srt(tokens, time_map, ids)

    # ---- 导出 ----
    paths = exporter.export_all(
        samples=processed,
        sample_rate=rate,
        output_dir=output_dir,
        basename=basename,
        edl=edl,
        srt_text=srt_text,
        audio_format=audio_format,
    )

    return {
        "token_count": len(tokens),
        "deleted_count": deleted_count,
        "output_duration": output_duration,
        "paths": paths,
        "edl": edl,
    }


# ----------------------------------------------------------------------
# 内部工具
# ----------------------------------------------------------------------
def _word_text(word: Mapping[str, object], index: int) -> str:
    """取 word 的文本字段并 strip；缺失时按空字符串处理。"""
    raw = word.get("word")
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise TypeError(
            f"words[{index}]['word'] 必须是 str，实际为 {type(raw).__name__}"
        )
    return raw.strip()


def _word_confidence(word: Mapping[str, object], index: int) -> float:
    """取 prob 字段作为置信度，缺失按 1.0，结果夹到 [0, 1]。"""
    raw = word.get("prob")
    if raw is None:
        return 1.0
    value = _as_finite_float(raw, f"words[{index}]['prob']")
    return min(max(value, 0.0), 1.0)


def _time_field(word: Mapping[str, object], name: str, index: int) -> float:
    """读取必填的时间字段，缺失或非法时抛出带下标的异常。"""
    value = _optional_time_field(word, name, index)
    if value is None:
        raise ValueError(
            f"words[{index}] 缺少字段 {name!r}，实际字段：{sorted(word.keys())!r}"
        )
    return value


def _optional_time_field(
    word: Mapping[str, object], name: str, index: int
) -> Optional[float]:
    """读取可选的时间字段；缺失或为 None 时返回 None，其余非法值抛异常。"""
    raw = word.get(name)
    if raw is None:
        return None
    return _as_finite_float(raw, f"words[{index}][{name!r}]")


def _as_finite_float(value: object, label: str) -> float:
    """把 int/float（含 numpy 标量）统一成 float，拒绝 bool、非数值、NaN、无穷大。"""
    if isinstance(value, bool) or not isinstance(
        value, (int, float, np.integer, np.floating)
    ):
        raise TypeError(f"{label} 必须是 int/float，实际为 {type(value).__name__}")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} 必须是有限数，实际为 {result!r}")
    return result


def _as_non_negative_float(value: object, label: str) -> float:
    """校验非负有限浮点数（用于 pause_merge 这类阈值参数）。"""
    result = _as_finite_float(value, label)
    if result < 0.0:
        raise ValueError(f"{label} 必须 >= 0，实际为 {result!r}")
    return result


def _as_sample_rate(sample_rate: object) -> int:
    """校验采样率：必须是正整数，拒绝 bool。"""
    if isinstance(sample_rate, bool) or not isinstance(
        sample_rate, (int, np.integer)
    ):
        raise TypeError(
            f"sample_rate 必须是 int，实际为 {type(sample_rate).__name__}"
        )
    rate = int(sample_rate)
    if rate <= 0:
        raise ValueError(f"sample_rate 必须为正整数，实际为 {rate!r}")
    return rate


def _source_duration(samples: np.ndarray, sample_rate: int) -> float:
    """源音频总时长（秒）：样本数 / 采样率，口径与渲染器一致。"""
    return float(np.asarray(samples).size) / float(sample_rate)


def _source_label(basename: str) -> str:
    """由 basename 派生 EDL.source 的逻辑标识（必须是非空 str）。"""
    if isinstance(basename, str) and basename.strip():
        return basename.strip()
    return _DEFAULT_SOURCE_NAME


def _format_ids(ids: Sequence[str]) -> str:
    """把缺失的 id 列表格式化成一段可读文本（过长时截断）。"""
    shown = [repr(item) for item in ids[:_MAX_LISTED_IDS]]
    text = ", ".join(shown)
    if len(ids) > _MAX_LISTED_IDS:
        text += f" …（共 {len(ids)} 个）"
    return text
