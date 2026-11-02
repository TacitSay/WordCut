# -*- coding: utf-8 -*-
"""``model_fetch`` 的 smoke 测试（不联网）。

覆盖：目录约定、可用性判断、环境变量覆盖、禁止下载时的报错。
真实下载路径由 ``scripts/ensure_models.py`` 手工验证（需要网络，不适合放进单测）。
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import model_fetch

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


def with_env(key, value):
    """临时设置环境变量，返回恢复函数。"""
    old = os.environ.get(key)
    os.environ[key] = value

    def restore():
        if old is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = old

    return restore


# 1. 目录约定
check("默认模型名 small", model_fetch.DEFAULT_MODEL == "small")
check(
    f"模型目录命名({model_fetch.local_dir('small', Path('D:/m'))})",
    str(model_fetch.local_dir("small", Path("D:/m"))).replace("\\", "/").endswith(
        "m/faster-whisper-small"
    ),
)
check(
    "仓库模板",
    model_fetch.REPO_TEMPLATE.format(name="small") == "Systran/faster-whisper-small",
)

# 2. 可用性判断
tmp = Path(tempfile.mkdtemp(prefix="wordcut_mf_"))
empty = tmp / "faster-whisper-small"
empty.mkdir()
check("空目录不算可用", not model_fetch.is_ready(empty))

for name in ("model.bin", "config.json"):
    (empty / name).write_bytes(b"x")
check("有 model.bin+config 才算可用", model_fetch.is_ready(empty))
check("非目录不算可用", not model_fetch.is_ready(tmp / "不存在"))

# 3. 环境变量
restore_root = with_env("WORDCUT_MODELS_DIR", str(tmp))
check(f"WORDCUT_MODELS_DIR 生效({model_fetch.models_root()})", model_fetch.models_root() == tmp)
check("find_local 能找到", model_fetch.find_local("small") == empty.resolve())

restore_name = with_env("WORDCUT_WHISPER_MODEL", "medium")
check(f"WORDCUT_WHISPER_MODEL 生效({model_fetch.model_name()})", model_fetch.model_name() == "medium")
check("换模型后不误用 small", model_fetch.find_local("medium") is None)

restore_name()
restore_root()

restore_path = with_env("WORDCUT_WHISPER_MODEL_PATH", str(empty))
check("WORDCUT_WHISPER_MODEL_PATH 优先生效", model_fetch.explicit_path() == empty)
restore_path()

# 4. 禁止下载
restore_off = with_env("WORDCUT_NO_MODEL_DOWNLOAD", "1")
try:
    model_fetch.ensure_model("small", root=tmp / "空目录")
    check("禁止下载时应报错", False)
except model_fetch.ModelNotAvailable as exc:
    check(f"禁止下载时报 ModelNotAvailable({str(exc)[:24]}…)", "自动下载已关闭" in str(exc))
finally:
    restore_off()

# 5. 已有模型时 ensure 不再下载（直接返回）
restore_root2 = with_env("WORDCUT_MODELS_DIR", str(tmp))
path = model_fetch.ensure_model("small", allow_download=False)
check("已有模型时直接复用", path == empty.resolve())
restore_root2()

# 6. 体积提示覆盖常用模型
for name in ("tiny", "base", "small", "medium", "large-v3"):
    check(f"体积提示含 {name}", name in model_fetch.MODEL_SIZE_HINT)

print()
print(f"结果: {passed} 通过, {failed} 失败")
sys.exit(1 if failed else 0)
