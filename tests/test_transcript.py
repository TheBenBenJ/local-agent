#!/usr/bin/env python3
"""Cursor jsonl classifier: calls vs reconstructed Read paths."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_agent.transcript import classify_day, classify_jsonl  # noqa: E402


def check(name: str, condition: bool) -> None:
    print(f"  {'OK' if condition else 'KO'}  {name}")
    if not condition:
        raise SystemExit(1)


def main() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        source = root / "note.py"
        source.write_text("x" * 5000, encoding="utf-8")
        transcript = root / "chat.jsonl"
        events = [
            {
                "role": "user",
                "message": {"content": [{"type": "text", "text": "Where is route_task?"}]},
            },
            {
                "role": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "I will read the file."},
                        {"type": "tool_use", "name": "Read", "input": {"path": str(source), "limit": 4}},
                    ]
                },
            },
        ]
        transcript.write_text("\n".join(json.dumps(item) for item in events) + "\n", encoding="utf-8")
        row = classify_jsonl(transcript)
        check("user text counted", row["user_chars"] == len("Where is route_task?"))
        check("read file reconstructed", row["read_files"] == 1)
        check("eligible capped by limit", row["eligible_read_chars"] == 4 * 200)
        check("not billed", "Not billed" in row["note"])
        missing = classify_jsonl(transcript)  # path still exists
        check("missing stays 0", missing["read_missing"] == 0)
        day = classify_day(root)
        check("day sees jsonl", day["transcripts"] == 1)
        check("day eligible > 0", day["eligible_read_tokens"] > 0)
    print("tous les controles transcript passent")


if __name__ == "__main__":
    main()
