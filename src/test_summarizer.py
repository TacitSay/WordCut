# -*- coding: utf-8 -*-
"""``summarizer`` 的 smoke 测试（不联网、不加载真模型）。

要守住的行为：
1. 没配 API、没下本地模型时**必须**退回关键词，不能报错；
2. 后端依次降级并记录错误原因；
3. 任何后端失败都不抛异常，且返回与输入等长的标题列表；
4. 标题清理（去前缀、去引号、限字数）；
5. 本地模型状态查询在未加载时安全。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import summarizer

passed = 0
failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"PASS: {name}")
    else:
        failed += 1
        print(f"FAIL: {name}")


texts = [
    "咱们前面这三节课,其实是一个比较慢热的状态,然后到了这里第三节课,也就是我们最重要的内容。",
    "应该是对你们的情绪刺激是最大的,所以你们就一定要把这个情绪转化为实际的行动去改变点什么。",
    "因为我发现很多次强调情绪的重要性的时候,很多人还是不把他放在行动上。",
]

# ---- 1. 无后端时退回关键词 ----
result = summarizer.summarize_paragraphs(texts, backend="auto")
check(f"降级到关键词({result['backend']})", result["backend"] == "keyword")
check(f"标题数量一致({len(result['titles'])})", len(result["titles"]) == len(texts))
check("记录了失败原因", len(result["errors"]) >= 1)
check(
    f"错误里提到 api({result['errors'][0][:20]!r})",
    any("api" in item.lower() for item in result["errors"]),
)

# ---- 2. 指定 api 但未配置 ----
r_api = summarizer.summarize_paragraphs(texts, backend="api")
check("指定 api 未配置时也退回关键词", r_api["backend"] == "keyword")
check("api 失败有原因", any("api" in item.lower() for item in r_api["errors"]))

# ---- 3. 指定 local 但无模型 ----
r_local = summarizer.summarize_paragraphs(texts, backend="local")
check("指定 local 无模型时退回关键词", r_local["backend"] == "keyword")

# ---- 4. 空输入 ----
empty = summarizer.summarize_paragraphs([], backend="auto")
check("空输入返回空", empty == {"titles": [], "backend": "none", "errors": []})

# ---- 5. 标题清理 ----
check("去掉'标题：'前缀", summarizer._normalize_title("标题：情绪与行动", 0) == "情绪与行动")
check("去掉引号", summarizer._normalize_title('"情绪"', 0) == "情绪")
check("去换行", "\n" not in summarizer._normalize_title("情\n绪", 0))
check("字数限制生效", len(summarizer._normalize_title("情绪与行动的重要性", 4)) <= 4)

# ---- 6. 本地状态查询安全 ----
status = summarizer.local_summarizer_status()
check("状态可序列化", isinstance(status, dict) and "loaded" in status)
check("未加载时 loaded=False", status["loaded"] is False)
check("未加载时卸载返回 False", summarizer.unload_local_summarizer() is False)
check("空闲检查安全", summarizer.maybe_unload_summarizer() is False)

# ---- 7. 脏输入不抛异常 ----
for dirty in ([""], ["   "], ["。。。"], ["a"]):
    try:
        summarizer.summarize_paragraphs(dirty, backend="auto")
        ok = True
    except Exception:
        ok = False
    check(f"脏输入不抛异常({dirty[0]!r})", ok)

print()
print(f"结果: {passed} 通过, {failed} 失败")
sys.exit(1 if failed else 0)
