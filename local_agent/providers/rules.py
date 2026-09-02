"""Index lossless des regles projet : selection par le LLM local, texte ORIGINAL renvoye."""

from __future__ import annotations

from pathlib import Path

from ..config import Config
from ..files import is_denied, relative_to_root

CANDIDATES = (
    "CLAUDE.md",
    ".cursorrules",
    "AGENTS.md",
    ".local-agent.md",
)
RULE_GLOBS = (
    ".cursor/rules/*.mdc",
    ".cursor/rules/*.md",
    ".claude/rules/*.md",
)


def _collect(config: Config) -> list[Path]:
    root = config.repo_root
    found: list[Path] = []
    for name in CANDIDATES:
        path = root / name
        if path.is_file():
            found.append(path)
    for pattern in RULE_GLOBS:
        found.extend(sorted(root.glob(pattern)))
    return found


def _chunks(text: str, origin: str) -> list[dict]:
    blocks: list[dict] = []
    current: list[str] = []
    heading = origin
    for line in text.splitlines():
        if line.startswith("#"):
            if current:
                blocks.append({"id": heading, "source": origin, "text": "\n".join(current).strip()})
            heading = f"{origin}:{line.strip('#').strip()[:60]}"
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append({"id": heading, "source": origin, "text": "\n".join(current).strip()})
    return [block for block in blocks if len(block["text"]) > 40]


def select(config: Config, task: str, files: list) -> list[dict]:
    """Pick rule blocks whose headings or body share tokens with the task. Text is verbatim."""
    needles = {word.lower() for word in (task or "").split() if len(word) > 3}
    for item in files or []:
        needles.update(part.lower() for part in str(item).replace(".", " ").split("/") if len(part) > 3)
    hits: list[dict] = []
    for path in _collect(config):
        relative = relative_to_root(config, path)
        if is_denied(relative):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")[:80_000]
        except OSError:
            continue
        for block in _chunks(text, relative):
            blob = (block["id"] + " " + block["text"]).lower()
            if needles and not any(needle in blob for needle in needles):
                continue
            hits.append({"id": block["id"], "source": block["source"], "text": block["text"][:2500]})
            if len(hits) >= 6:
                return hits
    if not hits:
        for path in _collect(config)[:2]:
            relative = relative_to_root(config, path)
            text = path.read_text(encoding="utf-8", errors="replace")[:1500]
            hits.append({"id": relative, "source": relative, "text": text})
    return hits[:6]
