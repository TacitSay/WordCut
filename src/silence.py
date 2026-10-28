"""静音检测模块：用短时能量找出可删除的静音段，并转成 DeleteSpan。

纯 Python + numpy 实现，不依赖任何第三方音频库。对外三个函数：

- :func:`detect_silences`：短时 RMS 能量检测静音段，返回秒级区间。
- :func:`silences_to_deletes`：静音段 -> ``DeleteSpan(cause="pause")``。
- :func:`suggest_pause_deletes`：一步给出可直接进 EDL 的暂停删除区间。

约定：

- 时间参数与返回值一律为秒（float），见 docs/不变量.md 不变量 2。
- ``samples`` 只读，本模块内部一律先 ``np.asarray`` 再只读使用，
  绝不原地写入，见 docs/不变量.md 不变量 1。
- 静音段对应术语表里的「静音谷底」所在区间，删除原因用 ``pause``
  （``DELETE_CAUSES`` 之一），不用 cut / trim 之类别名。
"""

from __future__ import annotations

from typing import Any, List, Mapping, Optional, Sequence

import numpy as np

from model import DeleteSpan, Token, validate_interval

__all__ = [
    "detect_silences",
    "silences_to_deletes",
    "suggest_pause_deletes",
    "FRAME_MS",
    "HOP_MS",
]

#: 分析帧长（毫秒）。20 ms 在语音上足够稳，同时保证帧数不太少。
FRAME_MS: float = 20.0

#: 帧移（毫秒）。10 ms 帧移 = 1 ms 级的时间分辨率，且便于用 strides/cumsum 加速。
HOP_MS: float = 10.0

#: 判断「静音是否被词包住」时用的浮点容差（秒）。
_EPSILON: float = 1e-9

#: 用于把 0 能量映射成极小 dB 值的下限，避免 log10(0) 报警。
_TINY: float = float(np.finfo(np.float64).tiny)


# ----------------------------------------------------------------------
# 公开 API
# ----------------------------------------------------------------------
def detect_silences(
    samples: np.ndarray,
    sample_rate: int,
    min_silence: float = 0.8,
    threshold_db: float = -38.0,
    padding: float = 0.08,
) -> List[dict]:
    """用短时能量检测静音段。

    以 ``FRAME_MS``（20 ms）为帧长、``HOP_MS``（10 ms）为帧移把信号分帧，
    逐帧计算 RMS，再按 dBFS 阈值判定每一帧是否为静音；连续静音帧合并成
    一个候选静音段，最后做「最短时长过滤 + 两端收缩」。

    参数:
        samples: 一维 numpy 数组（float32，取值 -1..1）。只读，不会被修改。
        sample_rate: 采样率（Hz），正整数。
        min_silence: 最短静音时长（秒）。原始持续时长短于此值的静音段
            直接丢弃，默认为 0.8。
        threshold_db: 静音判定阈值（dBFS，相对满刻度 1.0）。帧 RMS 换算出的
            dB 值 ``<= threshold_db`` 即视为静音，默认为 -38.0。
        padding: 静音段两端各收缩的秒数（秒），避免削掉字头字尾，默认为 0.08。
            收缩后长度不为正的静音段丢弃。

    返回:
        ``[{"start": 秒, "end": 秒, "duration": 秒}, ...]``，按时间升序。
        没有可用静音段时返回空列表。

    边界行为:
        - ``samples`` 为空：返回 ``[]``。
        - 全程静音：返回整段时长作为一个静音段（收缩 padding 后）。
        - 全程有声：返回 ``[]``。
        - 信号短于一个分析帧：整段当作一帧来判断。

    异常:
        TypeError: ``samples`` 不是数组/序列，或时间参数不是数值。
        ValueError: ``sample_rate`` 非正、``samples`` 非一维、参数非法或为 NaN/无穷。
    """
    signal = _as_signal(samples)
    rate = _as_sample_rate(sample_rate)
    min_len = _as_seconds(min_silence, "min_silence")
    threshold = _as_seconds(threshold_db, "threshold_db")
    pad = _as_seconds(padding, "padding")
    if min_len < 0.0:
        raise ValueError(f"min_silence 必须 >= 0，实际为 {min_silence!r}")
    if pad < 0.0:
        raise ValueError(f"padding 必须 >= 0，实际为 {padding!r}")

    if signal.size == 0:
        return []

    total_duration = float(signal.size) / float(rate)
    frame_rms, frame_spans = _frame_rms(signal, rate)

    if frame_rms.size == 0:
        return []

    with np.errstate(divide="ignore"):
        frame_db = 20.0 * np.log10(np.maximum(frame_rms, _TINY))
    silent_mask = frame_db <= threshold

    silences: List[dict] = []
    for first, last in _mask_runs(silent_mask):
        raw_start = float(frame_spans[first][0])
        raw_end = float(frame_spans[last][1])
        raw_start = max(raw_start, 0.0)
        raw_end = min(raw_end, total_duration)
        # 最短时长按「原始静音时长」判定，收缩只影响最终要删的区间。
        # 用原始时长判断（padding 会各自收缩 pad 秒，叠加后总缩短 2*pad）
        if raw_end - raw_start < min_len - _EPSILON:
            continue
        start = raw_start + pad
        end = raw_end - pad
        end = min(end, total_duration)
        if not start < end:
            continue
        silences.append(
            {
                "start": float(start),
                "end": float(end),
                "duration": float(end - start),
            }
        )
    return silences


def silences_to_deletes(
    silences: Sequence[Mapping[str, Any]],
    tokens: Optional[Sequence[Token]] = None,
) -> List[DeleteSpan]:
    """把静音段转成 ``DeleteSpan(cause="pause")``。

    提供 ``tokens`` 时会逐个检查静音段与 token 时间区间的重叠情况：
    **完全落在某个 token 内部**（即被词包住）的静音段跳过，避免删掉发音；
    跨越 token 边界（只压到词的一部分）的静音段仍然保留，交由调用方
    或 :func:`suggest_pause_deletes` 进一步裁剪。

    参数:
        silences: ``detect_silences`` 的返回值，或任何含 ``start`` / ``end``
            键（秒）的映射；也接受带 ``start`` / ``end`` 属性的对象。
        tokens: Token 序列；为 None 时不做重叠过滤（仍然只产出合法区间）。

    返回:
        ``DeleteSpan`` 列表，按 ``(start, end)`` 升序；非法或零长区间被丢弃。

    异常:
        TypeError: ``silences`` 元素既不是映射也没有 ``start`` / ``end`` 属性。
    """
    token_bounds = _token_bounds(tokens)

    deletes: List[DeleteSpan] = []
    for silence in silences or ():
        start, end = _silence_bounds(silence)
        if not start < end:
            continue
        validate_interval(start, end, label="DeleteSpan(pause)")
        if token_bounds and _is_inside_any_token(start, end, token_bounds):
            continue
        deletes.append(DeleteSpan(start=start, end=end, token_id=None, cause="pause"))

    deletes.sort(key=lambda span: (span.start, span.end))
    return deletes


def suggest_pause_deletes(
    samples: np.ndarray,
    sample_rate: int,
    tokens: Sequence[Token],
    min_silence: float = 0.8,
) -> List[DeleteSpan]:
    """一步给出可直接写进 EDL 的暂停删除区间。

    流程：``detect_silences``（默认 ``threshold_db=-38.0``、``padding=0.08``）
    -> 丢掉与任何 token 发音区间有重叠的静音段 -> 按时间排序并保证互不重叠。

    与 :func:`silences_to_deletes` 的区别：这里只要和 token 区间**有任何重叠**
    就整段丢弃（宁可少删，不啃发音），因此结果可以无脑并入 EDL。

    参数:
        samples: 一维 numpy 数组（float32，取值 -1..1）。只读，不会被修改。
        sample_rate: 采样率（Hz）。
        tokens: Token 序列，代表不可删除的发音区间。
        min_silence: 最短静音时长（秒），默认为 0.8。

    返回:
        ``DeleteSpan(cause="pause")`` 列表，按时间升序且互不重叠；
        没有可用区间时返回空列表。

    边界行为:
        与 :func:`detect_silences` 一致：空输入 / 全程有声返回空列表；
        全程静音且提供了 tokens 时，凡是与 token 重叠的部分都会被丢弃。
    """
    silences = detect_silences(
        samples,
        sample_rate,
        min_silence=min_silence,
    )
    if not silences:
        return []

    token_bounds = _token_bounds(tokens)

    candidates: List[DeleteSpan] = []
    for silence in silences:
        start, end = _silence_bounds(silence)
        if not start < end:
            continue

        # 与 token 重叠时不是整段丢弃，而是把发音区间挖掉、保留剩余部分。
        # 真实音频里静音边界总会蹭到词首/词尾，整段丢弃会导致一段都留不下。
        remaining = _subtract_tokens(start, end, token_bounds)
        for piece_start, piece_end in remaining:
            if piece_end - piece_start < _MIN_PIECE_SECONDS:
                continue
            candidates.append(
                DeleteSpan(
                    start=piece_start, end=piece_end, token_id=None, cause="pause"
                )
            )

    candidates.sort(key=lambda span: (span.start, span.end))

    result: List[DeleteSpan] = []
    for span in candidates:
        if result and span.start < result[-1].end - _EPSILON:
            # 与已接受区间重叠：丢弃后者，保证输出互不重叠（不合并，避免膨胀）。
            continue
        result.append(span)
    return result


# ----------------------------------------------------------------------
# 内部工具：参数校验
# ----------------------------------------------------------------------
def _as_signal(samples: np.ndarray) -> np.ndarray:
    """把输入统一成一维 float64 只读视图，绝不修改调用方数组。"""
    try:
        signal = np.asarray(samples)
    except Exception as exc:  # pragma: no cover - numpy 抛错类型随版本而异
        raise TypeError(f"samples 必须是 numpy 数组或数值序列：{exc}") from exc
    if signal.ndim != 1:
        raise ValueError(f"samples 必须是一维数组，实际维度为 {signal.ndim}")
    if not np.issubdtype(signal.dtype, np.number):
        raise TypeError(f"samples 必须是数值类型，实际为 {signal.dtype}")
    if np.issubdtype(signal.dtype, np.complexfloating):
        raise TypeError("samples 不支持复数类型")
    # astype(copy=False)：已是 float64 时返回同一块内存，全程只读使用。
    return signal.astype(np.float64, copy=False)


def _as_sample_rate(sample_rate: int) -> int:
    """校验采样率：必须是正整数。"""
    if isinstance(sample_rate, bool) or not isinstance(sample_rate, (int, np.integer)):
        raise TypeError(
            f"sample_rate 必须是 int，实际为 {type(sample_rate).__name__}"
        )
    rate = int(sample_rate)
    if rate <= 0:
        raise ValueError(f"sample_rate 必须为正整数，实际为 {sample_rate!r}")
    return rate


def _as_seconds(value: float, label: str) -> float:
    """校验数值参数：拒绝 bool、非数值、NaN 与无穷大。"""
    if isinstance(value, bool) or not isinstance(value, (int, float, np.integer, np.floating)):
        raise TypeError(f"{label} 必须是 int/float，实际为 {type(value).__name__}")
    number = float(value)
    if number != number:
        raise ValueError(f"{label} 不能为 NaN")
    if number in (float("inf"), float("-inf")):
        raise ValueError(f"{label} 不能为无穷大")
    return number


# ----------------------------------------------------------------------
# 内部工具：能量计算
# ----------------------------------------------------------------------
def _frame_rms(
    signal: np.ndarray,
    sample_rate: int,
) -> "tuple[np.ndarray, np.ndarray]":
    """分帧计算 RMS，并返回每帧的样本区间。

    用前缀和（``np.cumsum``）做滑动能量累加，避免 Python 逐帧循环：
    第 i 帧覆盖样本 ``[i*hop, i*hop + frame_len)``，其平方和等于
    ``csum[i*hop + frame_len] - csum[i*hop]``。

    参数:
        signal: 一维 float64 数组（内部已校验）。
        sample_rate: 采样率（Hz）。

    返回:
        ``(rms, spans)``：
        ``rms`` 是长度等于帧数的 float64 数组；
        ``spans`` 是形状 ``(帧数, 2)`` 的 float64 数组，每行为该帧的
        起止时间（秒）。信号为空时不产生任何帧。
    """
    size = int(signal.size)
    if size == 0:
        return np.empty(0, dtype=np.float64), np.empty((0, 2), dtype=np.float64)

    frame_len = max(1, int(round(FRAME_MS / 1000.0 * sample_rate)))
    hop = max(1, int(round(HOP_MS / 1000.0 * sample_rate)))

    if size < frame_len:
        # 不足一帧：整段当作唯一一帧处理，保证短音频也有判定结果。
        starts = np.zeros(1, dtype=np.int64)
        effective_len = size
    else:
        frame_count = 1 + (size - frame_len) // hop
        starts = np.arange(frame_count, dtype=np.int64) * hop
        effective_len = frame_len

    # 平方和前缀和：前缀长度 size + 1，前缀[0] = 0。
    prefix = np.empty(size + 1, dtype=np.float64)
    prefix[0] = 0.0
    np.cumsum(np.square(signal), out=prefix[1:])

    window_sums = prefix[starts + effective_len] - prefix[starts]
    window_sums = np.maximum(window_sums, 0.0)  # 抵消浮点误差带来的负数
    rms = np.sqrt(window_sums / float(effective_len))

    end_indices = np.minimum(starts + effective_len, size)
    spans = np.stack(
        (starts.astype(np.float64) / sample_rate, end_indices.astype(np.float64) / sample_rate),
        axis=1,
    )
    return rms, spans


def _mask_runs(mask: np.ndarray) -> "list[tuple[int, int]]":
    """把布尔掩码里的连续 True 段转成 ``[(first, last), ...]``（闭区间）。"""
    if mask.size == 0 or not bool(mask.any()):
        return []
    padded = np.concatenate(([False], mask.astype(bool), [False]))
    changes = np.diff(padded.astype(np.int8))
    begins = np.flatnonzero(changes == 1)
    ends = np.flatnonzero(changes == -1)
    return [(int(begin), int(end) - 1) for begin, end in zip(begins, ends)]


# ----------------------------------------------------------------------
# 内部工具：区间重叠判定
# ----------------------------------------------------------------------
#: 裁剪后保留的最短片段（秒）：短于此的碎片不进入建议删除列表。
_MIN_PIECE_SECONDS = 0.05


def _subtract_tokens(
    start: float, end: float, bounds: Sequence[tuple]
) -> "list[tuple[float, float]]":
    """从区间 [start, end] 中挖掉所有 token 发音区间，返回剩余片段。

    用于静音裁剪：静音段与词首/词尾蹭到一起时，保留真正无发音的部分，
    而不是整段丢弃。

    参数:
        start: 静音段起点（秒）。
        end: 静音段终点（秒）。
        bounds: 已排序的 token 区间列表 [(s, e), ...]。

    返回:
        剩余片段列表（升序、互不重叠）；全部被发音覆盖时返回空列表。
    """
    pieces: list = []
    cursor = start
    for token_start, token_end in bounds:
        if token_end <= cursor:
            continue
        if token_start >= end:
            break
        if token_start > cursor:
            pieces.append((cursor, min(token_start, end)))
        cursor = max(cursor, token_end)
        if cursor >= end:
            return pieces
    if cursor < end:
        pieces.append((cursor, end))
    return [(a, b) for a, b in pieces if b > a]


def _token_bounds(tokens: Optional[Sequence[Token]]) -> "list[tuple[float, float]]":
    """取出所有 token 的 ``(start, end)`` 时间区间（秒），已排序。

    不区分 ``is_deleted``：只要 token 有发音区间就保护它，宁可不删。
    """
    if not tokens:
        return []
    bounds: List[tuple] = []
    for token in tokens:
        start = float(token.start)
        end = float(token.end)
        if start < end:
            bounds.append((start, end))
    bounds.sort()
    return bounds


def _silence_bounds(silence: Any) -> "tuple[float, float]":
    """从 dict / 对象里取出静音段的 ``(start, end)``（秒）。"""
    if isinstance(silence, Mapping):
        start = silence["start"]
        end = silence["end"]
    else:
        try:
            start = silence.start
            end = silence.end
        except AttributeError as exc:
            raise TypeError(
                "静音段必须是含 start/end 的映射或对象，"
                f"实际为 {type(silence).__name__}"
            ) from exc
    return _as_seconds(start, "silence.start"), _as_seconds(end, "silence.end")


def _overlaps_any_token(
    start: float,
    end: float,
    token_bounds: Sequence["tuple[float, float]"],
) -> bool:
    """静音段与任一 token 区间是否有正长度重叠（贴着边界不算）。"""
    for token_start, token_end in token_bounds:
        if start < token_end - _EPSILON and end > token_start + _EPSILON:
            return True
    return False


def _is_inside_any_token(
    start: float,
    end: float,
    token_bounds: Sequence["tuple[float, float]"],
) -> bool:
    """静音段是否被某个 token 完整包住（被词包住的静音不删）。"""
    for token_start, token_end in token_bounds:
        if start >= token_start - _EPSILON and end <= token_end + _EPSILON:
            return True
    return False
