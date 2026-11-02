# -*- coding: utf-8 -*-
"""把设计稿 PNG 转成站点所需的全部图标尺寸。

一次性脚本，但保留了可重复运行的能力：以后换了设计稿，改 ``--source``
重跑即可，不需要手工裁切。

产出：

==========================  ================================================
``frontend/public/favicon.ico``        多尺寸（16/24/32/48/64/128/256），浏览器标签页
``frontend/public/favicon-32.png``     32px，支持 PNG 图标的浏览器
``frontend/public/favicon-16.png``     16px
``frontend/public/apple-touch-icon.png`` 180px，iOS 主屏（铺底色，不支持透明）
``frontend/public/icon-192.png``       192px，PWA / manifest
``frontend/public/icon-512.png``       512px，PWA / manifest
``assets/brand/wordcut-icon-1024.png`` 1024px 母版（保留透明圆角）
``assets/wordcut.ico``                 256px，Windows 快捷方式 / 控制台窗口图标
==========================  ================================================

用法::

    python scripts/make_icons.py
    python scripts/make_icons.py --source "C:\\path\\to\\new-icon.png"
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
BRAND_DIR = ROOT / "assets" / "brand"
PUBLIC_DIR = ROOT / "frontend" / "public"
ASSETS_DIR = ROOT / "assets"

#: 透明区域铺底色用的深色（与图标自身背景接近，避免 Apple 触屏图标出现白角）
FLATTEN_BG = (11, 15, 22)
#: ICO 里要打包的尺寸
ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def resize(img: Image.Image, size: int, sharpen: bool = False) -> Image.Image:
    """按 LANCZOS 缩放；小尺寸额外锐化一点，避免 16px 糊成一团。"""
    out = img.resize((size, size), Image.LANCZOS)
    if sharpen and size <= 64:
        out = out.filter(ImageFilter.UnsharpMask(radius=1.0, percent=80, threshold=0))
    return out


def tighten(img: Image.Image, margin_ratio: float = 0.06) -> Image.Image:
    """按不透明内容裁掉多余留白，再留一圈边距。

    设计稿四周留白很大（图形只占约 70% 宽），直接缩到 16px 时线条糊成一片。
    小尺寸图标先用内容包围盒裁紧，让图形尽量占满画布。
    """
    alpha = img.getchannel("A")
    bbox = alpha.getbbox()
    if not bbox:
        return img
    cropped = img.crop(bbox)
    side = max(cropped.size)
    margin = int(side * margin_ratio)
    canvas = Image.new("RGBA", (side + margin * 2, side + margin * 2), (0, 0, 0, 0))
    canvas.alpha_composite(
        cropped,
        (margin + (side - cropped.size[0]) // 2, margin + (side - cropped.size[1]) // 2),
    )
    return canvas


def flatten(img: Image.Image, background: tuple[int, int, int]) -> Image.Image:
    """把透明通道铺成实色背景（iOS 触屏图标不接受透明）。"""
    canvas = Image.new("RGBA", img.size, (*background, 255))
    canvas.alpha_composite(img)
    return canvas.convert("RGB")


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 WordCut 全套图标")
    parser.add_argument(
        "--source",
        default=str(BRAND_DIR / "wc-source.png"),
        help="设计稿 PNG（默认 assets/brand/wc-source.png）",
    )
    args = parser.parse_args()

    source = Path(args.source)
    if not source.is_file():
        print(f"找不到设计稿：{source}")
        return 1

    BRAND_DIR.mkdir(parents=True, exist_ok=True)
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)

    original = Image.open(source).convert("RGBA")
    print(f"设计稿 {source.name}: {original.size[0]}×{original.size[1]} RGBA")

    # 1024 母版（保留透明圆角）
    master = resize(original, 1024, sharpen=False)
    master.save(BRAND_DIR / "wordcut-icon-1024.png", optimize=True)

    # 小尺寸统一用"裁紧内容"的版本（原稿留白太多，16/32 会糊）
    tight = tighten(original)

    # 浏览器图标：ICO + PNG
    # 多尺寸 ICO。
    # 注意：Pillow 写 ICO 只接受"一张源图 + sizes 列表"，由它内部逐尺寸缩放；
    # 传 append_images 会被静默忽略（之前就是这么得到一个 0.8KB 坏文件的）。
    # 因此源图用"裁紧版 256"，这样每个尺寸都是内容填满的版本。
    ico_base = resize(tight, 256, sharpen=False)
    ico_base.save(PUBLIC_DIR / "favicon.ico", sizes=ICO_SIZES)
    # 16/32 用小尺寸裁紧版：图形更满、笔画更清楚
    resize(tight, 32, sharpen=True).save(PUBLIC_DIR / "favicon-32.png", optimize=True)
    resize(tight, 16, sharpen=True).save(PUBLIC_DIR / "favicon-16.png", optimize=True)

    # iOS 主屏：180px、铺底色
    flatten(resize(original, 180), FLATTEN_BG).save(
        PUBLIC_DIR / "apple-touch-icon.png", optimize=True
    )

    # PWA / manifest
    resize(original, 192).save(PUBLIC_DIR / "icon-192.png", optimize=True)
    resize(original, 512).save(PUBLIC_DIR / "icon-512.png", optimize=True)

    # Windows 快捷方式 / 控制台图标
    ico_base.save(ASSETS_DIR / "wordcut.ico", sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])

    for path in (
        BRAND_DIR / "wordcut-icon-1024.png",
        PUBLIC_DIR / "favicon.ico",
        PUBLIC_DIR / "favicon-16.png",
        PUBLIC_DIR / "favicon-32.png",
        PUBLIC_DIR / "apple-touch-icon.png",
        PUBLIC_DIR / "icon-192.png",
        PUBLIC_DIR / "icon-512.png",
        ASSETS_DIR / "wordcut.ico",
    ):
        print(f"  {path.relative_to(ROOT)}  {path.stat().st_size / 1024:.1f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
