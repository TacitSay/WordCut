# -*- coding: utf-8 -*-
"""跑一遍 ``src/test_*.py`` 里的全部 smoke 测试，汇总结果。

项目不用 pytest：每个测试文件自带 ``check(name, cond)``，打印 ``PASS:`` /
``FAIL:`` 并以非零码退出。这个脚本负责把它们串起来，让"跑测试"只有一条命令：

    python scripts/run_tests.py
    python scripts/run_tests.py edl_render pipeline     # 只跑指定模块

退出码：全部通过为 0，否则为 1（方便接 CI 或 pre-commit）。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
#: 匹配测试自身的汇总行，例如 "结果: 22 通过, 0 失败"
SUMMARY_RE = re.compile(r"(\d+)\s*通过\s*[,，]\s*(\d+)\s*失败")


def discover(only: list[str]) -> list[Path]:
    files = sorted(SRC.glob("test_*.py"))
    if not only:
        return files
    wanted = {name if name.startswith("test_") else f"test_{name}" for name in only}
    wanted = {name if name.endswith(".py") else f"{name}.py" for name in wanted}
    return [path for path in files if path.name in wanted]


def run_one(path: Path) -> tuple[int, int, float]:
    """跑一个测试文件，返回 (通过数, 失败数, 耗时秒)。"""
    started = time.perf_counter()
    result = subprocess.run(
        [sys.executable, path.name],
        cwd=str(SRC),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    elapsed = time.perf_counter() - started
    output = (result.stdout or "") + (result.stderr or "")

    passed = len(re.findall(r"(?m)^PASS", output))
    failed = len(re.findall(r"(?m)^FAIL", output))
    match = SUMMARY_RE.search(output)
    if match:
        passed, failed = int(match.group(1)), int(match.group(2))

    status = "OK  " if failed == 0 and result.returncode == 0 else "FAIL"
    print(f"  [{status}] {path.name:<24} {passed:>3} 通过 {failed:>2} 失败  {elapsed:5.2f}s")
    if status == "FAIL":
        for line in output.splitlines():
            if line.startswith("FAIL") or "Traceback" in line or "Error" in line:
                print(f"         {line.strip()[:120]}")
    return passed, failed, elapsed


def main() -> int:
    parser = argparse.ArgumentParser(description="跑 WordCut 全部 smoke 测试")
    parser.add_argument("modules", nargs="*", help="只跑这些模块（如 edl_render）")
    args = parser.parse_args()

    files = discover(args.modules)
    if not files:
        print("没有找到匹配的测试文件")
        return 1

    print(f"共 {len(files)} 个测试文件，目录 {SRC}")
    total_pass = total_fail = 0
    for path in files:
        passed, failed, _ = run_one(path)
        total_pass += passed
        total_fail += failed

    print("-" * 62)
    print(f"合计: {total_pass} 通过, {total_fail} 失败")
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
