"""Cache des paquets de preuves : l'orchestrateur reçoit des ids, le brut reste sur disque."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from .files import GuardrailError

EVIDENCE_DIR = Path.home() / ".local-agent" / "evidence"
RETENTION_SECONDS = 7 * 24 * 3600


def prune() -> None:
    try:
        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        deadline = time.time() - RETENTION_SECONDS
        for path in EVIDENCE_DIR.iterdir():
            if path.stat().st_mtime < deadline:
                path.unlink(missing_ok=True)
    except OSError:
        pass


def store(image_id: str, payload: dict) -> Path:
    prune()
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    body = dict(payload)
    body["id"] = image_id
    body["created"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    target = EVIDENCE_DIR / f"{image_id}.json"
    target.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    return target


def load(image_id: str) -> dict:
    path = EVIDENCE_DIR / f"{image_id}.json"
    if not path.is_file():
        raise GuardrailError(
            f"unknown evidence id {image_id}: run local_image first, packets expire after 7 days"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise GuardrailError(f"corrupt evidence packet {image_id}") from error
    if not isinstance(payload, dict):
        raise GuardrailError(f"corrupt evidence packet {image_id}")
    return payload


def parse_region_id(raw: str) -> tuple[str, str]:
    text = str(raw or "").strip().replace("image://", "")
    if not text:
        raise ValueError("id is required, e.g. a832b1c4-R1")
    if "/" in text:
        image_id, region = text.split("/", 1)
        region = region if region.upper().startswith("R") else f"R{region}"
        return image_id, region.upper() if region[1:].isdigit() else region
    if "-R" in text.upper():
        head, tail = text.rsplit("-", 1)
        if tail.upper().startswith("R") and tail[1:].isdigit():
            return head, tail.upper()
    raise ValueError(f"id must look like a832b1c4-R1, got {raw!r}")
