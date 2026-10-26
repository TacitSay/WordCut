"""数据模型模块：Token / DeleteSpan / EDL / TimeMap。

纯标准库实现，兼容 Python 3.9+。
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

__all__ = [
    "Token",
    "DeleteSpan",
    "EDL",
    "TimeMap",
    "DELETE_CAUSES",
    "validate_interval",
]

# DeleteSpan.cause 的合法取值。
DELETE_CAUSES: Tuple[str, ...] = ("word", "pause", "repeat")

# target_time 撞车时注入的微偏移（秒），以及判等容差。
_EPSILON = 1e-9
_TOLERANCE = 1e-9


def validate_interval(start: float, end: float, label: str = "interval") -> None:
    """校验时间区间：类型合法、有限，且满足 0 <= start < end。"""
    if not isinstance(start, (int, float)) or isinstance(start, bool):
        raise TypeError(f"{label}: start 必须是 int/float，实际为 {type(start).__name__}")
    if not isinstance(end, (int, float)) or isinstance(end, bool):
        raise TypeError(f"{label}: end 必须是 int/float，实际为 {type(end).__name__}")
    start = float(start)
    end = float(end)
    if start != start or end != end:
        raise ValueError(f"{label}: start/end 不能为 NaN")
    if start in (float("inf"), float("-inf")) or end in (float("inf"), float("-inf")):
        raise ValueError(f"{label}: start/end 不能为无穷大")
    if start < 0:
        raise ValueError(f"{label}: start 必须 >= 0，实际为 {start!r}")
    if not start < end:
        raise ValueError(
            f"{label}: 必须满足 start < end，实际为 start={start!r}, end={end!r}"
        )


@dataclass(frozen=True)
class Token:
    """一个词元及其在源时间轴上的区间（单位：秒）。

    frozen dataclass 保证字段一经构造便不可修改，因此 ``id`` 生成后不可变；
    同时也不允许挂载新的属性。
    """

    id: str
    text: str
    start: float
    end: float
    is_deleted: bool = False
    confidence: float = 1.0

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("Token.id 必须是非空字符串")
        if not isinstance(self.text, str):
            raise TypeError("Token.text 必须是字符串")
        validate_interval(self.start, self.end, label=f"Token({self.id!r})")
        if isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float)):
            raise TypeError("Token.confidence 必须是数值")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError(
                f"Token.confidence 必须在 [0, 1] 内，实际为 {self.confidence!r}"
            )

    @property
    def duration(self) -> float:
        """区间时长（秒）。"""
        return self.end - self.start

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "text": self.text,
            "start": self.start,
            "end": self.end,
            "is_deleted": self.is_deleted,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Token":
        return cls(
            id=data["id"],
            text=data["text"],
            start=float(data["start"]),
            end=float(data["end"]),
            is_deleted=bool(data.get("is_deleted", False)),
            confidence=float(data.get("confidence", 1.0)),
        )


@dataclass(frozen=True)
class DeleteSpan:
    """需要从时间轴上删除的一段区间，cause 取值 word / pause / repeat。"""

    start: float
    end: float
    token_id: Optional[str] = None
    cause: str = "word"

    def __post_init__(self) -> None:
        validate_interval(self.start, self.end, label="DeleteSpan")
        if self.cause not in DELETE_CAUSES:
            raise ValueError(
                f"DeleteSpan.cause 必须是 {DELETE_CAUSES} 之一，实际为 {self.cause!r}"
            )
        if self.token_id is not None and not isinstance(self.token_id, str):
            raise TypeError("DeleteSpan.token_id 必须是 str 或 None")

    @property
    def duration(self) -> float:
        """区间时长（秒）。"""
        return self.end - self.start

    def to_dict(self) -> dict:
        return {
            "start": self.start,
            "end": self.end,
            "token_id": self.token_id,
            "cause": self.cause,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DeleteSpan":
        return cls(
            start=float(data["start"]),
            end=float(data["end"]),
            token_id=data.get("token_id"),
            cause=data.get("cause", "word"),
        )


@dataclass
class EDL:
    """编辑决策列表：记录源素材与全部删除区间。"""

    source: str
    sample_rate: int
    deletes: List[DeleteSpan] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not self.source:
            raise ValueError("EDL.source 必须是非空字符串")
        if isinstance(self.sample_rate, bool) or not isinstance(self.sample_rate, int):
            raise TypeError("EDL.sample_rate 必须是 int")
        if self.sample_rate <= 0:
            raise ValueError(f"EDL.sample_rate 必须为正整数，实际为 {self.sample_rate!r}")
        self.deletes = [_coerce_delete_span(item) for item in self.deletes]

    def add_delete(
        self,
        start: float,
        end: float,
        token_id: Optional[str] = None,
        cause: str = "word",
    ) -> DeleteSpan:
        """新增一个删除区间并返回它；参数非法时抛 ValueError / TypeError。"""
        span = DeleteSpan(start=start, end=end, token_id=token_id, cause=cause)
        self.deletes.append(span)
        return span

    def extend(self, spans: List[DeleteSpan]) -> None:
        """批量追加删除区间；有任何一个非法则不追加任何一项。"""
        checked = [_coerce_delete_span(item) for item in spans]
        self.deletes.extend(checked)

    @property
    def merged_deletes(self) -> List[DeleteSpan]:
        """按时间排序并合并重叠/首尾相接的删除区间。"""
        if not self.deletes:
            return []
        merged: List[DeleteSpan] = []
        for span in sorted(self.deletes, key=lambda s: (s.start, s.end)):
            if merged and span.start <= merged[-1].end:
                last = merged[-1]
                if span.end > last.end:
                    merged[-1] = DeleteSpan(
                        start=last.start,
                        end=span.end,
                        token_id=last.token_id,
                        cause=last.cause,
                    )
            else:
                merged.append(span)
        return merged

    @property
    def total_deleted(self) -> float:
        """删除总时长（秒，已去掉重叠部分）。"""
        return sum(span.duration for span in self.merged_deletes)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "sample_rate": self.sample_rate,
            "deletes": [span.to_dict() for span in self.deletes],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EDL":
        return cls(
            source=data["source"],
            sample_rate=int(data["sample_rate"]),
            deletes=[DeleteSpan.from_dict(item) for item in data.get("deletes", [])],
        )


@dataclass
class TimeMap:
    """源时间轴与目标时间轴之间的分段线性映射。

    ``anchors`` 是 (source_time, target_time) 断点序列：

    * source_time 严格递增；
    * target_time 非递减——相等表示源轴上那一整段被删除，在目标轴上收缩成一点；
      构造时会为等值 target 注入极小的偏移（每级 1e-9 秒），使映射整体严格
      单调、双向可逆，且区段端点取值精确，不受影响；
    * 区间内线性插值，区间外按最近的边界段线性外推。

    语义：目标时间 = 源时间 - 该时间点之前被删除的累计时长。
    """

    anchors: List[Tuple[float, float]] = field(default_factory=list)

    def __post_init__(self) -> None:
        normalized: List[Tuple[float, float]] = []
        for index, anchor in enumerate(self.anchors):
            if not isinstance(anchor, (tuple, list)) or len(anchor) != 2:
                raise ValueError(f"anchors[{index}] 必须是 (source_time, target_time) 二元组")
            src = _as_float(anchor[0], f"anchors[{index}][0]")
            dst = _as_float(anchor[1], f"anchors[{index}][1]")
            if src < 0:
                raise ValueError(f"anchors[{index}] 的 source_time 必须 >= 0，实际为 {src!r}")
            if normalized:
                prev_src, prev_dst = normalized[-1]
                if not src > prev_src:
                    raise ValueError(
                        f"anchors[{index}] 的 source_time 必须严格递增"
                        f"（前一个为 {prev_src!r}，当前为 {src!r}）"
                    )
                if dst < prev_dst - _TOLERANCE:
                    raise ValueError(
                        f"anchors[{index}] 的 target_time 必须非递减"
                        f"（前一个为 {prev_dst!r}，当前为 {dst!r}）"
                    )
            normalized.append((src, dst))
        self.anchors = _enforce_strict_target(normalized)

    # -- 构造辅助 ------------------------------------------------------
    @classmethod
    def identity(cls) -> "TimeMap":
        """恒等映射。"""
        return cls(anchors=[(0.0, 0.0), (1.0, 1.0)])

    @classmethod
    def from_deletes(cls, deletes: List[DeleteSpan]) -> "TimeMap":
        """由删除区间构造映射：每段删除把其后的目标时间整体前移该段时长。"""
        spans = sorted(deletes, key=lambda s: (s.start, s.end))
        if not spans:
            return cls.identity()

        merged: List[Tuple[float, float]] = []
        for span in spans:
            if merged and span.start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], span.end))
            else:
                merged.append((span.start, span.end))

        anchors: List[Tuple[float, float]] = [(0.0, 0.0)]
        removed = 0.0
        for del_start, del_end in merged:
            if del_start > anchors[-1][0]:
                anchors.append((del_start, del_start - removed))
            removed += del_end - del_start
            anchors.append((del_end, del_end - removed))
        # 末尾补一个断点，使最后一段之后的换算按“无删除”的斜率继续。
        last_src = anchors[-1][0]
        anchors.append((last_src + 1.0, last_src + 1.0 - removed))
        return cls(anchors=anchors)

    # -- 双向换算 ------------------------------------------------------
    def source_to_target(self, source_time: float) -> float:
        """源时间轴（秒）-> 目标时间轴（秒）。"""
        return _interpolate(self.anchors, _as_float(source_time, "source_time"), 0)

    def target_to_source(self, target_time: float) -> float:
        """目标时间轴（秒）-> 源时间轴（秒）。"""
        return _interpolate(self.anchors, _as_float(target_time, "target_time"), 1)

    # -- 便捷方法 ------------------------------------------------------
    def map_interval(self, start: float, end: float) -> Tuple[float, float]:
        """把一个源区间整体映射到目标时间轴。"""
        validate_interval(start, end, label="map_interval")
        mapped_start = self.source_to_target(start)
        mapped_end = self.source_to_target(end)
        if not mapped_start < mapped_end:
            raise ValueError(
                f"映射后区间非法：start={mapped_start!r}, end={mapped_end!r}"
            )
        return mapped_start, mapped_end

    def to_dict(self) -> dict:
        return {"anchors": [[src, dst] for src, dst in self.anchors]}

    @classmethod
    def from_dict(cls, data: dict) -> "TimeMap":
        return cls(
            anchors=[(float(item[0]), float(item[1])) for item in data.get("anchors", [])]
        )


# ----------------------------------------------------------------------
# 内部工具
# ----------------------------------------------------------------------
def _coerce_delete_span(item: object) -> DeleteSpan:
    """把 dict 转成 DeleteSpan；其余非法类型抛 TypeError。"""
    if isinstance(item, DeleteSpan):
        return item
    if isinstance(item, dict):
        return DeleteSpan.from_dict(item)
    raise TypeError(
        f"删除区间必须是 DeleteSpan 或 dict，实际为 {type(item).__name__}"
    )


def _as_float(value: object, label: str) -> float:
    """把 int/float 统一成 float，拒绝 bool、非数值、NaN 与无穷大。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{label} 必须是 int/float，实际为 {type(value).__name__}")
    result = float(value)
    if result != result:
        raise ValueError(f"{label} 不能为 NaN")
    if result in (float("inf"), float("-inf")):
        raise ValueError(f"{label} 不能为无穷大")
    return result


def _enforce_strict_target(
    anchors: List[Tuple[float, float]],
) -> List[Tuple[float, float]]:
    """让 target_time 严格递增。

    相邻断点的 target 相等时（源轴上那段被完全删除），把后一个断点的
    target 抬高一个极小量：被删区间在目标轴上收缩成一段几乎为零长的
    陡坡，坡内连续、整体严格单调可逆，两端之外的取值不受影响。
    """
    result: List[Tuple[float, float]] = []
    peak = float("-inf")
    for src, dst in anchors:
        if dst > peak:
            result.append((src, dst))
            peak = dst
        else:
            peak = peak + _EPSILON
            result.append((src, peak))
    return result


def _interpolate(
    anchors: List[Tuple[float, float]],
    value: float,
    src_index: int,
) -> float:
    """在 anchors 上做分段线性映射；src_index 指明输入轴（0=源，1=目标）。"""
    if not anchors:
        return value

    xs = [anchor[src_index] for anchor in anchors]
    ys = [anchor[1 - src_index] for anchor in anchors]

    if len(xs) == 1:
        # 只有一个断点：按 1:1 平移。
        return value + (ys[0] - xs[0])

    if value <= xs[0]:
        index = 0
    elif value >= xs[-1]:
        index = len(xs) - 2
    else:
        index = min(bisect_right(xs, value) - 1, len(xs) - 2)
        index = max(index, 0)

    x0, x1 = xs[index], xs[index + 1]
    y0, y1 = ys[index], ys[index + 1]
    slope = (y1 - y0) / (x1 - x0)
    return y0 + slope * (value - x0)
