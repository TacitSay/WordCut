# -*- coding: utf-8 -*-
"""检查本机能否用 CUDA 跑 faster-whisper，并给出缺什么。

用法::

    python scripts/check_cuda.py

为什么需要这个脚本：ctranslate2 在缺 cuDNN/cuBLAS 时不是抛异常，而是**直接
崩溃进程**（Windows 上表现为 0xC0000409 / 3221226505）。所以后台必须先用子进程
探测，探测不过就退回 CPU。这个脚本就是把那次探测单独跑一遍，方便定位原因。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import api  # noqa: E402


def main() -> int:
    print("=" * 66)
    print("WordCut CUDA 自检")
    print("=" * 66)

    try:
        import ctranslate2

        print(f"ctranslate2      : {ctranslate2.__version__}")
    except Exception as exc:  # noqa: BLE001
        print(f"ctranslate2      : 不可用（{exc}）")
        return 1

    count = api._cuda_device_count()
    print(f"CUDA 设备数      : {count}")

    try:
        model_path = api._resolve_model_path()
        print(f"模型目录         : {model_path}")
    except Exception as exc:  # noqa: BLE001
        print(f"模型目录         : 找不到（{exc}）")
        return 1

    if count <= 0:
        print("\n结论：没有可用的 CUDA 设备，转写只能用 CPU。")
        return 0

    print("\n正在子进程里实测 CUDA 推理（可能几秒到几十秒）…")
    usable = api._probe_cuda_usable(model_path)
    if usable:
        print("CUDA 可用        : 是")
        print("建议设置         : WORDCUT_WHISPER_DEVICE=auto（默认即会自动用 GPU）")
        return 0

    print("CUDA 可用        : 否")
    print(f"原因             : {api._cuda_probe_reason}")
    print(
        "\n常见修法（ctranslate2 4.4.0 需要 CUDA 12 + cuDNN 8）：\n"
        "  1. 安装 cuDNN 8.9.x（for CUDA 12），把 bin 目录加入 PATH；\n"
        "  2. 确认 cudnn_ops_infer64_8.dll / cudnn_cnn_infer64_8.dll 能在 PATH 里找到；\n"
        "  3. 确认 cublas64_12.dll 存在（CUDA 12 运行时）；\n"
        "  4. 重跑本脚本，看到“CUDA 可用: 是”即可。\n"
        "\n在装好之前，后端会自动退回 CPU，功能不受影响，只是慢。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
