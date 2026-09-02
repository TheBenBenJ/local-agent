"""Format de retour compact et structuré, unique surface exposée à l'orchestrateur."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .config import Config

TRUNCATION_NOTICE = "\n\n[truncated by LOCAL_AGENT_MAX_OUTPUT_TOKENS]"


@dataclass
class Report:
    title: str
    summary: str = ""
    findings: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    stats: dict[str, object] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    details: str = ""

    def to_dict(self) -> dict:
        payload = {
            "title": self.title,
            "summary": self.summary,
            "findings": self.findings,
            "files": self.files,
            "locations": self.locations,
            "risks": self.risks,
            "changes": self.changes,
            "evidence": self.evidence,
            "next_actions": self.next_actions,
            "stats": self.stats,
            "errors": self.errors,
        }
        if self.details:
            payload["details"] = self.details
        return {key: value for key, value in payload.items() if value}


def _section(name: str, items: list[str], limit: int = 25) -> list[str]:
    if not items:
        return []
    lines = [f"## {name}"]
    for item in items[:limit]:
        lines.append(f"- {item}")
    if len(items) > limit:
        lines.append(f"- (+{len(items) - limit} more)")
    lines.append("")
    return lines


def _evidence_section(items: list[dict], limit: int = 15) -> list[str]:
    if not items:
        return []
    lines = ["## Evidence"]
    for item in items[:limit]:
        identifier = item.get("id") or item.get("source") or "?"
        kind = item.get("type") or "evidence"
        conf = item.get("confidence")
        content = str(item.get("content") or "").replace("\n", " ").strip()
        suffix = f", {conf}" if isinstance(conf, (int, float)) else ""
        lines.append(f"- `{identifier}` ({kind}{suffix}): {content[:220]}")
    if len(items) > limit:
        lines.append(f"- (+{len(items) - limit} more)")
    lines.append("")
    return lines


def render_markdown(report: Report, config: Config) -> str:
    lines = [f"# {report.title}", ""]
    if report.summary:
        lines += [report.summary.strip(), ""]
    lines += _section("Findings", report.findings)
    lines += _section("Files", report.files, limit=40)
    lines += _section("Locations", report.locations, limit=40)
    lines += _section("Risks", report.risks)
    lines += _section("Changes", report.changes, limit=40)
    lines += _evidence_section(report.evidence)
    lines += _section("Next actions", report.next_actions, limit=10)
    if report.errors:
        lines += _section("Local-agent failures", report.errors)
    stats = dict(report.stats)
    source_chars = stats.pop("source_caracteres", None)
    if stats:
        rendered = ", ".join(f"{key}={value}" for key, value in stats.items())
        lines += ["## Stats", rendered, ""]
    if report.details:
        lines += ["## Details", report.details.strip(), ""]
    text = "\n".join(lines).strip()
    # Make the saving visible on every call: that is the point of the tool.
    if isinstance(source_chars, int) and source_chars > len(text):
        ratio = source_chars / max(1, len(text))
        text += (
            f"\n\nContext avoided: ~{source_chars} characters examined locally, "
            f"report of {len(text)} characters, ~{ratio:.0f}x compression."
        )
    return clamp(text, config)


def render_json(report: Report, config: Config) -> str:
    payload = report.to_dict()
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    limit = config.max_output_chars
    if len(text) <= limit:
        return text
    evidence = list(payload.get("evidence") or [])
    while len(text) > limit and evidence:
        evidence.pop()
        payload["evidence"] = evidence
        stats = dict(payload.get("stats") or {})
        stats["evidence_truncated"] = True
        payload["stats"] = stats
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    if len(text) > limit:
        payload["findings"] = (payload.get("findings") or [])[:3]
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    if len(text) > limit:
        payload["summary"] = str(payload.get("summary") or "")[: max(400, limit // 3)]
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    return text


def clamp(text: str, config: Config) -> str:
    limit = config.max_output_chars
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + TRUNCATION_NOTICE
