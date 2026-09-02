#!/usr/bin/env python3
"""OCR : garde-fous de chemin, regroupement, rapport sans modèle."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_agent.config import get_config  # noqa: E402
from local_agent.files import GuardrailError  # noqa: E402
from local_agent.ocr import (  # noqa: E402
    collect_paths,
    read_images,
    resolve_image_path,
    rows_from_lines,
)

# PNG 1x1 : assez pour les garde-fous, pas pour un vrai OCR.
PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000"
    "907753de0000000c4944415408d763f8cf0000020101e2d26d3f0000000049454e44ae426082"
)


def check(name: str, condition: bool) -> None:
    status = "OK" if condition else "KO"
    print(f"  {status}  {name}")
    if not condition:
        raise SystemExit(1)


def _raises(callback) -> bool:
    try:
        callback()
    except (GuardrailError, ValueError):
        return True
    return False


def _render_sample(text: str) -> Path | None:
    if not shutil.which("swiftc"):
        return None
    source = Path(__file__).resolve().parent / "render_text.swift"
    work = Path(tempfile.mkdtemp())
    binary = work / "render_text"
    compile_proc = subprocess.run(
        ["swiftc", "-O", "-o", str(binary), str(source)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    if compile_proc.returncode != 0 or not binary.is_file():
        print(compile_proc.stderr)
        return None
    image = work / "sample.png"
    process = subprocess.run(
        [str(binary), text, str(image)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    if process.returncode != 0 or not image.is_file():
        print(process.stderr)
        return None
    return image


def main() -> None:
    config = get_config()
    check("refuse un appel vide", _raises(lambda: collect_paths(None, None)))
    check("fusionne path et paths", collect_paths("a.png", ["b.png"]) == ["a.png", "b.png"])
    check("plafonne à 10 images", _raises(lambda: collect_paths(None, [f"{i}.png" for i in range(11)])))

    lines = [
        {"text": "Erreur", "confidence": 0.9, "x": 0.1, "y": 0.8},
        {"text": "404", "confidence": 0.9, "x": 0.5, "y": 0.8},
        {"text": "Retour", "confidence": 0.9, "x": 0.1, "y": 0.4},
        {"text": "bruit", "confidence": 0.1, "x": 0.1, "y": 0.2},
    ]
    rows = rows_from_lines(lines)
    check("même ligne horizontale fusionnée", rows[0] == "Erreur  404")
    check("ligne plus basse séparée", rows[1] == "Retour")
    check("confiance trop basse écartée", len(rows) == 2)

    with tempfile.TemporaryDirectory() as raw:
        folder = Path(raw)
        image = folder / "ecran.png"
        image.write_bytes(PNG_1X1)
        resolved = resolve_image_path(config, str(image))
        check("accepte un PNG hors dépôt", resolved == image.resolve())

        secret = folder / "api-secret.png"
        secret.write_bytes(PNG_1X1)
        check("refuse un nom sensible", _raises(lambda: resolve_image_path(config, str(secret))))

        text_file = folder / "notes.txt"
        text_file.write_text("pas une image", encoding="utf-8")
        check("refuse une extension non image", _raises(lambda: resolve_image_path(config, str(text_file))))

        nested = folder / ".ssh" / "id.png"
        nested.parent.mkdir()
        nested.write_bytes(PNG_1X1)
        check("refuse un répertoire sensible", _raises(lambda: resolve_image_path(config, str(nested))))

        def runner(paths):
            return [
                {
                    "path": str(paths[0]),
                    "lines": [
                        {"text": "Type d'envoi", "confidence": 0.99, "x": 0.1, "y": 0.9},
                        {"text": "Erreur : champ obligatoire", "confidence": 0.99, "x": 0.1, "y": 0.5},
                    ],
                    "error": None,
                }
            ]

        report = read_images(config, str(image), task="erreur", runner=runner)
        check("n'appelle pas le LLM", report.stats.get("backend") == "injected")
        check("compte la source en équivalent base64", report.stats.get("source_caracteres") == int(len(PNG_1X1) * 4 / 3))
        check("filtre task sans modèle", any("champ obligatoire" in item for item in report.findings))
        check("détail contient le texte lu", "Type d'envoi" in report.details)

    sample = _render_sample("TypeEnvoiFacture")
    if sample is None:
        print("  SKIP  OCR Vision (swiftc absent)")
    else:
        live = read_images(config, str(sample))
        blob = (live.details or "") + "\n".join(live.findings)
        check("Vision lit le texte rendu", "TypeEnvoiFacture" in blob.replace(" ", ""))
        check("backend Vision sans LLM", live.stats.get("backend") == "macos-vision")

    print("tous les contrôles OCR passent")


if __name__ == "__main__":
    main()
