"""Deterministic high-signal extractors. The LLM never sees the raw source first."""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

_NOISE = [
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?"), "<date>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b0x[0-9a-f]+\b", re.I), "<hex>"),
    (re.compile(r"\b\d{3,}\b"), "<n>"),
]

LOG_LINE = re.compile(
    r"\b("
    r"ERROR|WARN(?:ING)?|FATAL|CRITICAL|EMERGENCY|ALERT|PANIC|"
    r"Exception|Traceback|Assertion(?:Error)?|"
    r"failed|failure|timeout|timed\s*out|"
    r"caused\s+by|ROOT_CAUSE"
    r")\b",
    re.IGNORECASE,
)

TEST_FAIL = re.compile(
    r"(FAIL(?:ED)?|ERROR|Assertion(?:Error)?|Failed asserting|E\s+|FATAL|"
    r"TypeError|NullPointer|undefined|not ok\b)",
    re.IGNORECASE,
)

KEEP_SIGNATURE = re.compile(r"ROOT_CAUSE|caused\s+by|panic|fatal", re.IGNORECASE)

MAX_LOG_BYTES = 8_000_000
MAX_LOG_HITS = 4_000
SURROUND = 2


def normalize_signature(text: str) -> str:
    blob = " ".join(str(text or "").split())
    for pattern, token in _NOISE:
        blob = pattern.sub(token, blob)
    return blob[:220]


def _surround(lines: list[str], index: int, radius: int = SURROUND) -> tuple[str, str]:
    start = max(1, index - radius)
    end = min(len(lines), index + radius)
    excerpt = "\n".join(f"{n}| {lines[n - 1]}" for n in range(start, end + 1))
    return excerpt, f"{start}-{end}"


def extract_log(path: Path) -> dict:
    """Scan a log for high-signal lines. Always keeps first, last, and rare unique signatures."""
    target = Path(path)
    if not target.is_file():
        return {"error": f"not a file: {path}", "hits": 0, "signatures": [], "excerpts": []}
    size = target.stat().st_size
    text = target.read_text(encoding="utf-8", errors="replace")
    if size > MAX_LOG_BYTES:
        text = text[: MAX_LOG_BYTES // 2] + "\n...\n" + text[-(MAX_LOG_BYTES // 2) :]
    lines = text.splitlines()
    hits: list[tuple[int, str]] = []
    for number, line in enumerate(lines, start=1):
        if LOG_LINE.search(line):
            hits.append((number, line.rstrip()))
            if len(hits) >= MAX_LOG_HITS:
                break
    groups: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for number, line in hits:
        groups[normalize_signature(line)].append((number, line))
    ranked = sorted(groups.items(), key=lambda item: (-len(item[1]), item[1][0][0]))
    last_order = sorted(groups.items(), key=lambda item: item[1][-1][0], reverse=True)
    selected: list[str] = []
    for signature, _rows in ranked[:5]:
        selected.append(signature)
    for signature, _rows in last_order[:3]:
        if signature not in selected:
            selected.append(signature)
    for signature in groups:
        sample = groups[signature][0][1]
        if KEEP_SIGNATURE.search(sample) and signature not in selected:
            selected.append(signature)
    signatures = []
    excerpts = []
    for signature in selected[:12]:
        rows = groups[signature]
        first_no, first_line = rows[0]
        last_no, last_line = rows[-1]
        first_excerpt, first_span = _surround(lines, first_no)
        item = {
            "signature": signature,
            "count": len(rows),
            "first_line": first_no,
            "last_line": last_no,
            "example": first_line[:300],
        }
        signatures.append(item)
        excerpts.append(
            {
                "kind": "raw_high_signal",
                "signature": signature,
                "count": len(rows),
                "lines": first_span,
                "content": f"{first_no}| {first_line}\n{first_excerpt}",
                "example": first_line[:300],
                "source_line": first_no,
            }
        )
        if last_no != first_no:
            last_excerpt, last_span = _surround(lines, last_no)
            excerpts.append(
                {
                    "kind": "raw_high_signal",
                    "signature": signature,
                    "count": len(rows),
                    "lines": last_span,
                    "content": f"{last_no}| {last_line}\n{last_excerpt}",
                    "example": last_line[:300],
                    "source_line": last_no,
                    "occurrence": "last",
                }
            )
    return {
        "path": str(target),
        "bytes": size,
        "total_lines": len(lines),
        "hits": len(hits),
        "unique_signatures": len(groups),
        "signatures": signatures,
        "excerpts": excerpts[:16],
        "first_hit": hits[0][0] if hits else None,
        "last_hit": hits[-1][0] if hits else None,
    }


def extract_test_output(output: str, *, kind: str = "test") -> dict:
    lines = str(output or "").splitlines()
    failures: list[dict] = []
    name = ""
    for number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if re.match(r"^(FAIL|ERROR|not ok)\b", stripped, re.I) or "::" in stripped and "FAIL" in stripped.upper():
            name = stripped[:200]
        if TEST_FAIL.search(stripped) and len(stripped) > 4:
            start = max(1, number - 1)
            end = min(len(lines), number + 8)
            excerpt = "\n".join(lines[start - 1 : end])
            failures.append(
                {
                    "kind": "raw_high_signal",
                    "test": name or kind,
                    "line": number,
                    "failure": stripped[:400],
                    "stack": excerpt[:1_500],
                    "lines": f"{start}-{end}",
                }
            )
            if len(failures) >= 8:
                break
    return {
        "kind": kind,
        "lines": len(lines),
        "failures": failures,
        "pass": not failures and "OK" in (output or "").upper(),
    }


def extract_diff(diff: str) -> dict:
    text = str(diff or "")
    files: list[str] = []
    additions = deletions = 0
    current = ""
    hunks = 0
    suspicious: list[str] = []
    markers = ("package-lock", ".min.js", "chmod", "vendor/", "dist/")
    for line in text.splitlines():
        if line.startswith("diff --git ") or line.startswith("+++ b/"):
            name = line.split()[-1]
            name = name[2:] if name.startswith("b/") else name
            if name not in files and name != "/dev/null":
                files.append(name)
                current = name
        elif line.startswith("+") and not line.startswith("+++"):
            additions += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1
        elif line.startswith("@@"):
            hunks += 1
        lowered = line.lower()
        if current and any(marker in lowered for marker in markers):
            flag = f"{current}: mechanical"
            if flag not in suspicious:
                suspicious.append(flag)
    return {
        "files": files[:40],
        "file_count": len(files),
        "hunks": hunks,
        "additions": additions,
        "deletions": deletions,
        "chars": len(text),
        "suspicious": suspicious[:8],
    }
