"""OCR local d'une capture : Vision (macOS) ou Tesseract, jamais le LLM de synthèse."""

from __future__ import annotations

import fnmatch
import json
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from .config import Config
from .files import DENIED_PATTERNS, GuardrailError, relative_to_root
from .report import Report

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".heic", ".heif", ".tif", ".tiff", ".bmp"}
SENSITIVE_PARTS = {".ssh", ".gnupg", ".aws", ".kube", ".docker"}
MAX_IMAGES = 10
MAX_IMAGE_BYTES = 8_000_000
MIN_CONFIDENCE = 0.3
ROW_Y_TOLERANCE = 0.015
# Trois captures d'admin tiennent ~8 Ko de texte ; le clamp code (900 tokens) tronquerait la troisième.
IMAGE_OUTPUT_TOKENS = 2000

OcrRunner = Callable[[list[Path]], list[dict]]

SWIFT_SOURCE = Path(__file__).resolve().parent / "ocr_vision.swift"
OCR_BINARY = Path(__file__).resolve().parent.parent / "var" / "local-ocr"


def backend_status() -> dict[str, object]:
    return {
        "preferred": "macos-vision" if shutil.which("swiftc") else "tesseract",
        "swiftc": bool(shutil.which("swiftc")),
        "binary_ready": OCR_BINARY.is_file(),
        "tesseract": bool(shutil.which("tesseract")),
    }


def image_config(config: Config) -> Config:
    return replace(config, max_output_tokens=max(config.max_output_tokens, IMAGE_OUTPUT_TOKENS))


def collect_paths(path: str | None, paths: list | None) -> list[str]:
    found: list[str] = []
    if path and str(path).strip():
        found.append(str(path).strip())
    for extra in paths or []:
        text = str(extra).strip()
        if text:
            found.append(text)
    if not found:
        raise ValueError("path or paths is required")
    if len(found) > MAX_IMAGES:
        raise ValueError(f"at most {MAX_IMAGES} images per call")
    return found


def resolve_image_path(config: Config, raw: str) -> Path:
    """Une capture vit rarement dans le dépôt : un chemin absolu hors git est accepté, un relatif non."""
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        from .files import ensure_usable_root

        ensure_usable_root(config)
        resolved = (config.repo_root / candidate).resolve()
        try:
            resolved.relative_to(config.repo_root)
        except ValueError as error:
            raise GuardrailError(f"chemin relatif hors du dépôt refusé : {resolved}") from error
    if not resolved.exists():
        raise GuardrailError(f"chemin inexistant : {resolved}")
    if not resolved.is_file():
        raise GuardrailError(f"pas un fichier image : {resolved}")
    if resolved.suffix.lower() not in IMAGE_EXTENSIONS:
        raise GuardrailError(f"extension image attendue ({', '.join(sorted(IMAGE_EXTENSIONS))}) : {resolved}")
    if any(part in SENSITIVE_PARTS for part in resolved.parts):
        raise GuardrailError(f"emplacement sensible refusé : {resolved}")
    if any(fnmatch.fnmatch(resolved.name, pattern) for pattern in DENIED_PATTERNS):
        raise GuardrailError(f"fichier sensible refusé : {relative_to_root(config, resolved)}")
    size = resolved.stat().st_size
    if size > MAX_IMAGE_BYTES:
        raise GuardrailError(f"image trop lourde ({size} octets, max {MAX_IMAGE_BYTES}) : {resolved}")
    if size == 0:
        raise GuardrailError(f"fichier vide : {resolved}")
    return resolved


def rows_from_lines(lines: list[dict], y_tolerance: float = ROW_Y_TOLERANCE) -> list[str]:
    """Regroupe les boîtes de même hauteur de lecture, gauche à droite."""
    rows: list[list[dict]] = []
    for line in lines:
        text = str(line.get("text") or "").strip()
        if not text:
            continue
        try:
            confidence = float(line.get("confidence") or 1)
        except (TypeError, ValueError):
            confidence = 1.0
        if confidence < MIN_CONFIDENCE:
            continue
        if rows and abs(float(rows[-1][0].get("y") or 0) - float(line.get("y") or 0)) <= y_tolerance:
            rows[-1].append(line)
        else:
            rows.append([line])
    return ["  ".join(str(item.get("text") or "").strip() for item in row) for row in rows]


def _ensure_vision_binary() -> Path:
    if not SWIFT_SOURCE.is_file():
        raise GuardrailError(f"source OCR absente : {SWIFT_SOURCE}")
    if OCR_BINARY.is_file() and OCR_BINARY.stat().st_mtime >= SWIFT_SOURCE.stat().st_mtime:
        return OCR_BINARY
    if not shutil.which("swiftc"):
        raise FileNotFoundError("swiftc")
    OCR_BINARY.parent.mkdir(parents=True, exist_ok=True)
    process = subprocess.run(
        ["swiftc", "-O", "-o", str(OCR_BINARY), str(SWIFT_SOURCE)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    if process.returncode != 0 or not OCR_BINARY.is_file():
        detail = (process.stderr or process.stdout or "swiftc failed").strip()[:400]
        raise GuardrailError(f"compilation OCR Vision impossible : {detail}")
    return OCR_BINARY


def _run_vision(paths: list[Path]) -> list[dict]:
    binary = _ensure_vision_binary()
    process = subprocess.run(
        [str(binary), *[str(path) for path in paths]],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    if process.returncode != 0:
        detail = (process.stderr or process.stdout or "ocr failed").strip()[:400]
        raise GuardrailError(f"OCR Vision failed : {detail}")
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError as error:
        raise GuardrailError("OCR Vision returned non-JSON output") from error
    if not isinstance(payload, list):
        raise GuardrailError("OCR Vision returned an unexpected payload")
    return payload


def _run_tesseract(paths: list[Path]) -> list[dict]:
    if not shutil.which("tesseract"):
        raise FileNotFoundError("tesseract")
    images: list[dict] = []
    for path in paths:
        process = subprocess.run(
            ["tesseract", str(path), "stdout", "-l", "fra+eng", "--psm", "6"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        if process.returncode != 0:
            images.append(
                {
                    "path": str(path),
                    "lines": [],
                    "error": (process.stderr or "tesseract failed").strip()[:400],
                }
            )
            continue
        lines = []
        rows = [row.strip() for row in process.stdout.splitlines() if row.strip()]
        total = max(1, len(rows))
        for index, text in enumerate(rows):
            lines.append(
                {
                    "text": text,
                    "confidence": 1.0,
                    "x": 0.0,
                    "y": 1.0 - (index / total),
                    "width": 1.0,
                    "height": 1.0 / total,
                }
            )
        images.append({"path": str(path), "lines": lines, "error": None})
    return images


def run_ocr(paths: list[Path]) -> tuple[list[dict], str]:
    """Préfère Vision pour ne pas décharger le modèle de synthèse. Tesseract seulement en repli."""
    if shutil.which("swiftc") or OCR_BINARY.is_file():
        try:
            return _run_vision(paths), "macos-vision"
        except FileNotFoundError:
            pass
    if shutil.which("tesseract"):
        return _run_tesseract(paths), "tesseract"
    raise GuardrailError(
        "no local OCR backend: install Xcode command-line tools (swiftc) or tesseract. "
        "The synthesis LLM is not used for screenshots."
    )


def read_images(
    config: Config,
    path: str | None,
    paths: list | None = None,
    task: str | None = None,
    *,
    runner: OcrRunner | None = None,
) -> Report:
    resolved = [resolve_image_path(config, item) for item in collect_paths(path, paths)]
    if runner is not None:
        raw_images, backend = runner(resolved), "injected"
    else:
        raw_images, backend = run_ocr(resolved)
    by_path = {str(Path(item.get("path") or "").resolve()): item for item in raw_images if isinstance(item, dict)}

    findings: list[str] = []
    details_parts: list[str] = []
    errors: list[str] = []
    line_count = 0
    needle = (task or "").strip().lower()
    matched: list[str] = []

    for file_path in resolved:
        payload = by_path.get(str(file_path), {"lines": [], "error": "no OCR result for this path"})
        error = payload.get("error")
        lines = payload.get("lines") or []
        if not isinstance(lines, list):
            lines = []
        rows = rows_from_lines(lines)
        line_count += len(rows)
        label = relative_to_root(config, file_path)
        if error:
            errors.append(f"{label}: {error}")
        if not rows:
            findings.append(f"{label}: no text detected. The image may be graphical only; look at it yourself.")
            continue
        if needle:
            matched.extend(f"{label}: {row}" for row in rows if needle in row.lower())
        preview = rows[:8]
        findings.append(f"{label} ({len(rows)} lines): " + " | ".join(preview))
        details_parts.append("### " + label + "\n" + "\n".join(rows))

    if matched:
        findings = [f"Matches for {task!r}:"] + matched[:20] + findings

    summary = (
        f"OCR inventory of {len(resolved)} image(s), {line_count} text lines, backend={backend}. "
        "On-screen text only, not a recette verdict and not layout or colour."
    )
    source_chars = sum(int(item.stat().st_size * 4 / 3) for item in resolved)
    return Report(
        title="Image text (OCR)",
        summary=summary,
        findings=findings,
        files=[relative_to_root(config, item) for item in resolved],
        next_actions=[
            "Inventory only: confirm layout, colour and recette yourself if they matter.",
        ],
        stats={
            "images": len(resolved),
            "lignes": line_count,
            "backend": backend,
            "source_caracteres": source_chars,
        },
        errors=errors,
        details="\n\n".join(details_parts),
    )
