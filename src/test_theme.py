# -*- coding: utf-8 -*-
"""主题切换的 CSS 与配置检查（不跑浏览器）。

前端主题靠 `<html data-theme>` + CSS 变量覆盖实现，这类"约定"最容易在
后续改动里被悄悄破坏（比如有人把深色值写回 :root，默认主题就不再是浅色）。
这里用静态检查把契约固定下来。

检查项：
1. `style.css` 里 `:root` 默认必须是**浅色**（默认主题约定）；
2. 存在 `:root[data-theme='dark']` 覆盖块，且覆盖了核心 token；
3. 两套主题都定义了整套新拟态阴影（漏一个就是某些元素没有立体感）；
4. `index.html` 有首屏内联脚本（避免刷新时闪一下浅色）；
5. 主题存储键在前端各处一致。
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STYLE = ROOT / "frontend" / "src" / "style.css"
INDEX = ROOT / "frontend" / "index.html"
THEME_JS = ROOT / "frontend" / "src" / "composables" / "useTheme.js"

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


css = STYLE.read_text(encoding="utf-8")

# ---- 1. 默认主题是浅色 ----
root_match = re.search(r":root\s*\{(.*?)\n\}", css, re.S)
check("style.css 存在 :root 块", root_match is not None)
root_block = root_match.group(1) if root_match else ""

check(":root 默认 color-scheme 为 light", "color-scheme: light;" in root_block)
check(
    "默认底色是浅色（亮度高）",
    bool(re.search(r"--bg:\s*#d[0-9a-f]{5};", root_block, re.I)),
)

# ---- 2. 深色覆盖块 ----
dark_match = re.search(r":root\[data-theme=['\"]dark['\"]\]\s*\{(.*?)\n\}", css, re.S)
check("存在 :root[data-theme='dark'] 覆盖块", dark_match is not None)
dark_block = dark_match.group(1) if dark_match else ""
check("深色块 color-scheme 为 dark", "color-scheme: dark;" in dark_block)
check(
    "深色底色是深色（亮度低）",
    bool(re.search(r"--bg:\s*#2[0-9a-f]{5};", dark_block, re.I)),
)

# ---- 3. 两套主题的核心 token 齐全 ----
CORE_TOKENS = (
    "--bg",
    "--bg-panel",
    "--bg-elevated",
    "--bg-reading",
    "--nm-light",
    "--nm-dark",
    "--nm-raised",
    "--nm-raised-sm",
    "--nm-inset",
    "--nm-inset-sm",
    "--text",
    "--text-strong",
    "--text-dim",
    "--accent",
)
missing_light = [t for t in CORE_TOKENS if t not in root_block]
missing_dark = [t for t in CORE_TOKENS if t not in dark_block]
check(f"浅色 token 齐全({missing_light or '无缺失'})", not missing_light)
check(f"深色 token 齐全({missing_dark or '无缺失'})", not missing_dark)

# 深色必须**覆盖**而不是继承浅色的关键项（否则深色主题看着是浅的）
for token in ("--bg", "--bg-panel", "--bg-reading", "--text", "--nm-dark"):
    check(f"深色覆盖了 {token}", token in dark_block)

# ---- 4. index.html 首屏内联脚本 ----
html = INDEX.read_text(encoding="utf-8")
check("index.html 有首屏主题脚本", "wordcut.theme" in html)
check("首屏脚本写入 data-theme", "data-theme" in html)
check(
    "首屏脚本在样式之前（避免闪白）",
    html.index("wordcut.theme") < html.index("<body"),
)

# ---- 5. 存储键一致 ----
theme_js = THEME_JS.read_text(encoding="utf-8") if THEME_JS.is_file() else ""
check("useTheme.js 存在", THEME_JS.is_file())
check("存储键与 index.html 一致", "'wordcut.theme'" in theme_js and "wordcut.theme" in html)
check("导出 toggleTheme", "export function toggleTheme" in theme_js)
check("默认主题常量为 light", "DEFAULT_THEME = 'light'" in theme_js)
check("只接受 light/dark", "THEMES" in theme_js)

print()
print(f"结果: {passed} 通过, {failed} 失败")
sys.exit(1 if failed else 0)
