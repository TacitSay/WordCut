# -*- coding: utf-8 -*-
"""faster-whisper 模型的定位与自动下载。

为什么单独成模块：

* 模型（`small` 约 460MB）不入库，仓库里只有代码。克隆后第一次转写必须能
  自己把模型拿下来，否则"能跑起来"这件事就依赖一份没写在文档里的手工操作；
* 国内直连 huggingface.co 经常超时，所以失败后要能自动改用镜像重试；
* 下载逻辑需要被两个地方复用：API 运行时（第一次点"转写"）和
  ``scripts/ensure_models.py``（提前预热，便于离线部署）。

目录约定与官方 faster-whisper 一致：``models/faster-whisper-<名字>/``，
里面有 ``model.bin`` / ``config.json`` / ``tokenizer.json`` / ``vocabulary.txt``。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

#: 默认模型：小模型在中文播客上质量够用，460MB 也不算下载负担。
DEFAULT_MODEL: str = "small"

#: 可选模型名（faster-whisper / CTranslate2 支持的官方尺寸）。
KNOWN_MODELS = (
    "tiny",
    "tiny.en",
    "base",
    "base.en",
    "small",
    "small.en",
    "medium",
    "medium.en",
    "large-v2",
    "large-v3",
    "distil-small.en",
    "distil-medium.en",
    "distil-large-v3",
)

#: HuggingFace 仓库模板（Systran 发布的 CTranslate2 转换版）。
REPO_TEMPLATE = "Systran/faster-whisper-{name}"

#: 国内镜像；官方源失败时自动重试。
HF_MIRROR = "https://hf-mirror.com"

#: 各模型的大致体积（仅用于提示，下载内容以仓库为准）。
MODEL_SIZE_HINT = {
    "tiny": "75MB",
    "base": "145MB",
    "small": "460MB",
    "medium": "1.5GB",
    "large-v2": "3.0GB",
    "large-v3": "3.0GB",
}

#: 判断"模型目录已经可用"的必需文件。
REQUIRED_FILES = ("model.bin", "config.json")

#: 项目根目录（``src/`` 的上一级）。
BASE_DIR: Path = Path(__file__).resolve().parent.parent

#: 下载完成的标志文件：避免"下到一半"的目录被当成可用模型。
_READY_MARKER = ".download-complete"


class ModelNotAvailable(RuntimeError):
    """本地没有模型，且自动下载失败（或已被禁用）。"""


def models_root() -> Path:
    """模型根目录：``WORDCUT_MODELS_DIR`` 优先，否则 ``<项目根>/models``。"""
    override = os.environ.get("WORDCUT_MODELS_DIR", "").strip()
    return Path(override) if override else BASE_DIR / "models"


def model_name() -> str:
    """要使用的模型名：``WORDCUT_WHISPER_MODEL`` 优先，默认 ``small``。"""
    return os.environ.get("WORDCUT_WHISPER_MODEL", "").strip() or DEFAULT_MODEL


def local_dir(name: Optional[str] = None, root: Optional[Path] = None) -> Path:
    """模型应放置的本地目录（``models/faster-whisper-<name>``）。"""
    base = Path(root) if root else models_root()
    return base / f"faster-whisper-{name or model_name()}"


def is_ready(path: Path) -> bool:
    """判断目录里是否是一份完整可用的模型。"""
    if not path.is_dir():
        return False
    return all((path / name).is_file() for name in REQUIRED_FILES)


def explicit_path() -> Optional[Path]:
    """``WORDCUT_WHISPER_MODEL_PATH`` 指定的目录（存在才返回）。"""
    raw = os.environ.get("WORDCUT_WHISPER_MODEL_PATH", "").strip()
    if not raw:
        return None
    path = Path(raw)
    return path if is_ready(path) else None


def search_paths(name: Optional[str] = None, root: Optional[Path] = None) -> List[Path]:
    """按优先级列出可能存放模型的目录（不含自动下载）。

    ``root`` 用于调用方显式指定的模型根目录（例如预热到别的盘）；
    省略时用 :func:`models_root`。
    """
    root = Path(root) if root else models_root()
    paths: List[Path] = [
        # 标准放法：models/faster-whisper-<名字>
        root / f"faster-whisper-{name or model_name()}",
        # 兼容"把模型文件直接解压到 models/ 下"的放法
        root,
    ]
    # 去重保序
    seen: set[str] = set()
    unique: List[Path] = []
    for path in paths:
        key = str(path.resolve()).lower() if path.exists() else str(path).lower()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def find_local(
    name: Optional[str] = None, root: Optional[Path] = None
) -> Optional[Path]:
    """已有的可用模型目录；没有则返回 ``None``。

    ``root`` 指定在哪个根目录里找（默认 :func:`models_root`）。显式指定的
    ``WORDCUT_WHISPER_MODEL_PATH`` 优先级最高。
    """
    override = explicit_path()
    if override is not None:
        return override
    for path in search_paths(name, root):
        if is_ready(path):
            return path.resolve()
    return None


# ----------------------------------------------------------------------
# 下载（子进程执行，便于切换 HF_ENDPOINT 镜像）
# ----------------------------------------------------------------------
_DOWNLOAD_SCRIPT = r"""
import sys
from huggingface_hub import snapshot_download

repo_id, target_dir = sys.argv[1], sys.argv[2]
snapshot_download(
    repo_id=repo_id,
    local_dir=target_dir,
    allow_patterns=["*.bin", "*.json", "*.txt"],
)
print("ok")
"""


def _snapshot_download(
    repo_id: str,
    target_dir: Path,
    endpoint: Optional[str],
    timeout: int = 3600,
) -> None:
    """在子进程里执行下载。

    为什么用子进程：``huggingface_hub`` 在 import 时就把 ``HF_ENDPOINT`` 读进了
    常量，进程内改环境变量不会生效；换镜像只能新起一个进程。
    """
    env = dict(os.environ)
    if endpoint:
        env["HF_ENDPOINT"] = endpoint
    env.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

    result = subprocess.run(
        [sys.executable, "-c", _DOWNLOAD_SCRIPT, repo_id, str(target_dir)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=timeout,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        tail = " / ".join(detail[-3:]) if detail else "未知错误"
        raise RuntimeError(tail)


def ensure_model(
    name: Optional[str] = None,
    root: Optional[Path] = None,
    allow_download: bool = True,
    log: Optional[object] = None,
) -> Path:
    """确保模型可用，返回目录；必要时自动下载。

    参数:
        name: 模型名（默认取 :func:`model_name`）。
        root: 模型根目录（默认 :func:`models_root`）。
        allow_download: 是否允许联网下载（``WORDCUT_NO_MODEL_DOWNLOAD=1``
            也会强制关闭）。
        log: 可选的 ``print`` 风格回调。

    返回:
        可用的模型目录（绝对路径）。

    异常:
        ModelNotAvailable: 本地没有、且下载被禁用或失败。
    """
    say = log or (lambda *_args, **_kwargs: None)
    resolved_name = name or model_name()

    existing = find_local(resolved_name, root)
    if existing is not None:
        return existing

    target = local_dir(resolved_name, root)

    disabled = os.environ.get("WORDCUT_NO_MODEL_DOWNLOAD", "").strip() == "1"
    if not allow_download or disabled:
        tried = "、".join(str(path) for path in search_paths(resolved_name, root))
        raise ModelNotAvailable(
            f"未找到模型且自动下载已关闭（已尝试：{tried}）。"
            f"可手动下载 {REPO_TEMPLATE.format(name=resolved_name)} 到 {target}"
        )

    repo_id = REPO_TEMPLATE.format(name=resolved_name)
    target.mkdir(parents=True, exist_ok=True)
    size_hint = MODEL_SIZE_HINT.get(resolved_name, "")
    say(
        f"本地没有模型，开始下载 {repo_id} → {target}"
        + (f"（约 {size_hint}，首次较慢）" if size_hint else "（首次较慢）")
    )

    errors: List[str] = []
    # 先试官方源；不通再换国内镜像
    for endpoint, label in ((None, "huggingface.co"), (HF_MIRROR, HF_MIRROR)):
        try:
            say(f"  通过 {label} 下载…")
            _snapshot_download(repo_id, target, endpoint)
            if not is_ready(target):
                raise RuntimeError("下载完成但缺少 model.bin / config.json")
            (target / _READY_MARKER).write_text(label, encoding="utf-8")
            say(f"  下载完成：{target}")
            return target.resolve()
        except Exception as exc:  # noqa: BLE001 - 换下一个源再试
            errors.append(f"{label}: {exc}")
            say(f"  失败（{exc}），换下一个源")

    raise ModelNotAvailable(
        "模型自动下载失败：" + "；".join(errors) + f"。可手动下载 {repo_id} 到 {target}"
    )


if __name__ == "__main__":  # pragma: no cover - 手动排查用
    print(ensure_model(log=print))
