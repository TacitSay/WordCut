"""工程文件（.wcproj）持久化模块。

方案 A（见 docs/待办与推后事项.md）：工程文件只记录源文件路径与编辑状态，
不复制音频/视频。源文件被移动或替换时通过 SHA-256 校验并提示用户。

文件结构::

    {
      "format": "wordcut-project",
      "version": "1",
      "project_id": "...",
      "name": "...",
      "source": {
        "audio_path": "...", "audio_hash": "sha256:...",
        "original_path": "...", "original_kind": "video"|"audio",
        "sample_rate": 16000, "duration": 300.0
      },
      "words": [...], "deleted_ids": [...], "in_out": null,
      "audio_params": {"gain_db": 0.0, "normalize": false, "fade_in": 0.0, "fade_out": 0.0},
      "created_at": "...", "modified_at": "..."
    }

不变量：

- 不变量 1：源文件只读——本模块只读取源文件做 hash 校验，绝不改写。
- 不变量 2：时间单位为秒。
- 不变量 4：deleted_ids 只记录 id，不重排 Token。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Sequence

try:
    from model import Token
    from pipeline import load_tokens
except ImportError:  # pragma: no cover
    from .model import Token  # type: ignore[no-redef]
    from .pipeline import load_tokens  # type: ignore[no-redef]

__all__ = [
    "PROJECT_FORMAT",
    "PROJECT_VERSION",
    "PROJECT_SUFFIX",
    "build_project_doc",
    "save_project",
    "load_project",
    "hash_file",
]

PROJECT_FORMAT = "wordcut-project"
PROJECT_VERSION = "1"
PROJECT_SUFFIX = ".wcproj"

_CHUNK = 1 << 20


def hash_file(path: Path) -> str:
    """计算文件的 SHA-256，返回 sha256:<hex> 形式。分块读取，避免大文件占内存。"""
    path = Path(path)
    if not path.is_file():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _now_iso() -> str:
    """当前时间的 ISO-8601 字符串（秒级）。"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _duration_of(audio_path: Path) -> float:
    """用 soundfile 读取音频时长（秒）；失败返回 0.0。"""
    try:
        import soundfile as sf

        info = sf.info(str(audio_path))
        return float(info.frames) / float(info.samplerate) if info.samplerate else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def build_project_doc(project: Dict[str, Any]) -> Dict[str, Any]:
    """由后端内存中的项目字典生成工程文档。"""
    audio_path = Path(str(project.get("audio_path") or ""))
    if not audio_path.is_file():
        raise ValueError(f"源音频不存在，无法保存工程：{audio_path}")

    deleted_ids = project.get("deleted_ids") or set()
    words: Sequence[Dict[str, Any]] = project.get("words") or []

    return {
        "format": PROJECT_FORMAT,
        "version": PROJECT_VERSION,
        "project_id": str(project.get("id", "")),
        "name": str(project.get("name", "")),
        "source": {
            "audio_path": str(audio_path.resolve()),
            "audio_hash": hash_file(audio_path),
            "original_path": project.get("source_path"),
            "original_kind": str(project.get("source_kind", "audio")),
            "sample_rate": int(project.get("sample_rate", 0)),
            "duration": _duration_of(audio_path),
        },
        "words": [dict(item) for item in words],
        "deleted_ids": sorted(str(item) for item in deleted_ids),
        "audio_params": dict(project.get("audio_params") or {}),
        "in_out": project.get("in_out"),
        "created_at": str(project.get("created_at", "")),
        "modified_at": _now_iso(),
    }


def save_project(project: Dict[str, Any], path: Path) -> Dict[str, Any]:
    """把项目写成 .wcproj 文件（UTF-8 无 BOM），返回实际写入的文档。"""
    path = Path(path)
    if path.suffix.lower() != PROJECT_SUFFIX:
        path = path.with_suffix(PROJECT_SUFFIX)

    doc = build_project_doc(project)
    payload = json.dumps(doc, ensure_ascii=False, indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload + "\n", encoding="utf-8")
    return doc


def load_project(path: Path) -> Dict[str, Any]:
    """读取 .wcproj，返回可用于恢复会话的项目字典（含 integrity 校验结果）。"""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"工程文件不存在：{path}")

    doc = json.loads(path.read_text(encoding="utf-8"))
    return parse_project_doc(doc)


def parse_project_doc(doc: Any) -> Dict[str, Any]:
    """解析已经反序列化的工程文档（供文件路径与上传内容两条入口共用）。

    参数:
        doc: json.loads 之后的字典。

    返回:
        与 :func:`load_project` 相同的项目字典。

    异常:
        ValueError: 不是合法工程文件，或版本不支持。
    """
    if not isinstance(doc, dict) or doc.get("format") != PROJECT_FORMAT:
        raise ValueError(
            f"不是 WordCut 工程文件（缺少 format={PROJECT_FORMAT!r}）：{doc!r:.200}"
        )
    if str(doc.get("version")) != PROJECT_VERSION:
        raise ValueError(
            f"工程文件版本不支持：{doc.get('version')!r}（当前支持 {PROJECT_VERSION}）"
        )

    source = doc.get("source") or {}
    audio_path = Path(str(source.get("audio_path") or ""))
    audio_exists = audio_path.is_file()

    integrity = {"audio_exists": audio_exists, "audio_ok": False, "message": ""}
    if not audio_exists:
        integrity["message"] = f"源音频不存在：{audio_path}（请重新导入源文件）"
    else:
        expected = str(source.get("audio_hash") or "")
        actual = hash_file(audio_path)
        if expected and actual == expected:
            integrity["audio_ok"] = True
            integrity["message"] = "源音频校验通过"
        else:
            integrity["message"] = (
                "源音频已被修改或替换（hash 不匹配），删除点可能失效，请确认后再编辑"
            )

    words: List[Dict[str, Any]] = [dict(item) for item in (doc.get("words") or [])]
    deleted_ids = {str(item) for item in (doc.get("deleted_ids") or [])}

    tokens: List[Token] = []
    if words:
        try:
            tokens = load_tokens(words, deleted_ids)
        except (TypeError, ValueError):
            tokens = []

    return {
        "id": str(doc.get("project_id") or ""),
        "name": str(doc.get("name") or ""),
        "audio_path": str(audio_path),
        "sample_rate": int(source.get("sample_rate") or 0),
        "words": words,
        "deleted_ids": deleted_ids,
        "created_at": str(doc.get("created_at") or ""),
        "modified_at": str(doc.get("modified_at") or ""),
        "source_path": source.get("original_path"),
        "source_kind": str(source.get("original_kind") or "audio"),
        "audio_params": (
            dict(doc.get("audio_params"))
            if isinstance(doc.get("audio_params"), dict)
            else {}
        ),
        "in_out": doc.get("in_out"),
        "tokens": tokens,
        "integrity": integrity,
    }
