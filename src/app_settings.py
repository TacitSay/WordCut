# -*- coding: utf-8 -*-
"""应用设置（界面可改、跨会话保留）。

为什么单独一个文件而不是塞进 SQLite：设置项很少、结构简单，而且可能被用户
手工查看/备份；JSON 文件放在 ``data/settings.json`` 里最直观。

优先级约定：**环境变量 > 设置文件 > 内置默认值**。
环境变量面向"部署/实验"场景（脚本里临时改），设置文件面向"用户点界面"。

写盘用"临时文件 + 原子改名"，避免写到一半断电留下半个 JSON。
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

__all__ = [
    "BASE_DIR",
    "SETTINGS_PATH",
    "DEFAULT_SETTINGS",
    "load_settings",
    "get_section",
    "update_section",
    "reset_section",
]

#: 项目根目录（``src/`` 的上一级）。
BASE_DIR: Path = Path(__file__).resolve().parent.parent

#: 设置文件位置（可用 ``WORDCUT_SETTINGS_PATH`` 覆盖，便于测试隔离）。
SETTINGS_PATH: Path = Path(
    os.environ.get("WORDCUT_SETTINGS_PATH", "").strip()
    or (BASE_DIR / "data" / "settings.json")
)

#: 默认设置。新增字段一律写在这里，读旧文件时自动补齐。
DEFAULT_SETTINGS: Dict[str, Any] = {
    "segment": {
        # 分段模式：rule（规则）/ local（本地小模型）/ api（OpenAI 兼容接口）
        "mode": "rule",
        # 本地模型（HuggingFace 仓库名或本地目录，见 local_model_dir）
        "local_model": "Xenova/bge-small-zh-v1.5",
        # 已有的本地模型目录（填了就直接用，不再走仓库名下载）
        "local_model_dir": "",
        # 模型下载根目录（空 = <项目>/models）
        "models_dir": "",
        # 语义分段 API（OpenAI 兼容）
        "api_base": "",
        "api_key": "",
        "api_model": "",
        "api_timeout": 60,
    }
}

_lock = threading.Lock()


def _read_raw(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _deep_merge(base: Dict[str, Any], extra: Mapping[str, Any]) -> Dict[str, Any]:
    """把 ``extra`` 合并进 ``base`` 的副本（只合 dict，其余直接覆盖）。"""
    result = copy.deepcopy(base)
    for key, value in extra.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_settings() -> Dict[str, Any]:
    """读取完整设置（缺失项用默认值补齐）。"""
    with _lock:
        return _deep_merge(DEFAULT_SETTINGS, _read_raw(SETTINGS_PATH))


def get_section(name: str) -> Dict[str, Any]:
    """读取一节设置（例如 ``"segment"``）。"""
    section = load_settings().get(name)
    return dict(section) if isinstance(section, dict) else {}


def update_section(name: str, patch: Mapping[str, Any]) -> Dict[str, Any]:
    """更新一节设置并落盘，返回更新后的该节内容。

    参数:
        name: 节名（``segment``）。
        patch: 只包含要改的字段；未知字段会被保留（向前兼容）。

    异常:
        ValueError: patch 不是映射，或节名非法。
    """
    if not isinstance(patch, Mapping):
        raise ValueError("patch 必须是字典")
    if not name or not name.isidentifier():
        raise ValueError(f"非法的设置节名：{name!r}")

    with _lock:
        current = _deep_merge(DEFAULT_SETTINGS, _read_raw(SETTINGS_PATH))
        section = current.get(name)
        if not isinstance(section, dict):
            section = {}
        merged = _deep_merge(section, patch)
        current[name] = merged
        _write_atomic(current)
    return dict(merged)


def reset_section(name: str) -> Dict[str, Any]:
    """把某一节恢复默认值并落盘。"""
    with _lock:
        current = _deep_merge(DEFAULT_SETTINGS, _read_raw(SETTINGS_PATH))
        current[name] = copy.deepcopy(DEFAULT_SETTINGS.get(name, {}))
        _write_atomic(current)
    return dict(current.get(name) or {})


def _write_atomic(data: Dict[str, Any]) -> None:
    """原子写盘：先写临时文件，再 replace。"""
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", delete=False, dir=str(SETTINGS_PATH.parent), suffix=".tmp"
    )
    try:
        with handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        Path(handle.name).replace(SETTINGS_PATH)
    except Exception:
        Path(handle.name).unlink(missing_ok=True)
        raise


def settings_path() -> Path:
    """设置文件路径（供界面显示）。"""
    return SETTINGS_PATH


def public_section(name: str, secret_keys: Optional[tuple] = None) -> Dict[str, Any]:
    """给前端用的设置副本（隐藏密钥明文，只标记"是否已设置"）。"""
    section = get_section(name)
    for key in secret_keys or ():
        value = str(section.get(key) or "")
        section[key] = ""
        section[f"{key}_set"] = bool(value)
    return section
