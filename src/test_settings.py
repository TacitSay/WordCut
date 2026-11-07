# -*- coding: utf-8 -*-
"""``app_settings`` 的 smoke 测试（用临时文件，不动真实配置）。

覆盖：默认值补齐、节更新与合并、原子落盘、密钥遮蔽、环境变量覆盖路径、非法输入。
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# 必须在导入 app_settings 之前指定临时设置路径（模块级读取环境变量）
_TMP = Path(tempfile.mkdtemp(prefix="wordcut_settings_")) / "settings.json"
os.environ["WORDCUT_SETTINGS_PATH"] = str(_TMP)

import app_settings  # noqa: E402

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


# 1. 默认值
settings = app_settings.load_settings()
check("读到默认 mode=rule", settings["segment"]["mode"] == "rule")
check("默认本地模型", settings["segment"]["local_model"].startswith("Xenova/"))
check("临时设置路径生效", app_settings.settings_path() == _TMP)

# 2. 更新与持久化
app_settings.update_section("segment", {"mode": "local", "api_timeout": 30})
check("更新后立即可读", app_settings.get_section("segment")["mode"] == "local")
check("文件已落盘", _TMP.is_file())

raw = json.loads(_TMP.read_text(encoding="utf-8"))
check("落盘内容正确", raw["segment"]["mode"] == "local" and raw["segment"]["api_timeout"] == 30)
check("未改字段保留默认", raw["segment"]["local_model"].startswith("Xenova/"))

# 3. 部分更新不会清空其它字段
app_settings.update_section("segment", {"api_base": "https://example.com/v1"})
section = app_settings.get_section("segment")
check("部分更新保留 mode", section["mode"] == "local")
check("部分更新写入 base", section["api_base"].endswith("/v1"))

# 4. 未知字段保留（向前兼容）
app_settings.update_section("segment", {"future_field": {"x": 1}})
check("未知字段被保留", app_settings.get_section("segment")["future_field"] == {"x": 1})

# 5. 密钥遮蔽
app_settings.update_section("segment", {"api_key": "sk-secret-123"})
public = app_settings.public_section("segment", secret_keys=("api_key",))
check("公开副本不含明文 key", public["api_key"] == "")
check("公开副本标记 key 已设置", public["api_key_set"] is True)

app_settings.update_section("segment", {"api_key": ""})
public2 = app_settings.public_section("segment", secret_keys=("api_key",))
check("清除 key 后标记为未设置", public2["api_key_set"] is False)

# 6. 重置
app_settings.reset_section("segment")
check("重置后回到默认", app_settings.get_section("segment")["mode"] == "rule")
check("重置后未知字段消失", "future_field" not in app_settings.get_section("segment"))

# 7. 非法输入
for bad in ([], "x", 3):
    try:
        app_settings.update_section("segment", bad)
        check(f"非法 patch {bad!r} 应报错", False)
    except ValueError:
        check(f"非法 patch {bad!r} 报 ValueError", True)

try:
    app_settings.update_section("bad name!", {})
    check("非法节名应报错", False)
except ValueError:
    check("非法节名报 ValueError", True)

# 8. 坏 JSON 不崩（回退默认）
_TMP.write_text("{ 这不是 JSON", encoding="utf-8")
check("坏 JSON 回退默认", app_settings.load_settings()["segment"]["mode"] == "rule")

# 8b. memory 节（空闲卸载阈值）
mem = app_settings.get_section("memory")
check("memory 默认 whisper=900", mem.get("whisper_idle_seconds") == 900)
check("memory 默认 semantic=600", mem.get("semantic_idle_seconds") == 600)
app_settings.update_section("memory", {"whisper_idle_seconds": 120})
check("memory 更新生效", app_settings.get_section("memory")["whisper_idle_seconds"] == 120)

# 9. 目录不存在时自动创建
_TMP.unlink(missing_ok=True)
nested = Path(tempfile.mkdtemp(prefix="wordcut_settings2_")) / "deep" / "dir" / "s.json"
app_settings.SETTINGS_PATH = nested
app_settings.update_section("segment", {"mode": "api"})
check("自动创建父目录", nested.is_file())

print()
print(f"结果: {passed} 通过, {failed} 失败")
sys.exit(1 if failed else 0)
