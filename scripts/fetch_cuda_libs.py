# -*- coding: utf-8 -*-
"""下载并解包 CUDA 运行库（cuDNN 8 + cuBLAS 12），供 ctranslate2 使用。

背景：这台机器的 pip 走代理会报 `FileNotFoundError(2)`，但 Python 的 urllib
能正常走代理。所以这里绕过 pip：直接用 urllib 下载 wheel（wheel 就是 zip），
把 DLL 解到 ``E:\\WordCut\\libs\\cuda\\bin``，运行时由 api.py 加进 DLL 搜索路径。

不装进 site-packages 是刻意的：
* 不动全局 Python 环境，卸载/回滚只需删目录；
* 论文里要写"环境配置"，独立目录更清楚；
* 避免 pip 的依赖解析再引入别的版本冲突。

用法::

    python scripts/fetch_cuda_libs.py
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

PROXY = "http://127.0.0.1:7890"
DEST = Path(r"E:\WordCut\libs\cuda")
DOWNLOAD_DIR = Path(r"E:\WordCut\libs\downloads")

PACKAGES = [
    ("nvidia-cudnn-cu12", "8.9.7.29"),
    ("nvidia-cublas-cu12", None),  # None = 取最新 12.x
]


def _urlopen(url: str, timeout: int = 120):
    """带代理的 urlopen（优先用环境变量，其次显式代理）。"""
    handlers = [urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})]
    opener = urllib.request.build_opener(*handlers)
    return opener.open(url, timeout=timeout)


def _pick_wheel(files: list[dict], name: str) -> dict:
    """从文件列表里挑 win_amd64 的 wheel（优先 cp310 无关的 py3-none）。"""
    wheels = [f for f in files if f["filename"].endswith("win_amd64.whl")]
    if not wheels:
        raise RuntimeError(f"{name} 没有 Windows wheel")
    # 优先 py3-none（纯 DLL 包）
    wheels.sort(key=lambda f: (0 if "py3-none" in f["filename"] else 1, f["filename"]))
    return wheels[0]


def _download(url: str, target: Path) -> None:
    """分块下载并打印进度。"""
    if target.is_file() and target.stat().st_size > 0:
        print(f"  已存在，跳过下载：{target.name}")
        return
    started = time.time()
    with _urlopen(url, timeout=300) as response, target.open("wb") as handle:
        total = int(response.headers.get("Content-Length") or 0)
        written = 0
        last_report = 0.0
        while True:
            chunk = response.read(4 * 1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)
            written += len(chunk)
            now = time.time()
            if now - last_report > 5:
                last_report = now
                mb = written / 1048576
                if total:
                    print(f"  {mb:.0f}/{total/1048576:.0f} MB "
                          f"({mb*100/(total/1048576):.0f}%) {now-started:.0f}s")
                else:
                    print(f"  {mb:.0f} MB {now-started:.0f}s")
    print(f"  完成：{target.name}（{target.stat().st_size/1048576:.0f} MB，"
          f"{time.time()-started:.0f}s）")


def _extract(wheel: Path, dest: Path) -> list[str]:
    """把 wheel 里的 .dll 解到 dest，返回解出的文件名列表。"""
    dest.mkdir(parents=True, exist_ok=True)
    extracted: list[str] = []
    with zipfile.ZipFile(wheel) as archive:
        for info in archive.infolist():
            if not info.filename.lower().endswith(".dll"):
                continue
            target = dest / Path(info.filename).name
            with archive.open(info) as src, target.open("wb") as out:
                out.write(src.read())
            extracted.append(target.name)
    return extracted


def main() -> int:
    DEST.mkdir(parents=True, exist_ok=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    all_dlls: list[str] = []

    for name, version in PACKAGES:
        print(f"\n=== {name} ===")
        meta = json.load(_urlopen(f"https://pypi.org/pypi/{name}/json", timeout=60))
        releases = meta["releases"]
        if version is None:
            candidates = [
                v for v in releases
                if v.startswith("12.") and any(
                    f["filename"].endswith("win_amd64.whl") for f in releases[v]
                )
            ]
            if not candidates:
                print("  找不到 12.x 的 Windows wheel，跳过")
                continue
            version = max(candidates, key=lambda v: [int(x) for x in v.split(".") if x.isdigit()])
        wheel_info = _pick_wheel(releases.get(version, []), name)
        print(f"  选中：{wheel_info['filename']}（{wheel_info['size']/1048576:.0f} MB）")

        wheel_path = DOWNLOAD_DIR / wheel_info["filename"]
        _download(wheel_info["url"], wheel_path)
        dlls = _extract(wheel_path, DEST / "bin")
        print(f"  解出 {len(dlls)} 个 DLL：{', '.join(sorted(dlls)[:6])}…")
        all_dlls.extend(dlls)

    print(f"\n完成：{DEST / 'bin'} 共 {len(set(all_dlls))} 个 DLL")
    print("下一步：python scripts/check_cuda.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
