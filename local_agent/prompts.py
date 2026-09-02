"""Consignes envoyées au modèle local et extraction de ses réponses structurées."""

from __future__ import annotations

import json
import re

SYSTEM_ANALYST = (
    "Tu es un assistant d'analyse de code au service d'un orchestrateur qui dispose d'un contexte très limité. "
    "Le dépôt est une application Symfony 7 / PHP 8.3 avec du TypeScript et du Twig, le vocabulaire métier est français. "
    "Tu réponds en français, de façon dense et factuelle, sans reformuler la consigne et sans recopier le code fourni. "
    "Tu ne renvoies jamais de longs extraits : uniquement des conclusions, des chemins de fichiers et des numéros de ligne. "
    "Emplacements d'abord, puis conclusion. Pas d'absence si tu as des emplacements à citer. Pas d'effectif inventé. "
    "Tu respectes strictement le format de sortie demandé, sans texte avant ni après."
)

SYSTEM_EDITOR = (
    "Tu es un assistant de refactoring mécanique sur un dépôt Symfony 7 / PHP 8.3. "
    "Tu appliques uniquement la consigne donnée, sans reformater le reste du fichier, sans changer le style, "
    "sans ajouter de commentaire et sans toucher à la logique métier. "
    "Si la consigne ne s'applique pas au fichier, tu le déclares inchangé. "
    "Tu respectes strictement le format de sortie demandé."
)

JSON_CONTRACT = """Réponds uniquement par un objet JSON valide, sans bloc de code markdown, avec ces clés :
{
  "summary": "3 phrases maximum",
  "findings": ["conclusion courte", "..."],
  "files": ["chemin/relatif.php", "..."],
  "locations": ["chemin/relatif.php:123 - ce qui s'y trouve", "..."],
  "risks": ["risque ou erreur detectee", "..."],
  "next_actions": ["action recommandee", "..."]
}
Listes vides autorisées. Maximum 8 entrées par liste, 140 caractères par entrée. Aucune clé supplémentaire.
Désigne les classes par leur nom court (AvenantStrategy), jamais par leur namespace complet.
Ne conclus à l'absence que si locations est vide. Les listes sont un échantillon, pas un recensement."""

FILE_ENVELOPE_CONTRACT = """Réponds exactement dans ce format, sans rien d'autre :
CHANGED: yes ou no
REASON: une phrase
---BEGIN FILE---
(contenu complet du fichier après modification, sans numéros de ligne, uniquement si CHANGED vaut yes)
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
    for candidate in _candidates(text):
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return normalize(payload)
    rescued = _rescue_fields(text)
    if rescued:
        return normalize(rescued)
    return normalize({"summary": text.strip()[:1500]})


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
