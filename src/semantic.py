# -*- coding: utf-8 -*-
"""语义分段：把「句」按语义合并成「段」。

为什么需要：纯规则分段（标点 + 停顿 + 时长）只能保证"不过长"，无法判断
"这几句在讲同一件事"。词级对齐的转写结果天生带大量短句，直接按标点断段会
得到一堆两三句的碎段；而段落是阅读与导出的基本单位，碎段会让文本流和字幕
文件都难用。

提供两种后端，编码逻辑一致、只换"判断边界"的方式：

* **本地小模型**（默认）：``BAAI/bge-small-zh-v1.5`` 之类的中文向量模型，
  对每个句子求句向量，相邻句相似度低的地方就是语义断点。模型约 100MB，
  纯 CPU 也能跑；
* **API**：把编号后的句子交给任意 OpenAI 兼容的 chat completions 接口，
  让它返回应该断段的序号。质量最好，但需要网络与 key。

两种后端都只做"在哪里断"，**不改变任何词与时间戳**；出现任何异常都回退到
规则分段，保证功能可用（不变量：分段永远不该让转写结果丢失）。

内存纪律：本地模型按需加载、空闲超时会 ``unload()``（见 :func:`maybe_unload`），
避免长期占用 200–300MB 常驻内存。
"""

from __future__ import annotations

import gc
import json
import math
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import app_settings
from model import Token
from segmenter import (
    RuleSegmenter,
    Segmenter,
    _ends_with_hard_punct,
)

__all__ = [
    "STRATEGY_RULE",
    "STRATEGY_SEMANTIC",
    "STRATEGY_SEMANTIC_LOCAL",
    "STRATEGY_SEMANTIC_API",
    "STRATEGIES",
    "SemanticSegmenter",
    "build_segmenter",
    "strategy_available",
    "local_model_status",
    "unload_local_model",
    "maybe_unload_local",
    "describe_strategies",
]

#: 纯规则（默认，零依赖、零内存）。
STRATEGY_RULE = "rule"
#: 语义：有 API 配置就用 API，否则用本地小模型。
STRATEGY_SEMANTIC = "semantic"
#: 强制本地小模型。
STRATEGY_SEMANTIC_LOCAL = "semantic_local"
#: 强制 API。
STRATEGY_SEMANTIC_API = "semantic_api"

STRATEGIES: Tuple[str, ...] = (
    STRATEGY_RULE,
    STRATEGY_SEMANTIC,
    STRATEGY_SEMANTIC_LOCAL,
    STRATEGY_SEMANTIC_API,
)

#: 默认本地向量模型仓库（Xenova 提供的 ONNX 版 BGE-small-zh，量化后约 25MB）。
DEFAULT_LOCAL_MODEL = "Xenova/bge-small-zh-v1.5"

#: 模型落在 ``models/semantic-<名字>/`` 下。
SEMANTIC_DIR_PREFIX = "semantic-"

#: 本地模型空闲多久后卸载（秒）。0 表示不自动卸载。
DEFAULT_IDLE_SECONDS = 600.0

#: 语义分段的段落长度约束（字符数），用于挑选断点。
DEFAULT_TARGET_CHARS = 180
DEFAULT_MIN_CHARS = 45
DEFAULT_MAX_CHARS = 600


# ----------------------------------------------------------------------
# 环境变量
# ----------------------------------------------------------------------
def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def segment_settings() -> Dict[str, Any]:
    """语义分段设置（``data/settings.json`` 的 ``segment`` 节）。"""
    try:
        return app_settings.get_section("segment")
    except Exception:  # noqa: BLE001 - 设置读不出来就用默认
        return {}


def local_model_name() -> str:
    """本地向量模型名：环境变量 > 设置 > 默认。"""
    env = os.environ.get("WORDCUT_SEMANTIC_MODEL", "").strip()
    if env:
        return env
    return str(segment_settings().get("local_model") or "").strip() or DEFAULT_LOCAL_MODEL


def local_model_dir_override() -> Optional[Path]:
    """设置里指定的"已有的本地模型目录"（填了就不再按仓库名下载）。"""
    raw = str(segment_settings().get("local_model_dir") or "").strip()
    if not raw:
        env = os.environ.get("WORDCUT_SEMANTIC_MODEL_DIR", "").strip()
        raw = env
    if not raw:
        return None
    path = Path(raw).expanduser()
    return path if path.is_dir() else None


def idle_seconds() -> float:
    """本地模型空闲卸载阈值（秒），``WORDCUT_SEMANTIC_IDLE_SECONDS``。"""
    return max(0.0, _env_float("WORDCUT_SEMANTIC_IDLE_SECONDS", DEFAULT_IDLE_SECONDS))


def api_config() -> Dict[str, str]:
    """读取 OpenAI 兼容接口配置；未配置完整时返回空字典。

    支持的环境变量::

        WORDCUT_SEMANTIC_API_BASE   例如 https://api.openai.com/v1
        WORDCUT_SEMANTIC_API_KEY    密钥（Bearer）
        WORDCUT_SEMANTIC_API_MODEL  模型名，例如 gpt-4o-mini
    """
    section = segment_settings()
    base = (
        os.environ.get("WORDCUT_SEMANTIC_API_BASE", "").strip()
        or str(section.get("api_base") or "").strip()
    ).rstrip("/")
    key = (
        os.environ.get("WORDCUT_SEMANTIC_API_KEY", "").strip()
        or str(section.get("api_key") or "").strip()
    )
    model = (
        os.environ.get("WORDCUT_SEMANTIC_API_MODEL", "").strip()
        or str(section.get("api_model") or "").strip()
    )
    if base and model:
        return {"base": base, "key": key, "model": model}
    return {}


def strategy_available(strategy: str) -> bool:
    """该策略当前是否真的能用（用于界面提示）。"""
    if strategy == STRATEGY_RULE:
        return True
    if strategy == STRATEGY_SEMANTIC_API:
        return bool(api_config())
    if strategy == STRATEGY_SEMANTIC_LOCAL:
        return True  # 依赖装好就能用（模型首次下载）
    if strategy == STRATEGY_SEMANTIC:
        return True
    return False


def describe_strategies() -> List[Dict[str, Any]]:
    """给前端/健康检查用的策略清单。"""
    api = api_config()
    return [
        {
            "id": STRATEGY_RULE,
            "name": "规则分段",
            "detail": "标点 / 停顿 / 时长，零依赖",
            "available": True,
        },
        {
            "id": STRATEGY_SEMANTIC,
            "name": "语义分段（自动）",
            "detail": "有 API 用 API，否则用本地小模型",
            "available": True,
        },
        {
            "id": STRATEGY_SEMANTIC_LOCAL,
            "name": "语义分段（本地模型）",
            "detail": f"{local_model_name()}，首次需下载约 100MB",
            "available": True,
        },
        {
            "id": STRATEGY_SEMANTIC_API,
            "name": "语义分段（API）",
            "detail": f"{api['model']} @ {api['base']}" if api else "需配置 API（BASE/KEY/MODEL）",
            "available": bool(api),
        },
    ]


# ----------------------------------------------------------------------
# 本地向量模型（惰性加载 + 空闲卸载）
# ----------------------------------------------------------------------
_local_lock = threading.Lock()
_local_state: Dict[str, Any] = {
    "model": None,
    "tokenizer": None,
    "device": "",
    "model_name": "",
    "last_used": 0.0,
    "loads": 0,
    "unloads": 0,
    "error": "",
}


def local_model_status() -> Dict[str, Any]:
    """本地模型当前状态（不含张量引用，便于直接 JSON 化）。"""
    with _local_lock:
        loaded = _local_state["model"] is not None
        return {
            "strategy": "local",
            "loaded": loaded,
            "model": _local_state["model_name"] or local_model_name(),
            "device": _local_state["device"] if loaded else "",
            "idle_seconds": round(time.time() - _local_state["last_used"], 1) if loaded else 0.0,
            "loads": _local_state["loads"],
            "unloads": _local_state["unloads"],
            "error": _local_state["error"],
        }


def unload_local_model() -> bool:
    """卸载本地向量模型并回收显存/内存；未加载时返回 ``False``。"""
    with _local_lock:
        if _local_state["model"] is None:
            return False
        _local_state["model"] = None
        _local_state["tokenizer"] = None
        _local_state["device"] = ""
        _local_state["unloads"] += 1
    gc.collect()
    # 注意：这里**不能** import torch。为了调一次 empty_cache 而导入 torch，
    # 会额外吃掉几百 MB 内存——比刚刚释放的模型还多（实测卸载后 RSS 反而从
    # 96MB 涨到 372MB）。只在 torch 已经被别处加载时才顺手清显存。
    torch_module = sys.modules.get("torch")
    if torch_module is not None:
        try:
            if torch_module.cuda.is_available():
                torch_module.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass
    # Windows 的 CRT 堆不会主动把释放的页还给系统，_heapmin 能真正把 RSS 压回去
    try:
        import ctypes

        ctypes.CDLL("msvcrt")._heapmin()
    except Exception:  # noqa: BLE001 - 非 Windows / 不支持就算了
        pass
    return True


def maybe_unload_local(now: Optional[float] = None) -> bool:
    """空闲超时则卸载；返回是否真的卸载了。"""
    timeout = idle_seconds()
    if timeout <= 0:
        return False
    with _local_lock:
        loaded = _local_state["model"] is not None
        idle = (now or time.time()) - _local_state["last_used"]
    if loaded and idle >= timeout:
        return unload_local_model()
    return False


#: 需要下载的文件（只取推理必需的，避免把整个仓库拉下来）。
_ONNX_PATTERNS = [
    "onnx/model_quantized.onnx",
    "onnx/model.onnx",
    "tokenizer.json",
    "config.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
]

_DOWNLOAD_SCRIPT = r"""
import sys
from huggingface_hub import snapshot_download

repo_id, target_dir = sys.argv[1], sys.argv[2]
patterns = sys.argv[3].split(",")
snapshot_download(repo_id=repo_id, local_dir=target_dir, allow_patterns=patterns)
print("ok")
"""


def semantic_model_dir() -> Path:
    """本地语义模型目录。

    优先级：设置的 ``local_model_dir`` > 设置的 ``models_dir`` > ``models/``。
    """
    override = local_model_dir_override()
    if override is not None:
        return override

    from model_fetch import models_root

    section = segment_settings()
    custom_root = str(section.get("models_dir") or "").strip()
    root = Path(custom_root).expanduser() if custom_root else models_root()
    safe = local_model_name().replace("/", "__")
    return root / f"{SEMANTIC_DIR_PREFIX}{safe}"


def _pick_onnx_file(model_dir: Path) -> Path:
    """挑一个可用的 onnx 文件（优先量化版）。"""
    for relative in ("onnx/model_quantized.onnx", "onnx/model.onnx"):
        candidate = model_dir / relative
        if candidate.is_file():
            return candidate
    found = sorted(model_dir.rglob("*.onnx"))
    if not found:
        raise RuntimeError(
            f"模型目录里没有 .onnx 文件：{model_dir}（可删掉该目录让它重新下载）"
        )
    return found[0]


def ensure_onnx_model(log: Optional[Any] = None) -> Path:
    """确保本地向量模型就绪，必要时自动下载（官方源失败切国内镜像）。

    返回:
        模型目录（含 ``onnx/*.onnx`` 与 ``tokenizer.json``）。

    异常:
        RuntimeError: 下载失败。
    """
    say = log or (lambda *_a, **_k: None)
    target = semantic_model_dir()
    try:
        _pick_onnx_file(target)
        if (target / "tokenizer.json").is_file():
            return target
    except RuntimeError:
        pass

    # 指定了"已有模型目录"却内容不全：直接给出明确错误，不要去下载覆盖用户的目录
    if local_model_dir_override() is not None:
        raise RuntimeError(
            f"指定的模型目录 {target} 里没有可用的 onnx + tokenizer.json，"
            "请检查设置里的「本地模型目录」"
        )

    if os.environ.get("WORDCUT_NO_MODEL_DOWNLOAD", "").strip() == "1":
        raise RuntimeError(
            f"本地语义模型缺失且已禁止下载（目录 {target}）；"
            "可设置 WORDCUT_NO_MODEL_DOWNLOAD=0 或改用 rule 分段"
        )

        import sys as _sys

    repo = local_model_name()
    target.mkdir(parents=True, exist_ok=True)
    patterns = ",".join(_ONNX_PATTERNS)
    say(f"下载语义模型 {repo} → {target}（量化版约 25MB）")

    errors: List[str] = []
    for endpoint, label in ((None, "huggingface.co"), ("https://hf-mirror.com", "hf-mirror.com")):
        env = dict(os.environ)
        if endpoint:
            env["HF_ENDPOINT"] = endpoint
        env.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
        result = subprocess.run(
            [_sys.executable, "-c", _DOWNLOAD_SCRIPT, repo, str(target), patterns],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=1800,
        )
        if result.returncode == 0:
            say(f"  下载完成（{label}）")
            return target
        tail = (result.stderr or result.stdout or "").strip().splitlines()
        errors.append(f"{label}: {tail[-1] if tail else '未知错误'}")
        say(f"  {label} 失败，换下一个源")

    raise RuntimeError("语义模型下载失败：" + "；".join(errors))


def _ensure_local_model() -> Tuple[Any, Any, str]:
    """按需加载本地向量模型（ONNX Runtime），返回 ``(session, tokenizer, device)``。

    为什么用 ONNX 而不是 transformers：

    * 本项目只需要"句向量"这一件事，`onnxruntime` 已经在依赖里（批处理转写要用），
      不必把 torch 拉进来——torch 光 import 就要 ~500MB 常驻内存；
    * ONNX 的 BGE-small 量化版只有 ~25MB，进程峰值内存约 200MB；
    * 卸载时只需丢掉 session，回收干净、无显存碎片。
    """
    with _local_lock:
        if _local_state["model"] is not None:
            _local_state["last_used"] = time.time()
            return _local_state["model"], _local_state["tokenizer"], _local_state["device"]

    name = local_model_name()
    model_dir = ensure_onnx_model()
    onnx_path = _pick_onnx_file(model_dir)

    try:
        import onnxruntime as ort
        from tokenizers import Tokenizer
    except ImportError as exc:  # pragma: no cover - 依赖缺失
        raise RuntimeError(
            f"缺少 onnxruntime / tokenizers，无法使用本地语义模型：{exc}"
        ) from exc

    tokenizer_file = model_dir / "tokenizer.json"
    if not tokenizer_file.is_file():
        raise RuntimeError(f"模型目录缺少 tokenizer.json：{model_dir}")

    providers = ["CPUExecutionProvider"]
    device = "cpu"
    if "CUDAExecutionProvider" in ort.get_available_providers():
        # 句向量是小模型，CPU 已经够快；只有显式开启才走 GPU，避免白占显存
        if os.environ.get("WORDCUT_SEMANTIC_ON_GPU", "").strip() == "1":
            providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
            device = "cuda"

    options = ort.SessionOptions()
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    options.intra_op_num_threads = max(1, min(4, os.cpu_count() or 1))
    session = ort.InferenceSession(str(onnx_path), sess_options=options, providers=providers)
    tokenizer = Tokenizer.from_file(str(tokenizer_file))

    with _local_lock:
        _local_state.update(
            model=session,
            tokenizer=tokenizer,
            device=device,
            model_name=name,
            last_used=time.time(),
            loads=_local_state["loads"] + 1,
            error="",
        )
    return session, tokenizer, device


def embed_sentences(sentences: Sequence[str], batch_size: int = 16) -> List[List[float]]:
    """对句子求 L2 归一化的句向量（均值池化）。

    参数:
        sentences: 句子列表（空串会被替换成占位符，避免模型报错）。
        batch_size: 批大小；句子多时批处理更省时间。

    返回:
        与输入等长的向量列表。
    """
    if not sentences:
        return []

    import numpy as np

    session, tokenizer, _device = _ensure_local_model()
    pad_id = 0
    try:
        pad_id = int(tokenizer.token_to_id("[PAD]") or 0)
    except Exception:  # noqa: BLE001
        pad_id = 0

    input_names = {item.name for item in session.get_inputs()}
    vectors: List[List[float]] = []

    for start in range(0, len(sentences), max(1, batch_size)):
        batch = [
            text if text.strip() else "。"
            for text in sentences[start : start + batch_size]
        ]
        encodings = tokenizer.encode_batch(batch)
        # 截断到 512，避免长句爆显存/内存
        ids = [enc.ids[:512] for enc in encodings]
        width = max(1, max(len(item) for item in ids))
        input_ids = np.full((len(ids), width), pad_id, dtype=np.int64)
        attention = np.zeros((len(ids), width), dtype=np.int64)
        for row, item in enumerate(ids):
            input_ids[row, : len(item)] = item
            attention[row, : len(item)] = 1

        feeds: Dict[str, Any] = {"input_ids": input_ids, "attention_mask": attention}
        if "token_type_ids" in input_names:
            feeds["token_type_ids"] = np.zeros_like(input_ids)

        hidden = session.run(None, feeds)[0]  # [B, T, H]
        mask = attention[..., None].astype(np.float32)
        summed = (hidden * mask).sum(axis=1)
        counts = np.clip(mask.sum(axis=1), 1e-6, None)
        pooled = summed / counts
        norms = np.clip(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-6, None)
        vectors.extend((pooled / norms).tolist())

    with _local_lock:
        _local_state["last_used"] = time.time()
    return vectors


# ----------------------------------------------------------------------
# 断点选择（与后端无关，两者共用）
# ----------------------------------------------------------------------
def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """两个已归一化向量的余弦相似度。"""
    total = 0.0
    for x, y in zip(a, b):
        total += float(x) * float(y)
    return total


def choose_boundaries(
    lengths: Sequence[int],
    scores: Sequence[float],
    eligible: Sequence[bool],
    target_chars: int = DEFAULT_TARGET_CHARS,
    min_chars: int = DEFAULT_MIN_CHARS,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> List[int]:
    """在句子之间挑选断点。

    思路：先算出每两句之间的"语义落差"``scores[i]``（越大越像换话题），
    再按总字数估算需要断几刀，从**允许断**的位置里挑落差最大的那几个，
    并且保证每段字数落在 ``[min_chars, max_chars]``。

    参数:
        lengths: 每句字数。
        scores: 长度 ``len(lengths)-1`` 的落差序列（第 i 项 = 第 i 句与第 i+1 句之间）。
        eligible: 同长度布尔序列，表示该位置是否允许断（例如必须以句末标点结尾）。
        target_chars: 目标段落字数，用来估算断几刀。
        min_chars / max_chars: 段落字数上下限。

    返回:
        升序的断点索引列表（在第 i 项后断开）。空列表表示合成一段。
    """
    count = len(lengths)
    if count <= 1:
        return []

    total = sum(lengths)
    desired = max(1, int(round(total / max(1, target_chars)))) - 1
    desired = min(desired, count - 1)

    candidates = [(scores[i], i) for i in range(count - 1) if eligible[i]]
    if not candidates:
        return []

    # 如果所有落差的差别都很小，说明没有明显的语义边界：
    # 这时候硬切只会切在句子中间，不如整段留着。
    values = [score for score, _ in candidates]
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    if math.sqrt(variance) < 0.02:
        return []
    candidates.sort(key=lambda item: item[0], reverse=True)

    chosen: List[int] = []
    filled = 0
    for _score, index in candidates:
        if len(chosen) >= desired:
            break
        trial = sorted(chosen + [index])
        if _respects_lengths(lengths, trial, min_chars, max_chars):
            chosen = trial
    return sorted(chosen)


def _respects_lengths(
    lengths: Sequence[int],
    boundaries: Sequence[int],
    min_chars: int,
    max_chars: int,
) -> bool:
    """检查一组断点是否把所有段落的字数都控制在范围内。"""
    start = 0
    for index in list(boundaries) + [len(lengths) - 1]:
        chunk = sum(lengths[start : index + 1])
        if chunk < min_chars or chunk > max_chars:
            return False
        start = index + 1
    return True


def _enforce_max_chars(
    boundaries: Sequence[int], lengths: Sequence[int], max_chars: int
) -> List[int]:
    """硬性保证：任何段落超过 ``max_chars`` 都要断开（即使语义上还连得紧）。

    语义分数只负责"哪里更适合断"，不保证长度；一段 2000 字的稿件在界面上没法读，
    所以这里按字数兜底。
    """
    if max_chars <= 0 or not lengths:
        return sorted(set(boundaries))

    keep = set(boundaries)
    accumulated = 0
    for index, length in enumerate(lengths[:-1]):
        accumulated += length
        if accumulated >= max_chars:
            keep.add(index)
            accumulated = 0
    return sorted(keep)


def _regroup(
    sentences: Sequence[List[Token]], boundaries: Sequence[int]
) -> List[List[Token]]:
    """按断点把「句子 Token 列表」合并成「段落 Token 列表」。"""
    groups: List[List[Token]] = []
    start = 0
    for index in list(boundaries) + [len(sentences) - 1]:
        merged: List[Token] = []
        for sentence in sentences[start : index + 1]:
            merged.extend(sentence)
        if merged:
            groups.append(merged)
        start = index + 1
    return groups


# ----------------------------------------------------------------------
# 分段器
# ----------------------------------------------------------------------
@dataclass
class SemanticSegmenter:
    """语义分段器：用向量落差或 API 判断在哪些句子之间断段。

    参数:
        strategy: 见 :data:`STRATEGIES`。
        target_chars / min_chars / max_chars: 段落长度约束（字符）。
        fallback: 出错时是否退回规则分段（默认 True）。
    """

    strategy: str = STRATEGY_SEMANTIC
    target_chars: int = DEFAULT_TARGET_CHARS
    min_chars: int = DEFAULT_MIN_CHARS
    max_chars: int = DEFAULT_MAX_CHARS
    fallback: bool = True
    _rule: RuleSegmenter = field(default_factory=RuleSegmenter, repr=False)
    last_error: str = ""
    last_backend: str = ""

    def __post_init__(self) -> None:
        if self.strategy not in STRATEGIES:
            raise ValueError(f"未知策略 {self.strategy!r}，可选：{'/'.join(STRATEGIES)}")

    # -- 主入口 --------------------------------------------------------
    def segment(self, tokens: Sequence[Token]) -> List[List[Token]]:
        """切段；任何异常都回退到规则分段（保证可用）。"""
        base = self._rule.segment(tokens)
        self.last_error = ""
        self.last_backend = "rule"

        if self.strategy == STRATEGY_RULE or len(base) < 3:
            return base

        try:
            if self.strategy == STRATEGY_SEMANTIC_API:
                boundaries, backend = self._boundaries_api(base), "api"
            elif self.strategy == STRATEGY_SEMANTIC_LOCAL:
                boundaries, backend = self._boundaries_local(base), "local"
            else:  # semantic：有 API 走 API，否则本地
                if api_config():
                    boundaries, backend = self._boundaries_api(base), "api"
                else:
                    boundaries, backend = self._boundaries_local(base), "local"
            self.last_backend = backend
            if not boundaries:
                return base
            return _regroup(base, boundaries)
        except Exception as exc:  # noqa: BLE001 - 分段失败不该让转写结果不可用
            self.last_error = str(exc)
            if self.fallback:
                return base
            raise

    # -- 本地向量 ------------------------------------------------------
    def _boundaries_local(self, sentences: Sequence[List[Token]]) -> List[int]:
        texts = ["".join(token.text for token in group).strip() for group in sentences]
        vectors = embed_sentences(texts)
        if len(vectors) != len(texts):
            raise RuntimeError("向量数量与句子数量不一致")

        scores: List[float] = []
        for index in range(len(vectors) - 1):
            similarity = _cosine(vectors[index], vectors[index + 1])
            score = 1.0 - similarity  # 落差越大越可能换话题
            # 句末有标点的位置更"像"段落边界，给一点加成；
            # 但**不强制要求标点**——whisper 转写经常没有标点，
            # 早期版本就是这么写着写着发现语义分段完全不生效的。
            if _ends_with_hard_punct(texts[index]):
                score += 0.05
            scores.append(score)

        lengths = [max(1, len(text)) for text in texts]
        eligible = [True] * (len(texts) - 1)
        boundaries = choose_boundaries(
            lengths,
            scores,
            eligible,
            target_chars=self.target_chars,
            min_chars=self.min_chars,
            max_chars=self.max_chars,
        )
        return _enforce_max_chars(boundaries, lengths, self.max_chars)

    # -- API -----------------------------------------------------------
    def _boundaries_api(self, sentences: Sequence[List[Token]]) -> List[int]:
        config = api_config()
        if not config:
            raise RuntimeError("未配置语义 API（需要 WORDCUT_SEMANTIC_API_BASE / _MODEL）")

        texts = ["".join(token.text for token in group).strip() for group in sentences]
        numbered = "\n".join(f"{index + 1}. {text}" for index, text in enumerate(texts))
        prompt = (
            "你是中文播客稿件的编辑，负责把句子合并成段落。\n"
            f"下面是 {len(texts)} 个编号句子：\n{numbered}\n\n"
            "请只输出一个 JSON 数组，元素是「应该在此句之后换段」的序号；"
            "不要输出任何解释。若整段连读更好，输出空数组 []。\n"
            f"要求：每段大约 {self.target_chars} 字，不要出现少于 "
            f"{self.min_chars} 字或超过 {self.max_chars} 字的段落。"
        )

        payload = {
            "model": config["model"],
            "temperature": 0,
            "messages": [
                {"role": "system", "content": "你是严谨的中文文本分段助手，只输出 JSON。"},
                {"role": "user", "content": prompt},
            ],
        }
        request = urllib.request.Request(
            f"{config['base']}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {config['key']}"} if config["key"] else {}),
            },
            method="POST",
        )
        timeout = _env_int("WORDCUT_SEMANTIC_API_TIMEOUT", 60)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.URLError as exc:
            raise RuntimeError(f"语义 API 请求失败：{exc}") from exc

        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"语义 API 返回结构异常：{body}") from exc

        indices = _parse_boundary_json(content, len(texts))
        # 再用长度约束过滤一遍，避免模型给出过碎/过长的段落
        lengths = [max(1, len(text)) for text in texts]
        kept: List[int] = []
        for index in indices:
            if _respects_lengths(lengths, sorted(kept + [index]), self.min_chars, self.max_chars):
                kept = sorted(kept + [index])
        return kept


def _parse_boundary_json(content: str, total: int) -> List[int]:
    """从模型回复里提取断点序号（1 基 -> 0 基），并做合法性校验。"""
    text = (content or "").strip()
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end <= start:
        raise RuntimeError(f"语义 API 未返回 JSON 数组：{text[:80]}")
    try:
        raw = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"语义 API 返回的 JSON 无法解析：{exc}") from exc

    indices: List[int] = []
    for item in raw if isinstance(raw, list) else []:
        try:
            index = int(item) - 1  # 模型用的是句子序号（1 基）
        except (TypeError, ValueError):
            continue
        if 0 <= index < total - 1 and index not in indices:
            indices.append(index)
    return sorted(indices)


def build_segmenter(strategy: Optional[str] = None, **kwargs: Any) -> Segmenter:
    """按名字构造分段器；``None``/未知值回退规则分段。

    参数:
        strategy: :data:`STRATEGIES` 中的一项。
        **kwargs: 透传给 :class:`SemanticSegmenter`（target_chars 等）。
    """
    name = (strategy or STRATEGY_RULE).strip().lower()
    if name not in STRATEGIES:
        name = STRATEGY_RULE
    if name == STRATEGY_RULE:
        return RuleSegmenter()
    return SemanticSegmenter(strategy=name, **kwargs)


# ----------------------------------------------------------------------
# 可选的本地模型（界面下拉用）
# ----------------------------------------------------------------------
#: 常用中文向量模型：仓库名 -> 说明。都是 Xenova 的 ONNX 版，量化后体积小。
AVAILABLE_LOCAL_MODELS: Tuple[Dict[str, str], ...] = (
    {
        "id": "Xenova/bge-small-zh-v1.5",
        "name": "BGE-small-zh（默认）",
        "detail": "约 23MB，速度快，适合日常分段",
    },
    {
        "id": "Xenova/bge-base-zh-v1.5",
        "name": "BGE-base-zh",
        "detail": "约 100MB，语义更细，适合长篇稿件",
    },
    {
        "id": "Xenova/bge-large-zh-v1.5",
        "name": "BGE-large-zh",
        "detail": "约 330MB，最准但更吃内存",
    },
)


def describe_local_models() -> List[Dict[str, Any]]:
    """本地模型清单（含"当前是否已下载"）。"""
    items: List[Dict[str, Any]] = []
    current = local_model_name()
    for item in AVAILABLE_LOCAL_MODELS:
        target = semantic_model_dir() if item["id"] == current else None
        ready = False
        if target is not None:
            try:
                _pick_onnx_file(target)
                ready = (target / "tokenizer.json").is_file()
            except RuntimeError:
                ready = False
        items.append({**item, "current": item["id"] == current, "ready": ready})
    return items
