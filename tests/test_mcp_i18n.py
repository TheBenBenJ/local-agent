#!/usr/bin/env python3
"""The MCP surface shown to orchestrators worldwide must stay English."""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_agent.mcp import TOOLS  # noqa: E402
from local_agent.prompts import SYSTEM_ANALYST, SYSTEM_DERIVE, analyst_system  # noqa: E402

# Accents and leftover French wording that would leak into Claude/Cursor tool lists.
_FRENCH = re.compile(
    r"[àâäéèêëïîôùûüçÀÂÉÈÊËÏÎÔÙÛÜÇ]"
    r"|\b(Réponds|Consigne|dépôt|fichiers|renvoyé|privilégier|Garde-fous|Utilise-le)\b",
    re.IGNORECASE,
)


def check(name: str, condition: bool) -> None:
    status = "OK" if condition else "KO"
    print(f"  {status}  {name}")
    if not condition:
        raise SystemExit(1)


def _walk(node) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        found = []
        for value in node.values():
            found.extend(_walk(value))
        return found
    if isinstance(node, list):
        found = []
        for item in node:
            found.extend(_walk(item))
        return found
    return []


def main() -> None:
    names = [tool["name"] for tool in TOOLS]
    check("nine tools exposed", len(TOOLS) == 9)
    check("local_search present", "local_search" in names)
    check("local_image present", "local_image" in names)
    for tool in TOOLS:
        blob = "\n".join(_walk(tool))
        hit = _FRENCH.search(blob)
        check(f"{tool['name']} schema is English", hit is None)
    check("system prompt does not hardcode French replies", "français" not in SYSTEM_ANALYST.lower())
    check("system prompt does not hardcode Symfony", "Symfony" not in SYSTEM_ANALYST)
    check("system prompt follows the query language", "same language as the question" in SYSTEM_ANALYST)
    check("derivation prompt is not the analyst prompt", "surface wording" in SYSTEM_DERIVE)
    flavored = analyst_system("a Python repository")
    check("flavor is appended to the system prompt", flavored.endswith("a Python repository."))
    print("MCP international surface checks pass")


if __name__ == "__main__":
    main()
