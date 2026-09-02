"""Analyse deterministe de csv/json/sqlite. Pas de DuckDB : stdlib uniquement."""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

from ..config import Config
from ..files import GuardrailError, resolve_path


def analyze(config: Config, raw: str, query: str | None = None) -> dict:
    path = resolve_path(config, raw)
    if not path.is_file():
        raise GuardrailError(f"data file missing: {path}")
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return _csv(path)
    if suffix in {".json", ".jsonl"}:
        return _json(path)
    if suffix in {".db", ".sqlite", ".sqlite3"}:
        return _sqlite(path, query)
    raise GuardrailError(f"unsupported data type {suffix}. csv, json, sqlite only")


def _csv(path: Path) -> dict:
    with path.open(encoding="utf-8", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = []
        for index, row in enumerate(reader):
            if index >= 5000:
                break
            rows.append(row)
        fieldnames = list(reader.fieldnames or [])
    numeric: dict[str, list[float]] = {name: [] for name in fieldnames}
    for row in rows:
        for name in fieldnames:
            try:
                numeric[name].append(float(str(row.get(name) or "").replace(",", ".").replace(" ", "")))
            except ValueError:
                pass
    stats = {}
    for name, values in numeric.items():
        if len(values) < 3:
            continue
        ordered = sorted(values)
        stats[name] = {
            "count": len(values),
            "min": ordered[0],
            "max": ordered[-1],
            "median": ordered[len(ordered) // 2],
        }
    return {
        "summary": f"{len(rows)} rows, {len(fieldnames)} columns",
        "columns": fieldnames[:40],
        "stats": stats,
        "sample": rows[:5],
    }


def _json(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")[:2_000_000]
    if path.suffix.lower() == ".jsonl":
        rows = []
        for line in text.splitlines()[:5000]:
            if line.strip():
                rows.append(json.loads(line))
        return {"summary": f"{len(rows)} jsonl records", "sample": rows[:3]}
    payload = json.loads(text)
    if isinstance(payload, list):
        return {"summary": f"{len(payload)} json records", "sample": payload[:3]}
    return {"summary": "json object", "keys": list(payload)[:40] if isinstance(payload, dict) else []}


def _sqlite(path: Path, query: str | None) -> dict:
    if query and any(word in query.lower() for word in ("drop", "delete", "update", "insert", "alter", "attach")):
        raise GuardrailError("only read-only SELECT queries are allowed")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        payload: dict = {"summary": f"sqlite tables: {', '.join(tables[:12])}", "tables": tables}
        if query:
            rows = connection.execute(query).fetchmany(20)
            payload["query_sample"] = rows
        elif tables:
            counts = {}
            for name in tables[:8]:
                counts[name] = connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            payload["counts"] = counts
        return payload
    finally:
        connection.close()
