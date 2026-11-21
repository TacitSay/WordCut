# -*- coding: utf-8 -*-
"""本地模型目录管理：扫描可用的模型、清理废弃模型。

为什么需要：

* **选择靠读文件夹**（用户要求）：与其让用户手填 HuggingFace 仓库名，
  不如直接扫描 `models/` 下真实存在的目录，选一个就用；
* **清理旧模型**：早期尝试过 mT5/Pegasus（依赖 sentencepiece，本机崩溃）
  和 ONNX 版 BGE，这些目录留着只占空间、还会在界面上误导用户。

判断"是不是一个可用的本地模型目录"只看关键文件是否存在，不做联网。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = [
    "MODELS_ROOT",
    "LocalModel",
    "scan_models",
    "remove_model",
    "DEPRECATED_DIRS",
]

#: 模型根目录。
MODELS_ROOT: Path = Path(__file__).resolve().parent.parent / "models"

#: 早期尝试过的模型目录 —— 已确认不可用，可安全删除。
#:
#: * `summarizer-mt5-zh`：mT5/Pegasus 需要 sentencepiece 读 spiece.model，
#:   本机 `sp.Load()` 触发 Windows 访问违规（0xC0000005）；
#: * `semantic-Xenova__*`：ONNX 向量模型，已由 Qwen 摘要模型取代；
#: * `summarizer-qwen05b`：0.5B 版，已被 1.5B 取代。
DEPRECATED_DIRS: tuple = (
    "summarizer-mt5-zh",
    "summarizer-qwen05b",
)

#: 判断目录"是不是模型"的关键文件组，命中任一即可。
_MODEL_SIGNATURES = (
    ("config.json", "tokenizer.json"),          # HF transformers（BPE）
    ("config.json", "model.safetensors"),        # safetensors 权重
    ("config.json", "pytorch_model.bin"),        # 老式 bin 权重
    ("config.bin",),                             # faster-whisper 的 CTranslate2
    ("model.bin", "config.json"),                # faster-whisper
)

#: 各类模型的用途判定（按目录名/文件推断）。
_KIND_HINTS = (
    ("whisper", "语音转写"),
    ("semantic", "语义分段"),
    ("summarizer", "段落标题"),
    ("bge", "语义向量"),
    ("qwen", "段落标题"),
)


@dataclass
class LocalModel:
    """扫描到的本地模型。"""

    name: str
    path: str
    size_mb: float
    kind: str
    detail: str
    #: 是否是早期废弃的模型（界面上标红，建议删除）
    deprecated: bool = False
    #: 是否包含可用的权重文件
    ready: bool = False
    files: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "size_mb": round(self.size_mb, 1),
            "kind": self.kind,
            "detail": self.detail,
            "deprecated": self.deprecated,
            "ready": self.ready,
            "files": self.files,
        }


#: 已废弃模型的**目录名前缀**（带版本后缀的也能匹配上）。
_DEPRECATED_PREFIXES: tuple = (
    "summarizer-mt5",     # sentencepiece 在本机崩溃
    "summarizer-qwen05b",  # 0.5B 已被 1.5B 取代
    "semantic-Xenova",     # ONNX 向量模型，已被摘要模型取代
    "semantic-",           # 其它旧语义向量模型
)


def _is_deprecated(name: str) -> bool:
    """判断目录名是否是已废弃模型。"""
    if name in DEPRECATED_DIRS:
        return True
    return any(name.startswith(prefix) for prefix in _DEPRECATED_PREFIXES)


def _dir_size_mb(path: Path) -> float:
    total = 0
    try:
        for item in path.rglob("*"):
            if item.is_file():
                total += item.stat().st_size
    except OSError:
        return 0.0
    return total / 1048576


def _is_model_dir(path: Path) -> bool:
    """目录里是否有模型关键文件。"""
    names = {item.name for item in path.iterdir() if item.is_file()} if path.is_dir() else set()
    for signature in _MODEL_SIGNATURES:
        if all(name in names for name in signature):
            return True
    # 允许一层子目录（例如 onnx/model.onnx 那种布局）
    for child in path.iterdir() if path.is_dir() else []:
        if child.is_dir():
            sub = {item.name for item in child.iterdir() if item.is_file()}
            if sub & {"model.onnx", "model_quantized.onnx"}:
                return True
    return False


def _guess_kind(name: str, files: List[str]) -> str:
    """按目录名与文件推断用途。"""
    lowered = name.lower()
    for hint, label in _KIND_HINTS:
        if hint in lowered:
            return label
    if "model.safetensors" in files or "pytorch_model.bin" in files:
        return "未知模型"
    return "未知"


def scan_models(root: Optional[Path] = None) -> List[Dict[str, Any]]:
    """扫描模型根目录，返回所有模型目录的信息。

    参数:
        root: 模型根目录，默认 :data:`MODELS_ROOT`。

    返回:
        按名称排序的字典列表；目录不存在时返回空列表。
    """
    base = Path(root) if root else MODELS_ROOT
    if not base.is_dir():
        return []

    results: List[LocalModel] = []
    for entry in sorted(base.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        files = sorted(item.name for item in entry.iterdir() if item.is_file())
        ready = _is_model_dir(entry)
        size_mb = _dir_size_mb(entry)
        deprecated = _is_deprecated(entry.name)
        kind = _guess_kind(entry.name, files)

        detail_parts = [f"{size_mb:.0f} MB"]
        if not ready:
            detail_parts.append("缺少权重文件")
        if deprecated:
            detail_parts.append("已废弃，建议删除")
        results.append(
            LocalModel(
                name=entry.name,
                path=str(entry),
                size_mb=size_mb,
                kind=kind,
                detail=" · ".join(detail_parts),
                deprecated=deprecated,
                ready=ready,
                files=files[:12],
            )
        )
    return [item.to_dict() for item in results]


def remove_model(name: str, root: Optional[Path] = None) -> Dict[str, Any]:
    """删除一个模型目录。

    **安全约束**：只允许删除 :data:`MODELS_ROOT` 的直接子目录，
    拒绝任何带路径分隔符或 `..` 的名字，避免误删别的目录。

    参数:
        name: 目录名（不含路径）。
        root: 模型根目录。

    返回:
        ``{"removed": bool, "name": str, "freed_mb": float, "error": str}``。
    """
    base = Path(root) if root else MODELS_ROOT
    if not name or "/" in name or "\\" in name or ".." in name:
        return {"removed": False, "name": name, "freed_mb": 0.0, "error": "非法目录名"}

    target = (base / name)
    try:
        target = target.resolve()
        base_resolved = base.resolve()
        if target.parent != base_resolved:
            return {
                "removed": False,
                "name": name,
                "freed_mb": 0.0,
                "error": "只允许删除模型根目录下的直接子目录",
            }
    except OSError as exc:
        return {"removed": False, "name": name, "freed_mb": 0.0, "error": str(exc)}

    if not target.is_dir():
        return {"removed": False, "name": name, "freed_mb": 0.0, "error": "目录不存在"}

    freed = _dir_size_mb(target)
    try:
        shutil.rmtree(target)
    except OSError as exc:
        return {"removed": False, "name": name, "freed_mb": 0.0, "error": str(exc)}

    return {"removed": True, "name": name, "freed_mb": round(freed, 1), "error": ""}
