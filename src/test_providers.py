# -*- coding: utf-8 -*-
"""``providers`` / ``model_registry`` 的 smoke 测试（不联网）。

要守住的行为：
1. 服务商预设**不含任何密钥**（只有公开端点与模型名）；
2. 每个服务商都有可用的 base 与模型列表；能按 URL 反推服务商；
3. 模型目录扫描：正确识别 ready / deprecated，不越界读；
4. 删除模型：拒绝非法目录名与越界路径；
5. 扫描结果可 JSON 序列化。
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import model_registry
import providers

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


# ----------------------------------------------------------------------
# 1. 服务商预设
# ----------------------------------------------------------------------
items = providers.provider_list()
check(f"服务商数量({len(items)})", len(items) >= 5)
check(
    "每个都有 id/name",
    all(item.get("id") and item.get("name") for item in items),
)
check(
    "都有 base（custom 除外）",
    all(item.get("base") for item in items if item["id"] != "custom"),
)
check(
    "都有模型（custom 除外）",
    all(item.get("models") for item in items if item["id"] != "custom"),
)

# 关键：预设里不能出现任何疑似密钥
raw = json.dumps(items, ensure_ascii=False)
check("预设不含 sk- 开头的 Key", "sk-" not in raw)
check("预设不含 Bearer", "Bearer" not in raw.lower())
check("预设不含 key 字段值", not any("key" in str(item).lower() and "key_url" not in str(item) for item in items))

# 已知服务商
deepseek = providers.find_provider("deepseek")
check("能查到 deepseek", deepseek is not None)
check("deepseek base 正确", deepseek["base"] == "https://api.deepseek.com/v1")
check("deepseek 有 key_url", bool(deepseek["key_url"]))

# 反推服务商
check("按 URL 反推 deepseek", providers.guess_provider("https://api.deepseek.com/v1") == "deepseek")
check("带尾斜杠也能反推", providers.guess_provider("https://api.deepseek.com/v1/") == "deepseek")
check("未知 URL 归为 custom", providers.guess_provider("https://example.com/api") == "custom")
check("空 URL 返回空", providers.guess_provider("") == "")

# 模型列表
check("models_for 返回列表", providers.models_for("zhipu") == providers.find_provider("zhipu")["models"])
check("未知服务商模型为空", providers.models_for("nope") == [])


# ----------------------------------------------------------------------
# 2. 模型目录扫描
# ----------------------------------------------------------------------
tmp = Path(tempfile.mkdtemp(prefix="wordcut_registry_"))

# 造一个"可用模型"
good = tmp / "summarizer-test"
good.mkdir()
(good / "config.json").write_text("{}", encoding="utf-8")
(good / "tokenizer.json").write_text("{}", encoding="utf-8")
(good / "model.safetensors").write_bytes(b"x" * 1024)

# 造一个"缺权重的目录"
bad = tmp / "empty-dir"
bad.mkdir()
(bad / "readme.txt").write_text("no weights", encoding="utf-8")

# 造一个废弃模型
dep = tmp / "semantic-Xenova__old"
dep.mkdir()
(dep / "config.json").write_text("{}", encoding="utf-8")
(dep / "model.onnx").write_bytes(b"x")

scanned = model_registry.scan_models(tmp)
by_name = {item["name"]: item for item in scanned}

check(f"扫描到 3 个目录({len(scanned)})", len(scanned) == 3)
check("识别可用模型 ready", by_name.get("summarizer-test", {}).get("ready") is True)
check("缺权重目录 ready=False", by_name.get("empty-dir", {}).get("ready") is False)
check("废弃前缀被标记", by_name.get("semantic-Xenova__old", {}).get("deprecated") is True)
check("可用模型不被误标废弃", by_name.get("summarizer-test", {}).get("deprecated") is False)
check("能推断用途（summarizer）", by_name.get("summarizer-test", {}).get("kind") == "段落标题")
check("扫描结果可 JSON 序列化", isinstance(json.dumps(scanned), str))
check("目录不存在时返回空", model_registry.scan_models(tmp / "no-such") == [])


# ----------------------------------------------------------------------
# 3. 删除模型（安全性）
# ----------------------------------------------------------------------
# 非法名
for bad_name in ("../etc", "a/b", "a\\b", "", ".."):
    result = model_registry.remove_model(bad_name, tmp)
    check(f"拒绝非法目录名({bad_name!r})", result["removed"] is False and result["error"])

# 不存在的目录
result = model_registry.remove_model("no-such-dir", tmp)
check("不存在时 removed=False", result["removed"] is False)

# 正常删除
result = model_registry.remove_model("empty-dir", tmp)
check("正常删除成功", result["removed"] is True)
check(f"报告释放空间({result['freed_mb']})", result["freed_mb"] >= 0)
check("目录确实消失了", not (tmp / "empty-dir").exists())
check("其它目录未受影响", (tmp / "summarizer-test").exists())

shutil.rmtree(tmp, ignore_errors=True)

print()
print(f"结果: {passed} 通过, {failed} 失败")
sys.exit(1 if failed else 0)
