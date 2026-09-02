"""OCR local d'une capture : Vision (macOS) ou Tesseract. La passe layout est dans vision.py."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from . import evidence, grid, vision
from .config import Config
from .files import (
    DENIED_DIRECTORIES,
    DENIED_PATTERNS,
    GuardrailError,
    _run_ripgrep,
    is_git_ignored,
    relative_to_root,
    unlocked_directories,
)
from .report import Report

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".heic", ".heif", ".tif", ".tiff", ".bmp"}
SENSITIVE_PARTS = {".ssh", ".gnupg", ".aws", ".kube", ".docker"}
MAX_IMAGES = 10
MAX_IMAGE_BYTES = 8_000_000
MIN_CONFIDENCE = 0.3
ROW_Y_TOLERANCE = 0.015
# Trois captures d'admin tiennent ~8 Ko de texte ; le clamp code (900 tokens) tronquerait la troisième.
IMAGE_OUTPUT_TOKENS = 2000
BLOCK_Y_GAP = 0.06
BOX_PAD = 0.04
MAX_REGIONS = 12
MAX_EMBED_BYTES = 400_000
SALIENT = re.compile(
    r"erreur|error|warning|disabled|obligatoire|interdit|exception|404|échec|echec|invalid",
    re.IGNORECASE,
)

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


def _looks_like_image_glob(globs: list | None) -> bool:
    if not globs:
        return False
    haystack = " ".join(str(item).lower() for item in globs)
    return any(ext.lstrip(".") in haystack for ext in IMAGE_EXTENSIONS)


def list_image_files(
    config: Config,
    target: Path,
    globs: list | None = None,
    max_files: int | None = None,
) -> list[Path]:
    """Liste des captures sous la cible, sans le filtre binaire de la découverte code."""
    limit = max(1, min(max_files or MAX_IMAGES, MAX_IMAGES))
    if target.is_file():
        return [target] if target.suffix.lower() in IMAGE_EXTENSIONS else []

    unlocked = unlocked_directories(config, target)
    args = ["--files", "--no-messages"]
    if is_git_ignored(config, target):
        args.append("--no-ignore-vcs")
    patterns = list(globs) if _looks_like_image_glob(globs) else [f"*{ext}" for ext in sorted(IMAGE_EXTENSIONS)]
    for pattern in patterns:
        args += ["--glob", pattern]
    for directory in sorted(DENIED_DIRECTORIES - unlocked):
        args += ["--glob", f"!{directory}/**"]
    args.append(".")
    found: list[Path] = []
    for name in _run_ripgrep(args, target):
        path = target / name
        if path.suffix.lower() not in IMAGE_EXTENSIONS or not path.is_file():
            continue
        relative = relative_to_root(config, path)
        parts = Path(relative).parts
        if any(part in DENIED_DIRECTORIES and part not in unlocked for part in parts):
            continue
        if any(fnmatch.fnmatch(path.name, pattern) for pattern in DENIED_PATTERNS):
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size == 0 or size > MAX_IMAGE_BYTES:
            continue
        found.append(path)
        if len(found) >= limit:
            break
    return found


def _usable_lines(lines: list[dict]) -> list[dict]:
    usable: list[dict] = []
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
        usable.append(dict(line, text=text, confidence=confidence))
    return usable


def row_clusters(lines: list[dict], y_tolerance: float = ROW_Y_TOLERANCE) -> list[list[dict]]:
    """Regroupe les boîtes de même hauteur de lecture, gauche à droite."""
    rows: list[list[dict]] = []
    for line in _usable_lines(lines):
        if rows and abs(float(rows[-1][0].get("y") or 0) - float(line.get("y") or 0)) <= y_tolerance:
            rows[-1].append(line)
        else:
            rows.append([line])
    return rows


def rows_from_lines(lines: list[dict], y_tolerance: float = ROW_Y_TOLERANCE) -> list[str]:
    return ["  ".join(str(item.get("text") or "").strip() for item in row) for row in row_clusters(lines, y_tolerance)]


def _row_y(row: list[dict]) -> float:
    return sum(float(item.get("y") or 0) for item in row) / max(1, len(row))


def text_blocks(lines: list[dict], gap: float = BLOCK_Y_GAP) -> list[list[dict]]:
    rows = row_clusters(lines)
    if not rows:
        return []
    blocks: list[list[dict]] = [list(rows[0])]
    prev_y = _row_y(rows[0])
    for row in rows[1:]:
        y = _row_y(row)
        if abs(prev_y - y) > gap:
            blocks.append(list(row))
        else:
            blocks[-1].extend(row)
        prev_y = y
    return blocks


def union_box(lines: list[dict]) -> dict[str, float]:
    left = min(float(item.get("x") or 0) for item in lines)
    bottom = min(float(item.get("y") or 0) for item in lines)
    right = max(float(item.get("x") or 0) + float(item.get("width") or 0) for item in lines)
    top = max(float(item.get("y") or 0) + float(item.get("height") or 0) for item in lines)
    return {"x": left, "y": bottom, "width": max(0.01, right - left), "height": max(0.01, top - bottom)}


def pad_box(box: dict[str, float], pad: float = BOX_PAD) -> dict[str, float]:
    x = max(0.0, float(box["x"]) - pad)
    y = max(0.0, float(box["y"]) - pad)
    right = min(1.0, float(box["x"]) + float(box["width"]) + pad)
    top = min(1.0, float(box["y"]) + float(box["height"]) + pad)
    return {"x": x, "y": y, "width": max(0.01, right - x), "height": max(0.01, top - y)}


def image_id_for(path: Path) -> str:
    stamp = f"{path.resolve()}:{path.stat().st_mtime_ns}:{path.stat().st_size}"
    return hashlib.sha256(stamp.encode("utf-8")).hexdigest()[:8]


def make_regions(image_id: str, lines: list[dict], limit: int = MAX_REGIONS) -> list[dict]:
    scored: list[tuple] = []
    for index, block in enumerate(text_blocks(lines)):
        text = " | ".join(str(item.get("text") or "").strip() for item in block)
        conf = min(float(item.get("confidence") or 1) for item in block)
        salient = bool(SALIENT.search(text))
        scored.append((0 if salient else 1, -len(block), index, block, text, conf, salient))
    scored.sort()
    regions: list[dict] = []
    for rank, item in enumerate(scored[:limit], start=1):
        _s, _n, _i, block, text, conf, salient = item
        box = pad_box(union_box(block))
        regions.append(
            {
                "id": f"{image_id}-R{rank}",
                "source": f"image://{image_id}/R{rank}",
                "type": "image_region",
                "content": text[:400],
                "confidence": round(conf, 2),
                "box": box,
                "salient": salient,
            }
        )
    return regions


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


def _run_crop(source: Path, destination: Path, box: dict) -> None:
    binary = _ensure_vision_binary()
    process = subprocess.run(
        [
            str(binary),
            "crop",
            str(source),
            str(destination),
            str(box["x"]),
            str(box["y"]),
            str(box["width"]),
            str(box["height"]),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    if process.returncode != 0 or not destination.is_file():
        detail = (process.stderr or process.stdout or "crop failed").strip()[:400]
        raise GuardrailError(f"crop failed : {detail}")


def read_images(
    config: Config,
    path: str | None,
    paths: list | None = None,
    task: str | None = None,
    *,
    runner: OcrRunner | None = None,
    client: object | None = None,
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
    evidence_items: list[dict] = []
    pages: list[dict] = []
    line_count = 0
    table_count = 0
    needle = (task or "").strip().lower()
    matched: list[str] = []

    for file_path in resolved:
        payload = by_path.get(str(file_path), {"lines": [], "error": "no OCR result for this path"})
        error = payload.get("error")
        lines = payload.get("lines") or []
        if not isinstance(lines, list):
            lines = []
        rows = rows_from_lines(lines)
        table = grid.build_grid(lines)
        line_count += len(rows)
        label = relative_to_root(config, file_path)
        image_id = image_id_for(file_path)
        regions = make_regions(image_id, lines)
        evidence.store(
            image_id,
            {
                "path": str(file_path),
                "backend": backend,
                "size": file_path.stat().st_size,
                "lines": lines,
                "regions": regions,
                "grid": table,
            },
        )
        evidence_items.extend(regions)
        excerpt = grid.render_markdown_table(table) if table else "\n".join(rows)
        pages.append(
            {
                "path": file_path,
                "image_id": image_id,
                "label": label,
                "table": table,
                "excerpt": excerpt,
            }
        )
        if error:
            errors.append(f"{label}: {error}")
        if not rows:
            findings.append(f"{label}: no text detected. The image may be graphical only; look at it yourself.")
            continue
        if needle:
            haystack = rows + ([cell for row in table for cell in row] if table else [])
            matched.extend(f"{label}: {item}" for item in haystack if needle in item.lower())
        if table:
            table_count += 1
            findings.append(
                f"{label} (table {len(table)}x{len(table[0])}, id={image_id}): "
                + " | ".join(table[0][:8])
            )
            details_parts.append(
                "### " + label + f" (`{image_id}`)\n" + grid.render_markdown_table(table)
            )
        else:
            preview = rows[:8]
            findings.append(
                f"{label} ({len(rows)} lines, id={image_id}): " + " | ".join(preview)
            )
            details_parts.append("### " + label + f" (`{image_id}`)\n" + "\n".join(rows))

    if matched:
        findings = [f"Matches for {task!r}:"] + matched[:20] + findings

    summary = (
        f"OCR inventory of {len(resolved)} image(s), {line_count} text lines, backend={backend}. "
        "Verbatim on-screen text, not a recette verdict. Spreadsheet screenshots are rebuilt as a "
        "table from bounding boxes. Crop a region with local_image_crop when layout or colour "
        "matters; do not attach the full screenshot."
    )
    source_chars = sum(int(item.stat().st_size * 4 / 3) for item in resolved)
    report = Report(
        title="Image text (OCR)",
        summary=summary,
        findings=findings,
        files=[relative_to_root(config, item) for item in resolved],
        evidence=evidence_items,
        next_actions=[
            "Crop a material region with local_image_crop and its id (e.g. a832b1c4-R1).",
            "Look at the original only if a crop is not enough for layout or colour.",
        ],
        stats={
            "images": len(resolved),
            "lignes": line_count,
            "tables": table_count,
            "regions": len(evidence_items),
            "backend": backend,
            "source_caracteres": source_chars,
        },
        errors=errors,
        details="\n\n".join(details_parts),
    )
    return vision.enrich(config, client, report, pages, task)


def crop_region(config: Config, raw_id: str) -> tuple[Report, Path]:
    image_id, region_id = evidence.parse_region_id(raw_id)
    packet = evidence.load(image_id)
    source = Path(str(packet.get("path") or ""))
    if not source.is_file():
        raise GuardrailError(f"original image missing for {image_id}: {source}")
    resolve_image_path(config, str(source))
    wanted = f"{image_id}-{region_id}"
    match = None
    for item in packet.get("regions") or []:
        if str(item.get("id")) in {wanted, region_id, f"{image_id}-{region_id}"}:
            match = item
            break
    if match is None:
        known = ", ".join(str(item.get("id")) for item in (packet.get("regions") or [])[:12]) or "none"
        raise GuardrailError(f"unknown region {raw_id}. Known: {known}")
    box = match.get("box") or {}
    if not all(key in box for key in ("x", "y", "width", "height")):
        raise GuardrailError(f"region {raw_id} has no box")
    destination = evidence.EVIDENCE_DIR / f"{wanted}.png"
    evidence.EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    _run_crop(source, destination, box)
    original = int(packet.get("size") or source.stat().st_size)
    report = Report(
        title=f"Image crop {wanted}",
        summary=(
            f"Crop of `{wanted}` from {relative_to_root(config, source)}. "
            "Inspect this region only; the full screenshot stays local."
        ),
        findings=[str(match.get("content") or "")],
        files=[str(destination)],
        evidence=[dict(match, crop=str(destination))],
        stats={
            "backend": "crop",
            "original_octets": original,
            "crop_octets": destination.stat().st_size,
            "source_caracteres": int(original * 4 / 3),
        },
        details=f"crop={destination}",
    )
    return report, destination
