"""Format de retour compact et structuré, unique surface exposée à l'orchestrateur."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .config import Config

TRUNCATION_NOTICE = "\n\n[sortie tronquée par LOCAL_AGENT_MAX_OUTPUT_TOKENS]"


@dataclass
class Report:
    title: str
    summary: str = ""
    findings: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)
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
        lines.append(f"- (+{len(items) - limit} autres)")
    lines.append("")
    return lines


def render_markdown(report: Report, config: Config) -> str:
    lines = [f"# {report.title}", ""]
    if report.summary:
        lines += [report.summary.strip(), ""]
    lines += _section("Conclusions", report.findings)
    lines += _section("Fichiers concernés", report.files, limit=40)
    lines += _section("Emplacements", report.locations, limit=40)
    lines += _section("Risques et erreurs détectés", report.risks)
    lines += _section("Modifications réalisées", report.changes, limit=40)
    lines += _section("Prochaines actions", report.next_actions, limit=10)
    if report.errors:
        lines += _section("Échecs de l'agent local", report.errors)
    if report.stats:
        rendered = ", ".join(f"{key}={value}" for key, value in report.stats.items())
        lines += ["## Statistiques", rendered, ""]
    if report.details:
        lines += ["## Détails", report.details.strip(), ""]
    return clamp("\n".join(lines).strip(), config)


def render_json(report: Report, config: Config) -> str:
    return clamp(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), config)


def clamp(text: str, config: Config) -> str:
    limit = config.max_output_chars
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + TRUNCATION_NOTICE
