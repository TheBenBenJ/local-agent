#!/usr/bin/env python3
"""Install identity used by ping and doctor."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_agent.version import SERVER_NAME, SERVER_VERSION, describe  # noqa: E402


def check(name: str, condition: bool) -> None:
    print(f"  {'OK' if condition else 'KO'}  {name}")
    if not condition:
        raise SystemExit(1)


def main() -> None:
    payload = describe()
    check("name", payload["name"] == SERVER_NAME)
    check("version", payload["version"] == SERVER_VERSION)
    check("code_root exists", Path(payload["code_root"]).is_dir())
    head = payload["git_head"]
    check("git_head present", len(head) >= 7)
    check("git_head hex", all(ch in "0123456789abcdef" for ch in head.lower()))
    print("version checks pass")


if __name__ == "__main__":
    main()
