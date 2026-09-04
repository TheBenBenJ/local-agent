"""Comparaison de deux captures : hash, dimensions, OCR, diff pixel (Vision/CoreGraphics)."""

from __future__ import annotations

import json
import struct
import subprocess
from pathlib import Path

from . import ocr
from .config import Config
from .files import GuardrailError
from .report import Report
from .store import sha256_file


def png_size(path: Path) -> tuple[int, int] | None:
    with path.open("rb") as handle:
        header = handle.read(24)
    if header[:8] != b"\x89PNG\r\n\x1a\n" or len(header) < 24:
        return None
    width, height = struct.unpack(">II", header[16:24])
    return int(width), int(height)


def _lines(report: Report) -> list[str]:
    blob = (report.details or "") + "\n" + "\n".join(report.findings)
    return [line.strip() for line in blob.splitlines() if line.strip() and not line.startswith("#")]


def _ocr_payloads(report: Report) -> set[str]:
    payloads: set[str] = set()
    for line in _lines(report):
        text = line.split("): ", 1)[-1] if "): " in line else line
        for part in text.split("|"):
            cell = part.strip()
            if len(cell) < 3 or cell.startswith("/") or cell.startswith("#"):
                continue
            payloads.add(cell[:80])
    return payloads


def pixel_diff(left: Path, right: Path, threshold: float = 0.12) -> dict:
    binary = ocr._ensure_vision_binary()
    process = subprocess.run(
        [str(binary), "diff", str(left), str(right), str(threshold)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    if process.returncode != 0:
        detail = (process.stderr or process.stdout or "pixel diff failed").strip()[:400]
        raise GuardrailError(f"pixel diff failed : {detail}")
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError as error:
        raise GuardrailError("pixel diff returned non-JSON") from error
    if not isinstance(payload, dict):
        raise GuardrailError("pixel diff returned an unexpected payload")
    return payload


def compare_images(config: Config, reference: str, current: str, *, client=None) -> Report:
    left = ocr.resolve_image_path(config, reference)
    right = ocr.resolve_image_path(config, current)
    hash_left = sha256_file(left)
    hash_right = sha256_file(right)
    findings: list[str] = []
    evidence: list[dict] = []
    backend = "hash+ocr"
    ratio = 0.0
    if hash_left == hash_right:
        findings.append("HIGH: files are byte-identical (SHA256)")
        summary = "Screenshots are identical (SHA256)."
        evidence.append(
            {
                "type": "compare_verdict",
                "content": f"SHA256 identical {hash_left[:12]}",
            }
        )
    else:
        findings.append(f"HIGH: SHA256 hashes differ ({hash_left[:12]} vs {hash_right[:12]})")
        size_left = png_size(left)
        size_right = png_size(right)
        if size_left and size_right and size_left != size_right:
            findings.append(f"HIGH: dimensions {size_left} vs {size_right}")
        try:
            pixels = pixel_diff(left, right)
            backend = "hash+ocr+pixel"
            ratio = float(pixels.get("changedRatio") or 0)
            regions = pixels.get("regions") or []
            if ratio >= 0.15:
                findings.append(f"HIGH: pixel change covers {ratio:.0%} of the grid")
            elif ratio > 0:
                findings.append(f"MEDIUM: pixel change covers {ratio:.0%} of the grid")
            else:
                findings.append("Pixel grid: no cell above threshold")
            for region in regions[:5]:
                box = {
                    "x": region.get("x"),
                    "y": region.get("y"),
                    "width": region.get("width"),
                    "height": region.get("height"),
                }
                evidence.append(
                    {
                        "type": "pixel_region",
                        "content": f"pixel region score={region.get('score')}",
                        "confidence": round(float(region.get("score") or 0), 2),
                        "box": box,
                    }
                )
        except (GuardrailError, FileNotFoundError) as error:
            findings.append(f"Pixel diff skipped: {error}")
        ocr_left = ocr.read_images(config, str(left), None, None, client=client)
        ocr_right = ocr.read_images(config, str(right), None, None, client=client)
        set_left = _ocr_payloads(ocr_left)
        set_right = _ocr_payloads(ocr_right)
        missing = sorted(set_left - set_right)[:8]
        added = sorted(set_right - set_left)[:8]
        if missing:
            findings.append("MEDIUM: OCR text only in reference: " + " | ".join(missing[:3]))
        if added:
            findings.append("MEDIUM: OCR text only in current: " + " | ".join(added[:3]))
        if not missing and not added:
            findings.append("Text: identical at OCR granularity")
        evidence.extend((ocr_left.evidence or [])[:2])
        evidence.extend((ocr_right.evidence or [])[:2])
        summary = (
            f"{len(findings)} material differences between {left.name} and {right.name} "
            f"(SHA256 {hash_left[:12]} vs {hash_right[:12]}, pixel {ratio:.0%})."
        )
        evidence.insert(
            0,
            {
                "type": "compare_verdict",
                "content": (
                    f"SHA256 {hash_left[:12]} vs {hash_right[:12]}; "
                    f"pixel change {ratio:.0%}; {backend}"
                ),
            },
        )
    return Report(
        title="Image compare",
        summary=summary,
        findings=findings,
        files=[str(left), str(right)],
        evidence=evidence,
        stats={
            "backend": backend,
            "source_caracteres": ocr.attach_source_chars(left) + ocr.attach_source_chars(right),
            "sha_left": hash_left[:12],
            "sha_right": hash_right[:12],
        },
        next_actions=["Crop a differing region with local_image_crop rather than attaching both screenshots."],
    )
