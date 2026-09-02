"""Deterministic DIRECT / REDUCE / AGENT / CLAUDE router.

Local-first does not mean LLM-first. The cheapest reliable layer that can
solve the task wins. This module never calls a local LLM: using a 35B to
decide whether to use a 35B is the failure mode we are removing.

Heuristics (first match wins), all overridable via Config / env:

  1. LOCAL_AGENT_FORCE_TIER
  2. HIGH risk (auth, security, public API, destructive migration) → claude
  3. autonomy patch/auto → agent (needs a decision loop, then a proposal)
  4. image + (repo|jira|confluence) → agent
  5. estimated_raw_tokens <= LOCAL_AGENT_DIRECT_CONTEXT_THRESHOLD → direct
     including the invariant: if a synthesis packet would be >= the source
     and the source is under the threshold, keep the raw evidence
  6. large log / large test dump / large diff → reduce
  7. image-only (OCR / pixel) → direct
  8. explicit symbol or dotted identifier, no causal cross-file ask → direct
  9. causal / unknown whole-repo / multi-source → agent
 10. default: agent for a whole repo, reduce for a single large file
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import gateway
from .budget import tokens_from_chars
from .config import Config
from .risk import AUTO, PATCH, task_risk

TIERS = ("direct", "reduce", "agent", "claude")

WHOLE_REPO_BYTES = 10_000_000
SYNTHESIS_FLOOR_TOKENS = 400

STOPWORDS = {
    "where", "what", "which", "when", "why", "how", "find", "show", "the",
    "this", "that", "with", "from", "into", "class", "file", "files", "code",
    "repo", "task", "local", "please", "pour", "quoi", "comment", "est",
    "une", "des", "les", "dans", "pourqoi", "pourquoi", "liste", "list",
}

CAUSAL = re.compile(
    r"\b(why|pourquoi|root cause|cause racine|how come|remain visible|"
    r"still (shows|visible|broken)|comment se fait)\b",
    re.IGNORECASE,
)
CROSS_FILE = re.compile(
    r"\b(across files?|multi-?file|cross-?file|plusieurs fichiers|"
    r"correlate|corrél)\b",
    re.IGNORECASE,
)
TEST_WORDS = re.compile(
    r"\b(phpunit|pytest|phpstan|eslint|testdox|failing tests?|run the "
    r"(tests?|checks?|project checks?))\b",
    re.IGNORECASE,
)
DIFF_WORDS = re.compile(r"\b(git diff|review (this )?diff|working tree)\b", re.IGNORECASE)
DOTTED = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")
SNAKE = re.compile(r"\b[a-z][a-z0-9]+(?:_[a-z0-9]+)+\b")
CAMEL = re.compile(r"\b[A-Z][a-zA-Z0-9]{2,}\b")
QUOTED = re.compile(r"[`'\"]([A-Za-z_][A-Za-z0-9_.]+)[`'\"]")
LOG_NAME = re.compile(r"\.(log|out|txt)$", re.IGNORECASE)


@dataclass
class RouteDecision:
    tier: str
    reason: str
    estimated_raw_tokens: int
    estimated_packet_tokens: int
    risk: str
    source_types: list[str] = field(default_factory=list)
    first_tool: str = ""
    first_args: dict = field(default_factory=dict)
    symbols: list[str] = field(default_factory=list)
    needs_claude: bool = False

    def as_dict(self) -> dict:
        return {
            "tier": self.tier,
            "reason": self.reason,
            "estimated_raw_tokens": self.estimated_raw_tokens,
            "estimated_packet_tokens": self.estimated_packet_tokens,
            "risk": self.risk,
            "source_types": self.source_types,
            "first_tool": self.first_tool,
            "first_args": self.first_args,
            "needs_claude": self.needs_claude,
        }


def explicit_symbols(task: str) -> list[str]:
    text = str(task or "")
    found: list[str] = []
    for match in QUOTED.findall(text):
        if match.lower() not in STOPWORDS and match not in found:
            found.append(match)
    for match in DOTTED.findall(text):
        if match not in found:
            found.append(match)
    for match in SNAKE.findall(text):
        if match not in found:
            found.append(match)
    for match in CAMEL.findall(text):
        if match.lower() not in STOPWORDS and match not in found:
            found.append(match)
    return found[:6]


_SKIP_DIR = {".git", "node_modules", "vendor", "var", "__pycache__", ".venv"}


def _dir_bytes(root: Path, *, cap_files: int = 60, cap_bytes: int = 500_000) -> int:
    root = Path(root).resolve()
    total = 0
    count = 0
    try:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            try:
                relative = path.relative_to(root)
            except ValueError:
                continue
            if any(part in _SKIP_DIR for part in relative.parts[:-1]):
                continue
            try:
                total += path.stat().st_size
            except OSError:
                continue
            count += 1
            if count >= cap_files or total >= cap_bytes:
                return max(total, cap_bytes)
    except OSError:
        return WHOLE_REPO_BYTES
    return total


def _source_bytes(config: Config, source: gateway.Source) -> int:
    if source.scheme in {"jira", "confluence"}:
        return 8_000
    if source.scheme == "docs":
        return 4_000
    reference = source.reference
    if source.scheme in {"repo", "file"} and reference in {".", ""}:
        return _dir_bytes(config.repo_root)
    candidate = Path(reference).expanduser()
    if not candidate.is_absolute():
        candidate = config.repo_root / reference
    try:
        if candidate.is_file():
            return candidate.stat().st_size
        if candidate.is_dir():
            return _dir_bytes(candidate)
    except OSError:
        return WHOLE_REPO_BYTES if source.scheme == "repo" else 0
    return WHOLE_REPO_BYTES if source.scheme == "repo" else 0


def estimate_raw_tokens(config: Config, sources: list[gateway.Source]) -> int:
    return tokens_from_chars(sum(_source_bytes(config, item) for item in sources) or 0)


def initial_action_hint(
    task: str,
    sources: list[gateway.Source],
    *,
    symbols: list[str] | None = None,
) -> tuple[str, dict]:
    schemes = {item.scheme for item in sources}
    logs = [item for item in sources if item.scheme == "log"]
    images = [item for item in sources if item.scheme == "image"]
    if logs:
        return "inspect_log", {"path": logs[0].reference}
    if len(images) >= 2:
        return "compare_images", {
            "reference": images[0].reference,
            "current": images[1].reference,
        }
    if images:
        return "inspect_image", {"path": images[0].reference}
    blob = task or ""
    if re.search(r"\b(list files?|ls\b)", blob, re.IGNORECASE):
        focus = next(
            (
                item.reference
                for item in sources
                if item.scheme in {"repo", "file"} and item.reference not in {".", ""}
            ),
            ".",
        )
        return "list_files", {"path": focus}
    if TEST_WORDS.search(blob):
        return "run_check", {}
    if DIFF_WORDS.search(blob) or "git" in schemes:
        return "git_diff", {"scope": "worktree"}
    focus = next(
        (
            item.reference
            for item in sources
            if item.scheme in {"repo", "file"} and item.reference not in {".", ""}
        ),
        ".",
    )
    terms = symbols if symbols is not None else explicit_symbols(task)
    pattern = terms[0] if terms else (re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", blob) or ["TODO"])[0]
    return "search_repo", {"pattern": pattern, "path": focus}


def _decision(
    tier: str,
    reason: str,
    *,
    raw: int,
    risk: str,
    types: list[str],
    task: str,
    sources: list[gateway.Source],
    symbols: list[str],
) -> RouteDecision:
    packet = SYNTHESIS_FLOOR_TOKENS if tier != "direct" else min(raw, SYNTHESIS_FLOOR_TOKENS)
    if tier == "direct":
        packet = min(raw, 300) if raw else 80
    tool, args = initial_action_hint(task, sources, symbols=symbols)
    return RouteDecision(
        tier=tier,
        reason=reason,
        estimated_raw_tokens=raw,
        estimated_packet_tokens=packet,
        risk=risk,
        source_types=types,
        first_tool=tool,
        first_args=args,
        symbols=symbols,
        needs_claude=tier == "claude",
    )


def route_task(
    config: Config,
    task: str,
    sources: list | None,
    *,
    autonomy: str = "read_only",
    risk: str = "LOW",
    metadata: dict | None = None,
) -> RouteDecision:
    parsed = list(sources or [])
    if parsed and not isinstance(parsed[0], gateway.Source):
        parsed = gateway.parse_sources(parsed)
    if not parsed:
        parsed = gateway.parse_sources(None)
    types = [item.scheme for item in parsed]
    schemes = set(types)
    symbols = explicit_symbols(task)
    raw = estimate_raw_tokens(config, parsed)
    extra = metadata or {}
    if extra.get("estimated_raw_tokens"):
        raw = int(extra["estimated_raw_tokens"])
    threshold = int(getattr(config, "direct_context_threshold", 2000) or 2000)
    forced = str(getattr(config, "force_tier", "") or extra.get("force_tier") or "").strip().lower()
    blob = task or ""
    computed = task_risk(blob)
    if computed == "HIGH" or risk == "HIGH":
        risk = "HIGH"
    elif computed == "MEDIUM" and risk == "LOW":
        risk = "MEDIUM"

    def finish(tier: str, reason: str) -> RouteDecision:
        return _decision(tier, reason, raw=raw, risk=risk, types=types, task=blob, sources=parsed, symbols=symbols)

    if forced in TIERS:
        return finish(forced, f"forced tier {forced}")
    if risk == "HIGH":
        return finish("claude", "high-risk decision (auth, security, public API, or destructive change)")
    if autonomy in {PATCH, AUTO}:
        return finish("agent", "patch autonomy needs a bounded investigation then a proposal")
    if "image" in schemes and schemes & {"repo", "file", "jira", "confluence", "docs"}:
        return finish("agent", "multi-source screenshot plus repository or ticket")
    if "jira" in schemes and schemes & {"repo", "file"}:
        return finish("agent", "ticket plus repository")
    if raw <= threshold:
        reason = f"source ~{raw} tok <= {threshold}"
        if SYNTHESIS_FLOOR_TOKENS >= raw:
            reason += ", synthesis >= source"
        return finish("direct", reason)
    if "log" in schemes or any(LOG_NAME.search(item.reference or "") for item in parsed):
        return finish("reduce", f"large log (~{raw} tokens), high-signal extraction then one synthesis")
    if TEST_WORDS.search(blob):
        return finish("reduce", "large test output, failures only then optional synthesis")
    if DIFF_WORDS.search(blob):
        return finish("reduce", "large diff, structured hunks then optional synthesis")
    images = [item for item in parsed if item.scheme == "image"]
    if images and schemes <= {"image"}:
        return finish("direct", "screenshot OCR and structure, no VLM")
    if symbols and not CAUSAL.search(blob) and not CROSS_FILE.search(blob):
        return finish("direct", f"explicit symbol + {symbols[0]}")
    if CAUSAL.search(blob) or CROSS_FILE.search(blob):
        if "log" in schemes:
            return finish("reduce", "causal question on a log: extract high-signal first")
        return finish("agent", "multi-file causal investigation")
    if symbols:
        return finish("direct", f"explicit symbol + {symbols[0]}")
    bulky = [
        item
        for item in parsed
        if item.scheme in {"file", "data", "log"}
        or (item.scheme == "repo" and item.reference not in {".", ""} and Path(
            item.reference if Path(item.reference).is_absolute() else config.repo_root / item.reference
        ).is_file())
    ]
    if len(bulky) == 1 and raw > threshold:
        return finish("reduce", f"large reducible file (~{raw} tokens)")
    return finish("agent", "unknown whole-repository exploration")
