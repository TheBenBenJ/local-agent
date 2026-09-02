"""Consignes envoyées au modèle local et extraction de ses réponses structurées."""

from __future__ import annotations

import json
import re

SYSTEM_ANALYST = (
    "You are a code-analysis assistant for an orchestrator with a very small context window. "
    "Be dense and factual. Do not restate the task. Do not copy source. "
    "Return only conclusions, file paths and line numbers. "
    "Locations first, then the conclusion. Never claim something is absent if you have locations to cite. "
    "Never invent counts. "
    "Write every user-facing string in the same language as the question or task; "
    "if none was given, match the language of comments in the excerpts. "
    "Obey the output format strictly, with no text before or after."
)

SYSTEM_EDITOR = (
    "You are a mechanical refactoring assistant. Apply only the given task. "
    "Do not reformat the rest of the file, do not change style, do not add comments, "
    "and do not change business logic. If the task does not apply, declare the file unchanged. "
    "Obey the output format strictly."
)


def analyst_system(flavor: str = "") -> str:
    """System prompt plus the detected stack, so a Python repo is never described as Symfony."""
    if not flavor:
        return SYSTEM_ANALYST
    return SYSTEM_ANALYST + f" The repository is {flavor}."


SYSTEM_DERIVE = (
    "You output only JSON. Patterns must match identifiers in the source code, "
    "never the surface wording of the question."
)

SYSTEM_VISION = (
    "You look at a screenshot to fill what OCR cannot: merged cells, column assignment, "
    "selected filters, disabled buttons, empty states, layout. "
    "The OCR table is the source of truth for numbers and labels. "
    "Never invent a value that is not clearly on the image. "
    "Do not restate the OCR table. "
    "Write every user-facing string in the same language as the task; "
    "if none was given, match the language of the OCR text."
)

JSON_VISION = """Reply with a single valid JSON object, no markdown fence, using these keys:
{
  "notes": ["layout or assignment facts OCR missed"],
  "ui": ["visible filters, selected values, disabled buttons, errors"],
  "header_split": ["true header per column if OCR glued two labels"]
}
Empty lists allowed. Max 8 items per list, 140 characters per item.
OCR numbers stay authoritative. Do not list cell values already in the OCR table
unless a column assignment is wrong.
Write string values in the same language as the task."""


SYSTEM_AGENT = (
    "You are the local execution layer of a coding agent. The orchestrator sent a mission, not files. "
    "Use tools to inspect the repository yourself. Prefer search_repo, then read_file windows. "
    "For two screenshots, compare_images then crop_image. Cite evidence ids (CODE-E, IMG-E, LOG-E). "
    "If a first tool result is already in the prompt, do not repeat that call and do not write a long plan. "
    "Do not dump full files. When you have enough, stop calling tools and reply with the JSON object only. "
    "If you are unsure or the change is high-risk, status needs_claude. Do not invent a fix. "
    "Write user-facing strings in the same language as the task."
)

JSON_TASK = """Reply with a single valid JSON object, no markdown fence:
{
  "status": "success or needs_claude",
  "summary": "3 sentences max",
  "root_cause": "one sentence or empty",
  "findings": ["short fact"],
  "files": ["relative/path"],
  "locations": ["relative/path:12"],
  "changes": ["proposed or applied change"],
  "questions": ["what Claude must decide"],
  "confidence": "HIGH or MEDIUM or LOW"
}
confidence is an escalation heuristic (HIGH/MEDIUM/LOW or 0-1), not a probability. Max 8 list items.
Write string values in the same language as the task."""


JSON_CONTRACT = """Reply with a single valid JSON object, no markdown fence, using these keys:
{
  "summary": "3 sentences max",
  "findings": ["short conclusion", "..."],
  "files": ["relative/path.php", "..."],
  "locations": ["relative/path.php:123 - what is there", "..."],
  "risks": ["risk or error found", "..."],
  "next_actions": ["recommended action", "..."]
}
Empty lists allowed. Max 8 items per list, 140 characters per item. No extra keys.
Use short class names (AvenantStrategy), never fully-qualified namespaces.
Only conclude absence if locations is empty. Lists are a sample, not a census.
Write string values in the same language as the question."""

FILE_ENVELOPE_CONTRACT = """Reply in this exact format, nothing else:
CHANGED: yes or no
REASON: one sentence
---BEGIN FILE---
(full file contents after the change, no line numbers, only if CHANGED is yes)
---END FILE---"""

BEGIN_MARKER = "---BEGIN FILE---"
END_MARKER = "---END FILE---"

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)
_LIST_KEYS = ("findings", "files", "locations", "risks", "next_actions")


JSON_ESCAPES = frozenset('"\\/bfnrtu')


def _escape_stray_backslashes(text: str) -> str:
    """Échappe les antislashs isolés, que les FQCN PHP (App\\Workflow\\X) rendent fréquents."""
    pieces: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char != "\\":
            pieces.append(char)
            index += 1
            continue
        following = text[index + 1] if index + 1 < length else ""
        if following and following in JSON_ESCAPES:
            pieces.append(char + following)
            index += 2
        else:
            pieces.append("\\\\")
            index += 1
    return "".join(pieces)


def _candidates(text: str) -> list[str]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned).strip()

    variants = [cleaned]
    block = _JSON_BLOCK.search(cleaned)
    if block:
        variants.append(block.group(0))
    salvaged = _salvage_truncated(cleaned)
    if salvaged:
        variants.append(salvaged)
    return variants + [_escape_stray_backslashes(variant) for variant in variants]


def extract_json(text: str) -> dict:
    """Récupère l'objet JSON d'une réponse, malgré le bruit, les balises markdown et les troncatures."""
    payload = extract_raw(text)
    if payload:
        return normalize(payload)
    rescued = _rescue_fields(text)
    if rescued:
        return normalize(rescued)
    return normalize({"summary": text.strip()[:1500]})


def extract_raw(text: str) -> dict:
    """Objet JSON tel quel, sans le réduire aux clés d'un rapport d'analyse."""
    for candidate in _candidates(text):
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def extract_list(text: str, key: str) -> list[str]:
    """Récupère une liste de chaînes d'une réponse JSON, même tronquée par la limite de tokens."""
    for candidate in _candidates(text):
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and isinstance(payload.get(key), list):
            return [str(item) for item in payload[key]]
    return []


_JSON_STRING = r'"((?:[^"\\]|\\.)*)"'


def _decode(raw: str) -> str:
    try:
        return json.loads(f'"{raw}"')
    except json.JSONDecodeError:
        return raw


def _rescue_fields(text: str) -> dict | None:
    """Repêche les champs un à un quand l'objet est cassé en son milieu, et pas seulement coupé à la fin.

    Le modèle ouvre parfois une clé sans fermer la liste précédente : refermer la fin ne répare rien, et
    sans ce repêchage le rapport présenterait le JSON brut en guise de résumé.
    """
    found = re.search(r'"summary"\s*:\s*' + _JSON_STRING, text, re.S)
    if not found:
        return None
    payload: dict[str, object] = {"summary": _decode(found.group(1))}
    stop = "|".join(_LIST_KEYS)
    for key in _LIST_KEYS:
        block = re.search(rf'"{key}"\s*:\s*\[(.*?)(?=\]|"(?:{stop})"\s*:)', text, re.S)
        if block:
            payload[key] = [_decode(item) for item in re.findall(_JSON_STRING, block.group(1))]
    return payload


def _salvage_truncated(text: str) -> str | None:
    """Referme un objet JSON coupé par la limite de tokens, en abandonnant l'entrée incomplète."""
    start = text.find("{")
    if start < 0:
        return None
    fragment = text[start:]
    if fragment.count('"') % 2:
        cut = fragment.rfind('"')
        if cut < 0:
            return None
        fragment = fragment[:cut]
    fragment = fragment.rstrip().rstrip(",")
    open_brackets = fragment.count("[") - fragment.count("]")
    open_braces = fragment.count("{") - fragment.count("}")
    if open_brackets < 0 or open_braces < 0:
        return None
    if fragment.rstrip().endswith(":"):
        fragment = fragment.rstrip()[:-1]
        cut = fragment.rfind('"')
        fragment = fragment[: fragment.rfind('"', 0, cut)].rstrip().rstrip(",")
    return fragment + "]" * open_brackets + "}" * open_braces


def normalize(payload: dict) -> dict:
    result: dict[str, object] = {"summary": str(payload.get("summary") or "").strip()}
    for key in _LIST_KEYS:
        raw = payload.get(key)
        if isinstance(raw, str):
            items = [raw]
        elif isinstance(raw, list):
            items = [str(item).strip() for item in raw]
        else:
            items = []
        result[key] = [item for item in items if item][:8]
    return result


def merge_payloads(payloads: list[dict]) -> dict:
    merged: dict[str, object] = {"summary": ""}
    summaries = [str(p.get("summary") or "").strip() for p in payloads]
    merged["summary"] = " ".join(s for s in summaries if s)[:1500]
    for key in _LIST_KEYS:
        seen: list[str] = []
        for payload in payloads:
            for item in payload.get(key) or []:  # type: ignore[union-attr]
                if item not in seen:
                    seen.append(str(item))
        merged[key] = seen
    return merged


def parse_file_envelope(text: str) -> tuple[bool, str, str | None]:
    changed = bool(re.search(r"^\s*CHANGED\s*:\s*yes", text, re.IGNORECASE | re.MULTILINE))
    reason_match = re.search(r"^\s*REASON\s*:\s*(.+)$", text, re.IGNORECASE | re.MULTILINE)
    reason = reason_match.group(1).strip() if reason_match else "sans justification"
    content: str | None = None
    if BEGIN_MARKER in text and END_MARKER in text:
        body = text.split(BEGIN_MARKER, 1)[1].rsplit(END_MARKER, 1)[0]
        body = body.strip("\n")
        if body.startswith("```"):
            body = re.sub(r"^```[a-zA-Z]*\n?", "", body)
            body = re.sub(r"\n?```$", "", body)
        content = body
    if not content:
        changed = False
    return changed, reason, content
