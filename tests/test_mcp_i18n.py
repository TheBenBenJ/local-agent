#!/usr/bin/env python3
"""The MCP surface shown to orchestrators worldwide must stay English."""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_agent.mcp import TOOLS  # noqa: E402
from local_agent.prompts import SYSTEM_ANALYST, SYSTEM_DERIVE, SYSTEM_VISION, analyst_system  # noqa: E402

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
    check("legacy tools kept", all(item in names for item in (
        "local_search", "local_analyze", "local_review", "local_fix",
        "local_test_analysis", "local_log_analysis", "local_image",
        "local_image_crop", "local_diff_review", "local_ping",
    )))
    check("local_task present", "local_task" in names)
    check("local_expand present", "local_expand" in names)
    check("local_expand mentions CODE-E", "CODE-E" in next(t["description"] for t in TOOLS if t["name"] == "local_expand"))
    check(
        "local_expand mentions 8-char image id",
        "8-char image id" in next(t["description"] for t in TOOLS if t["name"] == "local_expand"),
    )
    check("local_metrics present", "local_metrics" in names)
    check("local_image_compare present", "local_image_compare" in names)
    check("local_image_compare mentions pixel", "pixel" in next(t["description"] for t in TOOLS if t["name"] == "local_image_compare"))
    check("local_image present", "local_image" in names)
    check("local_image_crop present", "local_image_crop" in names)
    for tool in TOOLS:
        blob = "\n".join(_walk(tool))
        hit = _FRENCH.search(blob)
        check(f"{tool['name']} schema is English", hit is None)
    check("system prompt does not hardcode French replies", "français" not in SYSTEM_ANALYST.lower())
    check("system prompt does not hardcode Symfony", "Symfony" not in SYSTEM_ANALYST)
    check("system prompt follows the query language", "same language as the question" in SYSTEM_ANALYST)
    check("vision prompt follows the task language", "same language as the task" in SYSTEM_VISION)
    check("vision prompt forbids inventing values", "Never invent" in SYSTEM_VISION)
    check("derivation prompt is not the analyst prompt", "surface wording" in SYSTEM_DERIVE)
    flavored = analyst_system("a Python repository")
    check("flavor is appended to the system prompt", flavored.endswith("a Python repository."))
    print("MCP international surface checks pass")


if __name__ == "__main__":
    main()
