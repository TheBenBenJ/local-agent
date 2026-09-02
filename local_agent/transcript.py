"""Classify a Cursor/Claude jsonl transcript without loading it into an LLM.

Cursor agent transcripts store tool *calls*, not tool *results*. File bodies that
were billed at the time are missing from the jsonl. Eligible bytes are reconstructed
from Read paths still on disk. This is not billed Claude usage.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from .benchmark import tokens_from_chars

CHARS_PER_LINE = 200
READ_NAMES = {"Read", "read_file"}
IMAGE_SUFFIX = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def classify_jsonl(path: Path) -> dict:
    target = Path(path).expanduser()
    if not target.is_file():
        raise ValueError(f"transcript not found: {target}")
    user_chars = 0
    assistant_chars = 0
    tool_call_chars = 0
    reads: dict[str, int] = {}
    images: dict[str, int] = {}
    missing_reads = 0
    grep_calls = 0
    lines = 0
    with target.open(encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            lines += 1
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            role = str(obj.get("role") or "")
            content = (obj.get("message") or {}).get("content") if isinstance(obj.get("message"), dict) else obj.get("content")
            if isinstance(content, str):
                if role == "user":
                    user_chars += len(content)
                elif role == "assistant":
                    assistant_chars += len(content)
                continue
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                kind = str(block.get("type") or "")
                if kind == "text":
                    text = str(block.get("text") or "")
                    if role == "user":
                        user_chars += len(text)
                    else:
                        assistant_chars += len(text)
                elif kind == "tool_use":
                    tool_call_chars += len(json.dumps(block, ensure_ascii=False))
                    name = str(block.get("name") or "")
                    payload = block.get("input") if isinstance(block.get("input"), dict) else {}
                    if name in {"Grep", "grep", "search_repo"}:
                        grep_calls += 1
                    if name in READ_NAMES:
                        file_path = Path(str(payload.get("path") or ""))
                        if not file_path.is_file():
                            missing_reads += 1
                            continue
                        size = file_path.stat().st_size
                        limit = payload.get("limit")
                        estimated = size
                        if isinstance(limit, int) and limit > 0:
                            estimated = min(size, limit * CHARS_PER_LINE)
                        previous = reads.get(str(file_path), 0)
                        reads[str(file_path)] = max(previous, estimated)
                        if file_path.suffix.lower() in IMAGE_SUFFIX:
                            images[str(file_path)] = size
    jsonl_bytes = target.stat().st_size
    already = user_chars + assistant_chars + tool_call_chars
    eligible = sum(reads.values())
    return {
        "kind": "transcript",
        "path": str(target),
        "note": (
            "Cursor jsonl stores tool calls, not tool results. "
            "Not billed Claude tokens. eligible_read_chars reconstructs Read paths still on disk."
        ),
        "lines": lines,
        "jsonl_bytes": jsonl_bytes,
        "already_in_jsonl_chars": already,
        "user_chars": user_chars,
        "assistant_chars": assistant_chars,
        "tool_call_chars": tool_call_chars,
        "read_files": len(reads),
        "read_missing": missing_reads,
        "grep_calls": grep_calls,
        "image_files": len(images),
        "eligible_read_chars": eligible,
        "already_in_jsonl_tokens": tokens_from_chars(already),
        "eligible_read_tokens": tokens_from_chars(eligible),
        "jsonl_tokens": tokens_from_chars(jsonl_bytes),
        "must_remain_visible": "user messages and this chat's instructions",
        "non_interceptable": "assistant text, tool-call JSON already in the thread, missing tool results",
        "interceptable_if_rerun": "Read targets still on disk, via local_search / local_task instead of attaching the file",
    }


def classify_day(folder: Path) -> dict:
    root = Path(folder).expanduser()
    if not root.is_dir():
        raise ValueError(f"transcript folder not found: {root}")
    files = sorted(root.rglob("*.jsonl"))
    rows = []
    totals = defaultdict(int)
    for item in files:
        row = classify_jsonl(item)
        rows.append(
            {
                "path": row["path"],
                "jsonl_bytes": row["jsonl_bytes"],
                "already_in_jsonl_tokens": row["already_in_jsonl_tokens"],
                "eligible_read_tokens": row["eligible_read_tokens"],
                "read_files": row["read_files"],
            }
        )
        for key in (
            "jsonl_bytes",
            "already_in_jsonl_chars",
            "eligible_read_chars",
            "read_files",
            "read_missing",
            "grep_calls",
            "lines",
        ):
            totals[key] += int(row.get(key) or 0)
    return {
        "kind": "transcript-day",
        "note": (
            "Sum of Cursor jsonl under this folder. Not billed Claude tokens. "
            "Tool results are absent, so billed context at the time was larger than jsonl_bytes."
        ),
        "folder": str(root),
        "transcripts": len(files),
        "jsonl_bytes": totals["jsonl_bytes"],
        "jsonl_tokens": tokens_from_chars(totals["jsonl_bytes"]),
        "already_in_jsonl_tokens": tokens_from_chars(totals["already_in_jsonl_chars"]),
        "eligible_read_tokens": tokens_from_chars(totals["eligible_read_chars"]),
        "read_files": totals["read_files"],
        "read_missing": totals["read_missing"],
        "grep_calls": totals["grep_calls"],
        "lines": totals["lines"],
        "cases": rows[:40],
    }
