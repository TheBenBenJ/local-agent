"""Second passe image : le modèle chargé s'il a la vision, jamais un second checkpoint."""

from __future__ import annotations

import re
from pathlib import Path

from . import evidence, prompts
from .config import Config
from .files import GuardrailError
from .mlx import Completion, MlxError
from .report import Report

MAX_VISION_IMAGES = 4
VISION_HINTS = re.compile(
    r"\b(layout|filtre|filter|bouton|button|disabled|couleur|color|fusion|"
    r"merged|compare|comparer|colonne|column|selected|sélection|selection|"
    r"dropdown|checkbox|vide|empty|interface)\b",
    re.IGNORECASE,
)


def page_needs_vision(table: list[list[str]] | None, task: str | None) -> bool:
    if VISION_HINTS.search(task or ""):
        return True
    if not table:
        return True
    header = table[0] if table else []
    return any(not str(cell).strip() for cell in header)


def select_pages(pages: list[dict], task: str | None) -> list[dict]:
    hinted = bool(VISION_HINTS.search(task or ""))
    chosen = []
    for page in pages:
        if hinted or page_needs_vision(page.get("table"), task):
            chosen.append(page)
        if len(chosen) >= MAX_VISION_IMAGES:
            break
    if len(pages) > 1 and hinted:
        return pages[:MAX_VISION_IMAGES]
    return chosen


def _client_has_vision(client: object | None) -> bool:
    if client is None:
        return False
    checker = getattr(client, "supports_vision", None)
    return bool(checker()) if callable(checker) else False


def _prompt(pages: list[dict], task: str | None) -> str:
    parts = [
        "OCR already extracted the on-screen text. It is the source of truth for numbers and labels.",
        "Look at the image only to fix layout OCR cannot: merged headers, column assignment, "
        "selected filters, disabled buttons, empty states.",
    ]
    if task:
        parts.append(f"Task: {task}")
    for page in pages:
        parts.append(f"\n## {page.get('label') or page.get('path')}")
        excerpt = str(page.get("excerpt") or "").strip()
        parts.append(excerpt or "(no table reconstructed; this is likely a form or UI screenshot)")
    parts.append("\n" + prompts.JSON_VISION)
    return "\n".join(parts)


def _as_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()][:8]


def enrich(
    config: Config,
    client: object | None,
    report: Report,
    pages: list[dict],
    task: str | None = None,
) -> Report:
    if not config.vision:
        report.stats["vision"] = "disabled"
        return report
    if not _client_has_vision(client):
        report.stats["vision"] = "unavailable"
        return report
    chosen = select_pages(pages, task)
    if not chosen:
        report.stats["vision"] = "skipped"
        return report
    images = [Path(page["path"]) for page in chosen if page.get("path")]
    images = [path for path in images if path.is_file()][:MAX_VISION_IMAGES]
    if not images:
        report.stats["vision"] = "skipped"
        return report
    assert hasattr(client, "complete")
    try:
        completion = client.complete(
            _prompt(chosen, task),
            prompts.SYSTEM_VISION,
            max_tokens=min(800, config.max_completion_tokens),
            temperature=0.0,
            images=images,
        )
    except MlxError as error:
        report.stats["vision"] = "failed"
        report.errors.append(f"local vision: {error}")
        return report
    text = completion.text if isinstance(completion, Completion) else str(getattr(completion, "text", "") or "")
    payload = prompts.extract_raw(text) or prompts.extract_json(text)
    notes = _as_list(payload.get("notes"))
    ui = _as_list(payload.get("ui"))
    headers = _as_list(payload.get("header_split"))
    extra: list[str] = []
    extra.extend(f"Local vision: {item}" for item in notes)
    extra.extend(f"UI: {item}" for item in ui)
    if headers:
        extra.append("Local vision headers: " + " | ".join(headers[:12]))
    if extra:
        report.findings = extra + report.findings
        block = "### Local vision\n" + "\n".join(f"- {item}" for item in extra)
        report.details = (report.details + "\n\n" + block).strip() if report.details else block
    for page in chosen:
        image_id = str(page.get("image_id") or "")
        if not image_id:
            continue
        try:
            packet = evidence.load(image_id)
        except GuardrailError:
            continue
        packet["vision"] = {"notes": notes, "ui": ui, "header_split": headers}
        evidence.store(image_id, packet)
    report.stats["vision"] = "applied"
    report.stats["vision_images"] = len(images)
    report.summary = (
        report.summary.rstrip()
        + " Same local model filled layout/UI gaps; OCR numbers stay authoritative."
    )
    return report


def reason(config: Config, client: object | None, path: Path, ocr_text: str, task: str | None) -> dict:
    """Optional VLM on one crop. OCR text is already extracted; do not dump the screenshot."""
    if not config.vision:
        return {"vision": "disabled", "notes": []}
    if not _client_has_vision(client):
        return {"vision": "unavailable", "notes": []}
    if not path.is_file():
        raise GuardrailError(f"image not found: {path}")
    assert hasattr(client, "complete")
    prompt = (
        "OCR already extracted this text (source of truth for numbers and labels):\n"
        + (ocr_text.strip() or "(empty)")[:4000]
        + "\n\nLook at the image only for layout OCR cannot capture."
        + (f"\nTask: {task}" if task else "")
        + "\n\n"
        + prompts.JSON_VISION
    )
    try:
        completion = client.complete(prompt, prompts.SYSTEM_VISION, max_tokens=400, temperature=0.0, images=[path])
    except MlxError as error:
        return {"vision": "failed", "error": str(error), "notes": []}
    text = completion.text if isinstance(completion, Completion) else str(getattr(completion, "text", "") or "")
    payload = prompts.extract_raw(text) or prompts.extract_json(text) or {}
    return {
        "vision": "applied",
        "notes": _as_list(payload.get("notes")),
        "ui": _as_list(payload.get("ui")),
        "header_split": _as_list(payload.get("header_split")),
    }
