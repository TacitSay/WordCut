# -*- coding: utf-8 -*-
"""把 onnxruntime 的 Windows wheel 直接解包进 site-packages。

用途：faster-whisper 的批处理流水线（BatchedInferencePipeline）依赖 VAD，
而 VAD 需要 onnxruntime。这台机器 pip 走代理会报 FileNotFoundError，
所以用 urllib 下载 wheel（wheel 就是 zip）并解压到 site-packages。

只装 onnxruntime 本体：它的依赖（coloredlogs / flatbuffers / sympy /
protobuf / numpy）本机都已有。

用法::

    python scripts/install_onnxruntime.py
"""

from __future__ import annotations

import json
import sys
import sysconfig
import urllib.request
import zipfile
from pathlib import Path

PROXY = "http://127.0.0.1:7890"


def _urlopen(url: str, timeout: int = 120):
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})
    )
    return opener.open(url, timeout=timeout)


def main() -> int:
    meta = json.load(_urlopen("https://pypi.org/pypi/onnxruntime/json", timeout=60))
    py_tag = f"cp{sys.version_info.major}{sys.version_info.minor}"

    candidates = []
    for version, files in meta["releases"].items():
        for item in files:
            name = item["filename"]
            if name.endswith("win_amd64.whl") and py_tag in name:
                candidates.append((version, name, item["url"], item["size"]))

    if not candidates:
        print(f"找不到 {py_tag} 的 onnxruntime Windows wheel")
        return 1

    def version_key(v: str):
        return [int(p) for p in v.split(".") if p.isdigit()]

    version, filename, url, size = max(candidates, key=lambda c: version_key(c[0]))
    print(f"选中 {filename}（{size/1048576:.0f} MB）")

    site_packages = Path(sysconfig.get_paths()["purelib"])
    wheel_path = site_packages / filename
    if not wheel_path.is_file() or wheel_path.stat().st_size != size:
        with _urlopen(url, timeout=300) as response, wheel_path.open("wb") as handle:
            written = 0
            while True:
                chunk = response.read(4 * 1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                written += len(chunk)
                print(f"\r  {written/1048576:.0f}/{size/1048576:.0f} MB", end="")
        print()

    print(f"解包到 {site_packages} …")
    with zipfile.ZipFile(wheel_path) as archive:
        archive.extractall(site_packages)
    wheel_path.unlink(missing_ok=True)

    print("完成。验证：")
    import subprocess

    result = subprocess.run(
        [sys.executable, "-c", "import onnxruntime; print('onnxruntime', onnxruntime.__version__)"],
        capture_output=True,
        text=True,
    )
    print(result.stdout.strip() or result.stderr.strip())
    return 0 if result.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
