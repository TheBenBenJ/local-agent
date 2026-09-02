#!/usr/bin/env python3
"""Run every tests/test_*.py file. Exit 1 on the first failure."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> int:
    failed: list[str] = []
    files = sorted(path for path in ROOT.glob("test_*.py") if path.name != Path(__file__).name)
    for path in files:
        print(f"\n=== {path.name} ===")
        result = subprocess.run([sys.executable, str(path)], cwd=str(ROOT.parent))
        if result.returncode:
            failed.append(path.name)
            print(f"FAIL {path.name} exit={result.returncode}")
    if failed:
        print("failed:", ", ".join(failed))
        return 1
    print(f"\n{len(files)} test files passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
