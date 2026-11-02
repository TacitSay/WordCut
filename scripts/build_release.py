# -*- coding: utf-8 -*-
"""打发行包：把"能直接跑"的东西压成一个 zip，并生成 SHA256 校验文件。

为什么需要：GitHub Release 只自动给源码 zip，用户拿到后还得装 Node、`npm install`、
`npm run build`。而项目本身是个本地 Web 应用，完全可以预先构建好前端、让后端
在同一端口托管（``uvicorn`` 本身就够），于是发行包只需要 **Python + ffmpeg**。

产物（默认写到 ``release/``）：

* ``WordCut-<版本>-win64.zip`` —— 解压即用
* ``SHA256SUMS.txt`` —— 校验和（贴到 Release 里，方便用户核对）

包里刻意**不含**：模型（460MB，首次运行自动下载）、CUDA 运行库（1.7GB，
用 ``scripts/fetch_cuda_libs.py`` 取）、``node_modules``、源码里的测试与文档。

用法::

    python scripts/build_release.py
    python scripts/build_release.py --skip-build      # 已有 dist，跳过 npm build
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
FRONTEND = ROOT / "frontend"
DIST = FRONTEND / "dist"
RELEASE_DIR = ROOT / "release"

#: 包内要带的后端文件（相对 src/）
SRC_PATTERNS = ("*.py",)
#: 不打包的后端文件（测试与缓存）
SRC_EXCLUDE = ("test_*.py", "__pycache__")

#: 包内要带的脚本
SCRIPTS = (
    "ensure_models.py",
    "fetch_cuda_libs.py",
    "check_cuda.py",
    "run_tests.py",
)


def read_version() -> str:
    return (ROOT / "VERSION").read_text(encoding="utf-8").strip()


def run_npm_build() -> None:
    print("构建前端（npm run build）…")
    result = subprocess.run(
        ["cmd.exe", "/c", "npm run build"],
        cwd=str(FRONTEND),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-5:]
        raise SystemExit("npm run build 失败：\n" + "\n".join(tail))
    print("  前端构建完成")


def collect_into(target: Path) -> list[Path]:
    """把要打包的文件复制到 target，返回已复制的路径列表。"""
    copied: list[Path] = []

    def add(src: Path, dst_rel: Path) -> None:
        dst = target / dst_rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(dst)

    # 根目录文件
    for name in (
        "README.md",
        "VERSION",
        "requirements.txt",
        "start.bat",
        "start.ps1",
        "start-prod.bat",
        "stop.bat",
        "stop.ps1",
    ):
        path = ROOT / name
        if path.is_file():
            add(path, Path(name))

    # 后端源码（不含测试）
    for path in sorted(SRC.glob("*.py")):
        if any(path.match(pattern) for pattern in SRC_EXCLUDE):
            continue
        add(path, Path("src") / path.name)
    media_dir = SRC / "media"
    if media_dir.is_dir():
        for path in sorted(media_dir.glob("*.py")):
            add(path, Path("src") / "media" / path.name)

    # 前端构建产物
    for path in sorted(DIST.rglob("*")):
        if path.is_file():
            add(path, Path("frontend") / "dist" / path.relative_to(DIST))

    # 运维脚本
    for name in SCRIPTS:
        path = ROOT / "scripts" / name
        if path.is_file():
            add(path, Path("scripts") / name)

    # 图标（快捷方式用）
    icon = ROOT / "assets" / "brand" / "wordcut.ico"
    if icon.is_file():
        add(icon, Path("assets") / "brand" / "wordcut.ico")

    return copied


def make_zip(source_dir: Path, zip_path: Path, arc_root: str) -> None:
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(source_dir.rglob("*")):
            if path.is_file():
                archive.write(path, f"{arc_root}/{path.relative_to(source_dir).as_posix()}")


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="打 WordCut 发行包")
    parser.add_argument("--skip-build", action="store_true", help="跳过 npm run build")
    args = parser.parse_args()

    version = read_version()
    print(f"WordCut {version} 发行包")

    if not args.skip_build:
        run_npm_build()
    if not (DIST / "index.html").is_file():
        print(f"缺少前端产物：{DIST / 'index.html'}（先 npm run build）")
        return 1

    stage = RELEASE_DIR / f"WordCut-{version}-win64"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    copied = collect_into(stage)
    print(f"  收集 {len(copied)} 个文件")

    zip_path = RELEASE_DIR / f"WordCut-{version}-win64.zip"
    make_zip(stage, zip_path, stage.name)

    checksum = sha256_of(zip_path)
    sums = RELEASE_DIR / "SHA256SUMS.txt"
    sums.write_text(
        f"{checksum}  {zip_path.name}\n", encoding="utf-8", newline="\n"
    )

    size_mb = zip_path.stat().st_size / 1048576
    print()
    print(f"  {zip_path.relative_to(ROOT)}  {size_mb:.2f} MB")
    print(f"  {sums.relative_to(ROOT)}")
    print(f"  SHA256 {checksum}")
    print()
    print("把它俩一起上传到 GitHub Release 的 Assets 即可。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
