"""Install identity shown by ping and doctor. Not a billed-usage meter."""

from __future__ import annotations

import subprocess
from pathlib import Path

SERVER_NAME = "local-agent"
SERVER_VERSION = "1.3.0"
ROOT = Path(__file__).resolve().parent.parent


def git_head() -> str:
    try:
        got = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if got.returncode == 0:
        return (got.stdout or "").strip()
    try:
        head = (ROOT / ".git" / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if head.startswith("ref:"):
        try:
            return (ROOT / ".git" / head.split(" ", 1)[1].strip()).read_text(encoding="utf-8").strip()[:12]
        except OSError:
            return ""
    return head[:12]


def describe() -> dict[str, str]:
    return {
        "name": SERVER_NAME,
        "version": SERVER_VERSION,
        "git_head": git_head(),
        "code_root": str(ROOT),
    }
