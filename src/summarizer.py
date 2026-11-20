# -*- coding: utf-8 -*-
"""段落摘要：把一段话压成一句话标题。

为什么需要单独一层：关键词抽取（`titlemaker`）只能产出"词"，做不到
"概括"。用户要的是"前言 / 内容总结"这种**叙事级**标题，那必须是生成式
模型。这一层负责管理"谁来生成"，并把结果与失败情况都收敛成统一接口。

三种后端，按可用性自动降级：

1. **API**（推荐）：OpenAI 兼容接口，质量最好，几秒出结果；
2. **本地 seq2seq**：mT5/Pegasus 这类中文摘要模型（encoder-decoder），
   需要 ``transformers`` + ``sentencepiece``，CPU 也能跑；
3. **关键词兜底**：前两者都不可用时退回 `titlemaker`，保证永远有标题。

**内存纪律**：本地模型按需加载、空闲超时卸载（复用 semantic 的策略），
不在服务启动时就占内存。
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

__all__ = [
    "summarize_paragraphs",
    "local_summarizer_status",
    "unload_local_summarizer",
    "maybe_unload_summarizer",
    "SUMMARIZER_DIR",
]

#: 本地摘要模型目录（可由设置覆盖）。
SUMMARIZER_DIR: Path = Path(__file__).resolve().parent.parent / "models" / "summarizer-qwen15b"

#: 空闲多久卸载本地摘要模型（秒）。
_IDLE_SECONDS = 600.0

#: 生成参数：标题不宜过长，否则又变成"正文摘要"。
#: 生成上限（字符数），实际按 max_chars 动态调整
_MAX_NEW_TOKENS = 40

_local_lock = threading.Lock()
_local_state: Dict[str, Any] = {
    "tokenizer": None,
    "model": None,
    "dir": "",
    "last_used": 0.0,
    "loads": 0,
    "unloads": 0,
    "error": "",
}


def _model_dir() -> Path:
    """本地模型目录：设置优先，其次默认路径。"""
    try:
        import app_settings

        configured = str(app_settings.get_section("segment").get("summarizer_dir") or "").strip()
        if configured:
            return Path(configured).expanduser()
    except Exception:  # noqa: BLE001
        pass
    return SUMMARIZER_DIR


def local_summarizer_status() -> Dict[str, Any]:
    """本地摘要模型状态（可直接 JSON 化）。"""
    with _local_lock:
        loaded = _local_state["model"] is not None
        return {
            "loaded": loaded,
            "dir": _local_state["dir"] or str(_model_dir()),
            "available": _model_dir().is_dir(),
            "idle_seconds": round(time.time() - _local_state["last_used"], 1) if loaded else 0.0,
            "loads": _local_state["loads"],
            "unloads": _local_state["unloads"],
            "error": _local_state["error"],
        }


def unload_local_summarizer() -> bool:
    """卸载本地摘要模型；未加载时返回 ``False``。"""
    with _local_lock:
        if _local_state["model"] is None:
            return False
        _local_state["model"] = None
        _local_state["tokenizer"] = None
        _local_state["unloads"] += 1
    import gc

    gc.collect()
    torch_module = __import__("sys").modules.get("torch")
    if torch_module is not None:
        try:
            if torch_module.cuda.is_available():
                torch_module.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass
    return True


def maybe_unload_summarizer() -> bool:
    """空闲超时则卸载。"""
    with _local_lock:
        loaded = _local_state["model"] is not None
        idle = time.time() - _local_state["last_used"]
    if loaded and idle >= _IDLE_SECONDS:
        return unload_local_summarizer()
    return False


def _ensure_local() -> Any:
    """按需加载本地摘要模型，返回 ``(tokenizer, model)``。

    异常:
        RuntimeError: 依赖缺失或模型加载失败（调用方应降级到关键词）。
    """
    with _local_lock:
        if _local_state["model"] is not None:
            _local_state["last_used"] = time.time()
            return _local_state["tokenizer"], _local_state["model"]

    directory = _model_dir()
    if not directory.is_dir():
        raise RuntimeError(f"本地摘要模型目录不存在：{directory}")

    try:
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(f"缺少 transformers：{exc}") from exc

    try:
        tokenizer = AutoTokenizer.from_pretrained(str(directory))
        # 注意：参数名必须是 torch_dtype（写成 dtype 会被当成多余 kwarg）
        model = AutoModelForCausalLM.from_pretrained(str(directory), torch_dtype="float32")
        model.eval()
    except Exception as exc:  # noqa: BLE001 - sentencepiece 崩溃等都在这里
        raise RuntimeError(f"加载本地摘要模型失败：{exc}") from exc

    with _local_lock:
        _local_state.update(
            tokenizer=tokenizer,
            model=model,
            dir=str(directory),
            last_used=time.time(),
            loads=_local_state["loads"] + 1,
            error="",
        )
    return tokenizer, model


def _summarize_local(texts: Sequence[str], max_chars: int) -> List[str]:
    """用本地指令模型（Qwen 等）生成概括式标题。

    为什么用 causal LM 而不是 mT5/Pegasus：本机 sentencepiece 原生库在加载
    ``spiece.model`` 时直接触发 Windows 访问违规（0xC0000005），mT5/Pegasus
    全部不可用。Qwen 系列用 ``tokenizer.json``（BPE），不经过 sentencepiece，
    实测可正常加载（1.5B 模型 CPU 加载 12.8s、每段生成 1–2.5s）。
    """
    import torch

    tokenizer, model = _ensure_local()
    results: List[str] = []

    limit_hint = f"{max_chars} 字以内" if max_chars else "10–18 个字"
    max_new = 40 if max_chars <= 0 else min(48, max(16, max_chars * 3))

    for text in texts:
        messages = [
            {
                "role": "system",
                "content": (
                    "你是中文编辑。只输出一个概括性小标题，"
                    f"{limit_hint}，不要解释、不要引号、不要句号结尾。"
                ),
            },
            {"role": "user", "content": f"给这段访谈起个概括性小标题：\n{text[:1200]}"},
        ]
        try:
            prompt = tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True
            )
            encoded = tokenizer(prompt, return_tensors="pt", truncation=True, max_length=2048)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"提示词编码失败：{exc}") from exc

        # generation_config.json 里的 temperature/top_p 在 do_sample=False 时无效，
        # transformers 会打警告。显式传一份干净的配置避免刷屏。
        with torch.no_grad():
            output = model.generate(
                **encoded,
                max_new_tokens=max_new,
                do_sample=False,
                num_beams=1,
                no_repeat_ngram_size=3,
                pad_token_id=tokenizer.eos_token_id,
                temperature=None,
                top_p=None,
                top_k=None,
            )
        generated = output[0][encoded["input_ids"].shape[1] :]
        results.append(tokenizer.decode(generated, skip_special_tokens=True).strip())

    with _local_lock:
        _local_state["last_used"] = time.time()
    return results


def _summarize_api(texts: Sequence[str], max_chars: int) -> List[str]:
    """用 OpenAI 兼容接口生成摘要。"""
    import semantic

    config = semantic.api_config()
    if not config:
        raise RuntimeError("未配置语义 API")

    limit_hint = f"不超过 {max_chars} 字" if max_chars else "10–16 字"
    numbered = "\n".join(f"{i + 1}. {t[:500]}" for i, t in enumerate(texts))
    prompt = (
        f"请为下面 {len(texts)} 个段落各写一个概括性小标题，{limit_hint}。\n"
        "要求：概括这段讲的主题，像「前言」「核心观点」「行动建议」那样；"
        "不要照抄原文句子，不要只给一个词。\n\n"
        f"{numbered}\n\n"
        "只输出 JSON 数组，元素为标题字符串，顺序与段落一致。"
    )
    payload = {
        "model": config["model"],
        "temperature": 0.3,
        "messages": [
            {"role": "system", "content": "你是中文编辑，擅长起简洁准确的段落标题，只输出 JSON。"},
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
    timeout = int(os.environ.get("WORDCUT_SEMANTIC_API_TIMEOUT") or 60)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read().decode("utf-8", "replace"))

    content = body["choices"][0]["message"]["content"]
    left, right = content.find("["), content.rfind("]")
    if left < 0 or right <= left:
        raise RuntimeError("摘要 API 未返回 JSON 数组")
    raw = json.loads(content[left : right + 1])
    titles = [str(item or "").strip() for item in raw]
    if len(titles) != len(texts):
        raise RuntimeError(f"API 返回 {len(titles)} 条，期望 {len(texts)} 条")
    return titles


def summarize_paragraphs(
    texts: Sequence[str],
    backend: str = "auto",
    max_chars: int = 0,
    log: Optional[Any] = None,
) -> Dict[str, Any]:
    """为段落生成概括式标题。

    参数:
        texts: 段落全文列表。
        backend: ``auto``（API 优先，失败退本地）/ ``api`` / ``local``。
            默认 ``auto``：API 通常 1–2 秒出结果；限流时会自动退到本地，
            不会让用户干等。
        max_chars: 标题字数上限，``0`` 表示不限制。
        log: 调试回调。

    返回:
        ``{"titles": [...], "backend": "api|local|keyword", "errors": [...]}``。
        **永远返回与 texts 等长的标题列表**（失败时用关键词兜底）。
    """
    say = log or (lambda *_a, **_k: None)
    errors: List[str] = []

    if not texts:
        return {"titles": [], "backend": "none", "errors": []}

    order = ["api", "local"] if backend == "auto" else [backend]

    for name in order:
        try:
            if name == "api":
                titles = _summarize_api(texts, max_chars)
            elif name == "local":
                titles = _summarize_local(texts, max_chars)
            else:
                continue
            clean = [_normalize_title(item, max_chars) for item in titles]
            if sum(1 for item in clean if item) >= max(1, len(texts) // 2):
                say(f"摘要（{name}）：{sum(1 for i in clean if i)}/{len(texts)}")
                return {"titles": clean, "backend": name, "errors": errors}
            errors.append(f"{name}: 有效标题过少")
        except Exception as exc:  # noqa: BLE001 - 逐级降级
            errors.append(f"{name}: {exc}")
            say(f"摘要后端 {name} 失败：{exc}")

    # 兜底：关键词
    import titlemaker

    say("摘要后端均不可用，退回关键词")
    return {
        "titles": titlemaker.make_titles(texts, max_chars=max_chars, log=say),
        "backend": "keyword",
        "errors": errors,
    }


def _normalize_title(text: str, max_chars: int) -> str:
    """清掉模型输出里的多余符号与换行。"""
    cleaned = str(text or "").strip()
    for prefix in ("标题：", "标题:", "- ", "• "):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
    cleaned = cleaned.replace("\n", " ").strip(" 。，、\"'“”")
    if max_chars and len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars].rstrip()
    return cleaned
