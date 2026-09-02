#!/usr/bin/env python3
"""Grille OCR : reconstruction deterministe a partir des boites."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_agent.grid import build_grid, clean_cell, render_markdown_table  # noqa: E402

# Boites Vision de temp/56XX/5662/.../image-20260819-150108.png
TABLE = [
    {"text": "G", "x": 0.754, "y": 0.793, "width": 0.007, "height": 0.103, "confidence": 0.5},
    {"text": "D", "x": 0.567, "y": 0.776, "width": 0.007, "height": 0.121, "confidence": 0.5},
    {"text": "E", "x": 0.635, "y": 0.776, "width": 0.007, "height": 0.121, "confidence": 0.3},
    {"text": "F", "x": 0.690, "y": 0.776, "width": 0.007, "height": 0.121, "confidence": 0.5},
    {"text": "H", "x": 0.853, "y": 0.776, "width": 0.009, "height": 0.121, "confidence": 0.5},
    {"text": "A", "x": 0.083, "y": 0.759, "width": 0.010, "height": 0.138, "confidence": 0.5},
    {"text": "Agence", "x": 0.010, "y": 0.603, "width": 0.035, "height": 0.103, "confidence": 1.0},
    {"text": "- Client", "x": 0.156, "y": 0.603, "width": 0.042, "height": 0.121, "confidence": 1.0},
    {"text": "• Chantier", "x": 0.423, "y": 0.585, "width": 0.054, "height": 0.158, "confidence": 1.0},
    {"text": "- Animateur salaire", "x": 0.523, "y": 0.586, "width": 0.116, "height": 0.155, "confidence": 0.5},
    {"text": "-|CP", "x": 0.658, "y": 0.586, "width": 0.028, "height": 0.155, "confidence": 0.3},
    {"text": "-Salaire HCP", "x": 0.705, "y": 0.584, "width": 0.065, "height": 0.159, "confidence": 0.5},
    {"text": "• Chiffre d'affaire", "x": 0.788, "y": 0.586, "width": 0.086, "height": 0.155, "confidence": 1.0},
    {"text": "• HCP / CA", "x": 0.904, "y": 0.584, "width": 0.054, "height": 0.159, "confidence": 0.5},
    {"text": "NIMES - ABER propreté Azur", "x": 0.004, "y": 0.414, "width": 0.132, "height": 0.138, "confidence": 0.5},
    {"text": "COMMUNAUTE DE COMMUNE DU PAYS D'UZES - LOT 2", "x": 0.169, "y": 0.414, "width": 0.238, "height": 0.138, "confidence": 1.0},
    {"text": "OMBRIERE", "x": 0.435, "y": 0.414, "width": 0.051, "height": 0.172, "confidence": 0.5},
    {"text": "0,00 €", "x": 0.798, "y": 0.397, "width": 0.032, "height": 0.155, "confidence": 1.0},
    {"text": ", NIMES - ABER propreté Azur", "x": 0.000, "y": 0.241, "width": 0.137, "height": 0.155, "confidence": 0.5},
    {"text": "CHANTIER NÎMES *", "x": 0.435, "y": 0.241, "width": 0.087, "height": 0.155, "confidence": 1.0},
    {"text": "1451,62 €", "x": 0.605, "y": 0.241, "width": 0.049, "height": 0.172, "confidence": 1.0},
    {"text": "37,38 €", "x": 0.668, "y": 0.237, "width": 0.038, "height": 0.164, "confidence": 1.0},
    {"text": "1414,24 €", "x": 0.715, "y": 0.235, "width": 0.051, "height": 0.185, "confidence": 1.0},
    {"text": "0,00 €", "x": 0.798, "y": 0.224, "width": 0.033, "height": 0.207, "confidence": 1.0},
    {"text": "• Total", "x": 0.000, "y": 0.103, "width": 0.036, "height": 0.121, "confidence": 0.5},
    {"text": "1414,24 €", "x": 0.717, "y": 0.069, "width": 0.049, "height": 0.172, "confidence": 1.0},
    {"text": "0,00 €", "x": 0.798, "y": 0.069, "width": 0.032, "height": 0.155, "confidence": 1.0},
    {"text": "#DIV/O!", "x": 0.916, "y": 0.069, "width": 0.038, "height": 0.155, "confidence": 0.5},
]

FORM = [
    {"text": "Clients", "x": 0.053, "y": 0.586, "width": 0.053, "height": 0.08, "confidence": 1.0},
    {"text": "Chantiers", "x": 0.053, "y": 0.392, "width": 0.072, "height": 0.08, "confidence": 1.0},
    {"text": "OMBRIERE", "x": 0.058, "y": 0.323, "width": 0.20, "height": 0.08, "confidence": 1.0},
    {"text": "Export", "x": 0.934, "y": 0.855, "width": 0.064, "height": 0.08, "confidence": 1.0},
    {"text": "Sélection", "x": 0.936, "y": 0.258, "width": 0.061, "height": 0.08, "confidence": 1.0},
]


def check(name: str, condition: bool) -> None:
    status = "OK" if condition else "KO"
    print(f"  {status}  {name}")
    if not condition:
        raise SystemExit(1)


def main() -> None:
    check("corrige #DIV/O!", clean_cell("#DIV/O!") == "#DIV/0!")
    check("nettoie puces d'en-tete", clean_cell("• Chantier") == "Chantier")
    check("nettoie virgule OCR de numero de ligne", clean_cell(", NIMES") == "NIMES")

    table = build_grid(TABLE)
    check("reconstruit une table", table is not None)
    assert table is not None
    check("4 lignes (lettres Excel ecartees)", len(table) == 4)
    check("au moins 8 colonnes", len(table[0]) >= 8)
    header = " ".join(table[0])
    check("en-tetes Agence Client Chantier", "Agence" in header and "Client" in header and "Chantier" in header)
    bodies = " ".join(" ".join(row) for row in table[1:])
    check("OMBRIERE dans la grille", "OMBRIERE" in bodies)
    check("CHANTIER NÎMES * dans la grille", "CHANTIER NÎMES *" in bodies)
    check("#DIV/0! dans la grille", "#DIV/0!" in bodies)
    check("Salaire et CP ne sont pas colles", "1451,62 €" in bodies and "37,38 €" in bodies)
    check("1451 n'est pas dans la meme cellule que 37,38", not any("1451,62 €" in cell and "37,38" in cell for row in table for cell in row))
    check("CP et Salaire HCP separes", "Salaire HCP" in header and "CP" in header)
    markdown = render_markdown_table(table)
    check("rendu markdown", markdown.startswith("| ") and "---" in markdown)

    check("un formulaire 2 colonnes n'est pas une table", build_grid(FORM) is None)
    print("tous les controles de grille passent")
    print(markdown)


if __name__ == "__main__":
    main()
