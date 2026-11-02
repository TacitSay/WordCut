# -*- coding: utf-8 -*-
"""检查运行环境，必要时把模型下载下来（离线部署/首次安装用）。

和"运行中自动下载"的区别：这里可以在**不启动服务**的情况下把一切准备好，
适合装完就给用户、或拷到没网的机器上前先预热。

做四件事：

1. 检查 Python 依赖是否齐全（缺了直接给安装命令）；
2. 检查 ffmpeg / ffprobe 是否可用（转码与视频导出必需）；
3. 确保 faster-whisper 模型存在，没有就下载（官方源 → 国内镜像）；
4. 检查 CUDA 运行库（缺失时提示用 ``scripts/fetch_cuda_libs.py`` 补齐，不影响 CPU 可用）。

用法::

    python scripts/ensure_models.py
    python scripts/ensure_models.py --model medium
    python scripts/ensure_models.py --no-download      # 只检查，不下载
"""

from __future__ import annotations

import argparse
import importlib
import shutil
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import model_fetch  # noqa: E402

#: 运行必需的模块 -> pip 包名
REQUIRED_MODULES = {
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "pydantic": "pydantic",
    "soundfile": "soundfile",
    "numpy": "numpy",
    "faster_whisper": "faster-whisper",
    "ctranslate2": "ctranslate2",
    "huggingface_hub": "huggingface-hub",
    "multipart": "python-multipart",
}

#: 批处理转写额外需要的（缺了会退回顺序模式，不算致命）
OPTIONAL_MODULES = {"onnxruntime": "onnxruntime"}


def check_python_deps() -> list[str]:
    missing: list[str] = []
    print("Python 依赖：")
    for module, package in REQUIRED_MODULES.items():
        try:
            importlib.import_module(module)
            print(f"  ✓ {package}")
        except Exception:  # noqa: BLE001 - 任何导入失败都算缺失
            print(f"  ✗ {package}（缺失）")
            missing.append(package)
    for module, package in OPTIONAL_MODULES.items():
        try:
            importlib.import_module(module)
            print(f"  ✓ {package}（可选，批处理转写用）")
        except Exception:  # noqa: BLE001
            print(f"  ! {package}（可选，缺失则退回顺序转写）")
    return missing


def check_ffmpeg() -> bool:
    print("外部工具：")
    ok = True
    for name in ("ffmpeg", "ffprobe"):
        path = shutil.which(name)
        if path:
            print(f"  ✓ {name}: {path}")
        else:
            # 项目也支持把 ffmpeg 放到 F:\ffmpeg\bin 或通过环境变量指定
            guessed = Path(r"F:\ffmpeg\bin") / f"{name}.exe"
            if guessed.is_file():
                print(f"  ✓ {name}: {guessed}（未在 PATH，但项目会自动找到）")
            else:
                print(f"  ✗ {name}（未找到，转码/视频导出会失败）")
                ok = False
    return ok


def check_cuda_libs() -> None:
    libs = Path(__file__).resolve().parent.parent / "libs" / "cuda" / "bin"
    print("CUDA 运行库：")
    if libs.is_dir() and any(libs.glob("cudnn*64*.dll")):
        print(f"  ✓ 已就绪：{libs}")
        return
    print(f"  ! 缺少 cuDNN/cuBLAS（目录 {libs}）")
    print("    GPU 转写需要它；不装也能用，会自动回退 CPU（慢约 4 倍）")
    print("    补齐：python scripts/fetch_cuda_libs.py")


def main() -> int:
    parser = argparse.ArgumentParser(description="WordCut 环境自检与模型预热")
    parser.add_argument("--model", default=None, help="模型名（tiny/base/small/medium/large-v3）")
    parser.add_argument("--no-download", action="store_true", help="只检查，不下载模型")
    args = parser.parse_args()

    print("=" * 56)
    print("  WordCut 环境自检")
    print("=" * 56)

    missing = check_python_deps()
    ffmpeg_ok = check_ffmpeg()
    check_cuda_libs()

    print("模型：")
    name = args.model or model_fetch.model_name()
    found = model_fetch.find_local(name)
    if found is not None:
        print(f"  ✓ 已就绪：{found}")
    elif args.no_download:
        print(f"  ✗ 未找到模型 {name}（已指定 --no-download，跳过下载）")
    else:
        try:
            path = model_fetch.ensure_model(name=name, log=lambda msg: print(f"  {msg}"))
            print(f"  ✓ 下载完成：{path}")
        except model_fetch.ModelNotAvailable as exc:
            print(f"  ✗ 下载失败：{exc}")

    print("=" * 56)
    problems = []
    if missing:
        problems.append("缺 Python 包：" + " ".join(missing))
    if not ffmpeg_ok:
        problems.append("缺 ffmpeg/ffprobe")
    if model_fetch.find_local(name) is None:
        problems.append(f"缺模型 {name}")

    if problems:
        print("还需要处理：")
        for item in problems:
            print(f"  - {item}")
        if missing:
            print("\n安装 Python 依赖：")
            print("  python -m pip install -r requirements.txt")
        print("\n详细说明见 README.md 的「快速开始」。")
        return 1

    print("全部就绪，可以启动：双击 start.bat")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
