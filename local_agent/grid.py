"""Reconstruction d'une grille (lignes x colonnes) a partir des boites OCR, sans modele."""

from __future__ import annotations

import re
import statistics

MIN_TABLE_ROWS = 3
MIN_TABLE_COLS = 3
COLUMN_GAP = 0.04
EXCEL_ERROR = re.compile(r"#DIV/O!", re.IGNORECASE)
LEADING_BULLET = re.compile(r"^[\s•\-|,]+")


def _num(item: dict, key: str, default: float = 0.0) -> float:
    try:
        return float(item.get(key) or default)
    except (TypeError, ValueError):
        return default


def _left(item: dict) -> float:
    return _num(item, "x")


def _center_y(item: dict) -> float:
    return _num(item, "y") + _num(item, "height") / 2


def _height(item: dict) -> float:
    return max(0.01, _num(item, "height", 0.05))


def clean_cell(text: str) -> str:
    cleaned = EXCEL_ERROR.sub("#DIV/0!", str(text or "").strip())
    cleaned = LEADING_BULLET.sub("", cleaned).strip()
    return cleaned


def is_column_letter(item: dict) -> bool:
    """Lettres A-Z au-dessus d'une feuille Excel, trop etroites pour etre une cellule."""
    text = str(item.get("text") or "").strip()
    return len(text) == 1 and text.isalpha() and _num(item, "width") < 0.025


def cluster_1d(values: list[float], gap: float) -> list[list[int]]:
    """Regroupe des positions triees. Rend des listes d'indices dans l'ordre d'origine."""
    if not values:
        return []
    indexed = sorted(range(len(values)), key=lambda i: values[i])
    groups = [[indexed[0]]]
    for index in indexed[1:]:
        members = groups[-1]
        center = sum(values[i] for i in members) / len(members)
        if values[index] - center > gap:
            groups.append([index])
        else:
            groups[-1].append(index)
    return groups


def _row_gap(items: list[dict]) -> float:
    heights = [_height(item) for item in items]
    median = statistics.median(heights) if heights else 0.05
    return max(0.025, median * 0.45)


def group_rows(items: list[dict]) -> list[list[dict]]:
    if not items:
        return []
    gap = _row_gap(items)
    groups = cluster_1d([_center_y(item) for item in items], gap)
    rows = [[items[index] for index in group] for group in groups]
    rows.sort(key=lambda row: -sum(_center_y(item) for item in row) / len(row))
    for row in rows:
        row.sort(key=_left)
    return rows


def _column_centers(items: list[dict]) -> list[float]:
    lefts = [_left(item) for item in items]
    groups = cluster_1d(lefts, COLUMN_GAP)
    return [sum(lefts[index] for index in group) / len(group) for group in groups]


def _assign_column(x: float, centers: list[float]) -> int:
    return min(range(len(centers)), key=lambda i: abs(x - centers[i]))


def build_grid(lines: list[dict]) -> list[list[str]] | None:
    items = [item for item in lines if clean_cell(str(item.get("text") or "")) and not is_column_letter(item)]
    if len(items) < MIN_TABLE_ROWS * 2:
        return None
    rows = [row for row in group_rows(items) if row]
    if len(rows) < MIN_TABLE_ROWS:
        return None
    centers = _column_centers(items)
    if len(centers) < MIN_TABLE_COLS:
        return None
    grid: list[list[str]] = []
    for row in rows:
        cells = [""] * len(centers)
        for item in row:
            column = _assign_column(_left(item), centers)
            piece = clean_cell(str(item.get("text") or ""))
            if not piece:
                continue
            cells[column] = f"{cells[column]} {piece}".strip() if cells[column] else piece
        grid.append(cells)
    filled = sum(1 for row in grid for cell in row if cell)
    if filled < len(grid):
        return None
    return grid


def render_markdown_table(grid: list[list[str]]) -> str:
    if not grid:
        return ""
    width = max(len(row) for row in grid)
    rows = [row + [""] * (width - len(row)) for row in grid]

    def cell(value: str) -> str:
        return value.replace("|", "\\|")

    header = rows[0]
    lines = [
        "| " + " | ".join(cell(item) for item in header) + " |",
        "| " + " | ".join("---" for _ in header) + " |"
    ]
    for row in rows[1:]:
        lines.append("| " + " | ".join(cell(item) for item in row) + " |")
    return "\n".join(lines)


def render_tsv(grid: list[list[str]]) -> str:
    return "\n".join("\t".join(row) for row in grid)
