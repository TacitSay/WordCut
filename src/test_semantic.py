# -*- coding: utf-8 -*-
"""``semantic`` 的 smoke 测试（不联网、不加载真模型）。

覆盖：
* 断点选择算法（长度约束、只允许在句末断、落差越大越优先）；
* API 回复解析（JSON 数组、1 基序号、越界与脏数据）；
* 策略工厂与可用性判断；
* 语义后端失败时的**回退**（分段永远不该让项目不可用）；
* 本地模型卸载是幂等的（没加载时返回 False）。
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import semantic
from model import Token

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


def make_tokens(texts, step=2.0):
    """按给定文本造一串 Token（每句 1 个 token，时间连续）。"""
    tokens = []
    for index, text in enumerate(texts):
        tokens.append(
            Token(
                id=f"t{index:05d}",
                text=text,
                start=index * step,
                end=index * step + 1.5,
                is_deleted=False,
                confidence=0.9,
            )
        )
    return tokens


# ----------------------------------------------------------------------
# 1. 断点选择
# ----------------------------------------------------------------------
lengths = [50, 50, 50, 50]
scores = [0.1, 0.9, 0.2]          # 中间那处落差最大
eligible = [True, True, True]
picked = semantic.choose_boundaries(lengths, scores, eligible, target_chars=100, min_chars=40, max_chars=120)
check(f"按总字数估算需要 1 刀，选落差最大的位置({picked})", picked == [1])

# 目标更大 -> 不需要断
picked2 = semantic.choose_boundaries(lengths, scores, eligible, target_chars=1000, min_chars=10, max_chars=2000)
check(f"目标段落很大时不断({picked2})", picked2 == [])

# 不允许断的位置不能选
picked3 = semantic.choose_boundaries(lengths, scores, [False, False, False], target_chars=100)
check(f"没有可用断点时返回空({picked3})", picked3 == [])

# 长度约束：切完必须都 >= min_chars
lengths4 = [30, 30, 30, 30]
picked4 = semantic.choose_boundaries(lengths4, [0.9, 0.8, 0.7], [True, True, True], target_chars=60, min_chars=50, max_chars=1000)
check(f"段落不得短于 min_chars({picked4})", all(
    sum(lengths4[s:e + 1]) >= 50
    for s, e in zip([0] + [i + 1 for i in picked4], picked4 + [3])
))

# 单句/空输入
check("单句不断", semantic.choose_boundaries([10], [], []) == [])
check("空输入不断", semantic.choose_boundaries([], [], []) == [])

# 长度检查工具
check("_respects_lengths 合规", semantic._respects_lengths([50, 50], [0], 40, 60))
check("_respects_lengths 过短", not semantic._respects_lengths([10, 50], [0], 40, 60))
check("_respects_lengths 过长", not semantic._respects_lengths([50, 90], [], 40, 60))

# ----------------------------------------------------------------------
# 2. API 回复解析
# ----------------------------------------------------------------------
check("解析标准数组", semantic._parse_boundary_json("[2, 5]", 10) == [1, 4])
check("容忍解释性文字", semantic._parse_boundary_json("好的：\n[3]\n以上。", 10) == [2])
check("过滤越界序号", semantic._parse_boundary_json("[0, 1, 99]", 5) == [0])
check("去重并排序", semantic._parse_boundary_json("[4, 2, 2]", 10) == [1, 3])
check("空数组合法", semantic._parse_boundary_json("[]", 10) == [])

try:
    semantic._parse_boundary_json("没有 JSON", 5)
    check("非 JSON 应报错", False)
except RuntimeError:
    check("非 JSON 报 RuntimeError", True)

# ----------------------------------------------------------------------
# 3. 策略工厂与可用性
# ----------------------------------------------------------------------
check("未知策略回退规则", type(semantic.build_segmenter("nonsense")).__name__ == "RuleSegmenter")
check("None 回退规则", type(semantic.build_segmenter(None)).__name__ == "RuleSegmenter")
check("语义策略构造成功", isinstance(semantic.build_segmenter(semantic.STRATEGY_SEMANTIC_LOCAL), semantic.SemanticSegmenter))
check("策略清单非空", len(semantic.describe_strategies()) == len(semantic.STRATEGIES))

_old_base = os.environ.pop("WORDCUT_SEMANTIC_API_BASE", None)
_old_model = os.environ.pop("WORDCUT_SEMANTIC_API_MODEL", None)
check("未配置 API 时 semantic_api 不可用", not semantic.strategy_available(semantic.STRATEGY_SEMANTIC_API))
os.environ["WORDCUT_SEMANTIC_API_BASE"] = "https://example.com/v1"
os.environ["WORDCUT_SEMANTIC_API_MODEL"] = "demo"
check("配置齐全后 semantic_api 可用", semantic.strategy_available(semantic.STRATEGY_SEMANTIC_API))
check("api_config 读得到", semantic.api_config().get("model") == "demo")
os.environ.pop("WORDCUT_SEMANTIC_API_BASE")
os.environ.pop("WORDCUT_SEMANTIC_API_MODEL")
if _old_base is not None:
    os.environ["WORDCUT_SEMANTIC_API_BASE"] = _old_base
if _old_model is not None:
    os.environ["WORDCUT_SEMANTIC_API_MODEL"] = _old_model

# ----------------------------------------------------------------------
# 4. 语义失败必须回退到规则分段
# ----------------------------------------------------------------------
texts = [
    "今天我们来聊聊播客剪辑。",
    "第一个问题是素材太长。",
    "第二个问题是改错很难。",
    "所以我们做了词级对齐。",
    "这个思路可以推广。",
]
tokens = make_tokens(texts)

original_embed = semantic.embed_sentences


def boom(_sentences, batch_size=16):
    raise RuntimeError("模型炸了")


semantic.embed_sentences = boom
seg = semantic.SemanticSegmenter(strategy=semantic.STRATEGY_SEMANTIC_LOCAL)
groups = seg.segment(tokens)
check(f"语义失败后仍有段落({len(groups)})", len(groups) >= 1)
check("语义失败记录了原因", "模型炸了" in seg.last_error)
check("语义失败时后端标记为 rule", seg.last_backend == "rule")
semantic.embed_sentences = original_embed

# 规则策略不碰模型
rule_groups = semantic.build_segmenter(semantic.STRATEGY_RULE).segment(tokens)
check(f"规则分段可用({len(rule_groups)} 段)", len(rule_groups) >= 1)
check("规则分段覆盖全部 token", sum(len(g) for g in rule_groups) == len(tokens))

# ----------------------------------------------------------------------
# 5. 卸载是幂等的
# ----------------------------------------------------------------------
check("未加载时卸载返回 False", semantic.unload_local_model() is False)
status = semantic.local_model_status()
check(f"状态可序列化且 loaded=False({status['loaded']})", status["loaded"] is False)
check("空闲卸载在未加载时安全", semantic.maybe_unload_local() is False)

print()
print(f"结果: {passed} 通过, {failed} 失败")
sys.exit(1 if failed else 0)
