"""Heuristics d'escalade : ce n'est pas une probabilite scientifique."""

from __future__ import annotations

import re

HIGH_RISK = re.compile(
    r"\b(security|authentication|authorization|auth|securit|authenti|crypto|"
    r"password|mot de passe|secret|migration|drop table|"
    r"rm -rf|force.?push|sudo|public api|cross-?cutting|infrastructure|"
    r"suppression de donn|delete user|chiffrement|oauth|csrf|xss)\b",
    re.IGNORECASE,
)

MEDIUM_RISK = re.compile(
    r"\b(refactor|breaking change|compatib|public endpoint)\b",
    re.IGNORECASE,
)

READ_ONLY = "read_only"
PATCH = "patch"
AUTO = "auto"
SAFE = "safe"

ALIASES = {SAFE: PATCH, "readonly": READ_ONLY, "read-only": READ_ONLY}


def normalize_autonomy(value: str | None, default: str = READ_ONLY) -> str:
    text = (value or default or READ_ONLY).strip().lower()
    text = ALIASES.get(text, text)
    if text not in {READ_ONLY, PATCH, AUTO}:
        raise ValueError(f"autonomy must be read_only, patch/safe or auto, got {value!r}")
    return text


def confidence_band(value: float) -> str:
    if value >= 0.8:
        return "HIGH"
    if value >= 0.55:
        return "MEDIUM"
    return "LOW"


def parse_confidence(raw: object) -> float | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, str):
        band = raw.strip().upper()
        mapped = {"HIGH": 0.9, "MEDIUM": 0.65, "LOW": 0.35}
        if band in mapped:
            return mapped[band]
        try:
            return float(raw)
        except ValueError:
            return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def task_risk(task: str, extra: str = "") -> str:
    blob = f"{task}\n{extra}"
    if HIGH_RISK.search(blob):
        return "HIGH"
    if MEDIUM_RISK.search(blob):
        return "MEDIUM"
    return "LOW"


def score_confidence(
    *,
    found: bool,
    tests: str | None,
    risk: str,
    loop_stopped: bool,
    tool_errors: int,
    steps: int,
) -> float:
    """Escalation heuristic, not a calibrated probability. Documented as such in the packet."""
    value = 0.55
    if found:
        value += 0.15
    if tests == "PASS":
        value += 0.15
    elif tests == "FAIL":
        value -= 0.2
    if risk == "HIGH":
        value -= 0.25
    if loop_stopped:
        value -= 0.2
    if tool_errors:
        value -= min(0.2, 0.05 * tool_errors)
    if steps == 0:
        value -= 0.15
    return round(min(0.95, max(0.2, value)), 2)


def needs_claude(confidence: float, risk: str, threshold: float, tests: str | None = None) -> bool:
    if risk == "HIGH":
        return True
    if tests == "FAIL":
        return True
    if confidence_band(confidence) == "LOW":
        return True
    if confidence < threshold and threshold <= 0.55:
        return True
    return False
