"""SQLite evidence store, file-hash cache and persistent metrics.

JSON packets in evidence.py stay the source of truth for OCR regions (local_image_crop).
This module adds stable session ids (E1, IMG-E3, TEST-E4) and SHA256 reuse.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import evidence as image_evidence
from .files import GuardrailError

DB_PATH = Path.home() / ".local-agent" / "context.db"
SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created TEXT NOT NULL,
    task TEXT NOT NULL,
    autonomy TEXT,
    status TEXT,
    confidence REAL,
    risk TEXT,
    model TEXT,
    payload TEXT
);
CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY,
    task_id INTEGER,
    type TEXT NOT NULL,
    source TEXT,
    summary TEXT,
    sha256 TEXT,
    path TEXT,
    lines TEXT,
    confidence REAL,
    payload TEXT,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS file_cache (
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    summary TEXT,
    evidence_id TEXT,
    updated TEXT NOT NULL,
    PRIMARY KEY (path, sha256)
);
CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    tool TEXT,
    source_type TEXT,
    raw_tokens INTEGER,
    visible_tokens INTEGER,
    avoided_tokens INTEGER,
    local_llm_in INTEGER,
    local_llm_out INTEGER,
    tool_calls INTEGER,
    latency_s REAL,
    escalated INTEGER,
    model TEXT,
    status TEXT
);
"""

_lock = threading.Lock()
_PREFIX = {
    "code": "CODE-E",
    "image": "IMG-E",
    "log": "LOG-E",
    "test": "TEST-E",
    "diff": "DIFF-E",
    "jira": "JIRA-E",
    "doc": "DOC-E",
    "rule": "RULE-E",
    "data": "DATA-E",
}
SESSION_FILE = Path.home() / ".local-agent" / "session.id"
_CODE_PLAIN = re.compile(r"^E(\d+)$", re.IGNORECASE)
_CODE_FULL = re.compile(r"^CODE-E(\d+)$", re.IGNORECASE)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def current_session() -> str:
    override = (os.environ.get("LOCAL_AGENT_SESSION") or "").strip()
    if override:
        return override
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    if SESSION_FILE.is_file():
        value = SESSION_FILE.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = datetime.now(timezone.utc).strftime("%Y%m%d") + "-" + uuid.uuid4().hex[:8]
    SESSION_FILE.write_text(value + "\n", encoding="utf-8")
    return value


def new_session() -> str:
    value = datetime.now(timezone.utc).strftime("%Y%m%d") + "-" + uuid.uuid4().hex[:8]
    os.environ["LOCAL_AGENT_SESSION"] = value
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(value + "\n", encoding="utf-8")
    return value


def evidence_aliases(identifier: str) -> list[str]:
    text = str(identifier or "").strip()
    names = [text]
    plain = _CODE_PLAIN.fullmatch(text)
    if plain:
        names.append(f"CODE-E{plain.group(1)}")
    full = _CODE_FULL.fullmatch(text)
    if full:
        names.append(f"E{full.group(1)}")
    return names


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Store:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with _lock:
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(metrics)")}
        if "session_id" not in columns:
            self._conn.execute("ALTER TABLE metrics ADD COLUMN session_id TEXT")
        if "cache_hit" not in columns:
            self._conn.execute("ALTER TABLE metrics ADD COLUMN cache_hit INTEGER DEFAULT 0")

    def close(self) -> None:
        self._conn.close()

    def create_task(self, task: str, autonomy: str, model: str = "") -> int:
        with _lock:
            cursor = self._conn.execute(
                "INSERT INTO tasks(created, task, autonomy, status, model) VALUES (?,?,?,?,?)",
                (_now(), task, autonomy, "running", model),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def finish_task(
        self,
        task_id: int,
        *,
        status: str,
        confidence: float | None,
        risk: str,
        payload: dict | None = None,
    ) -> None:
        with _lock:
            self._conn.execute(
                "UPDATE tasks SET status=?, confidence=?, risk=?, payload=? WHERE id=?",
                (status, confidence, risk, json.dumps(payload or {}, ensure_ascii=False), task_id),
            )
            self._conn.commit()

    def _next_id(self, kind: str) -> str:
        prefix = _PREFIX.get(kind, "E")
        row = self._conn.execute("SELECT COUNT(*) AS n FROM evidence WHERE id LIKE ?", (prefix + "%",)).fetchone()
        return f"{prefix}{int(row['n']) + 1}"

    def put(
        self,
        kind: str,
        *,
        source: str = "",
        summary: str = "",
        sha256: str = "",
        path: str = "",
        lines: str = "",
        confidence: float | None = None,
        payload: dict | None = None,
        task_id: int | None = None,
    ) -> str:
        with _lock:
            identifier = self._next_id(kind)
            self._conn.execute(
                "INSERT INTO evidence(id, task_id, type, source, summary, sha256, path, lines, confidence, payload, created) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    task_id,
                    kind,
                    source,
                    summary[:800],
                    sha256,
                    path,
                    lines,
                    confidence,
                    json.dumps(payload or {}, ensure_ascii=False),
                    _now(),
                ),
            )
            self._conn.commit()
            return identifier

    def get(self, identifier: str) -> dict:
        row = None
        for name in evidence_aliases(identifier):
            row = self._conn.execute("SELECT * FROM evidence WHERE id=?", (name,)).fetchone()
            if row is not None:
                break
        if row is None:
            raise GuardrailError(f"unknown evidence id {identifier}")
        payload = json.loads(row["payload"] or "{}")
        return {
            "id": row["id"],
            "type": row["type"],
            "source": row["source"],
            "summary": row["summary"],
            "sha256": row["sha256"],
            "path": row["path"],
            "lines": row["lines"],
            "confidence": row["confidence"],
            "created": row["created"],
            "payload": payload,
        }

    def cached_summary(self, path: str, digest: str) -> dict | None:
        row = self._conn.execute(
            "SELECT summary, evidence_id FROM file_cache WHERE path=? AND sha256=?",
            (path, digest),
        ).fetchone()
        if row is None:
            return None
        return {"summary": row["summary"], "evidence_id": row["evidence_id"]}

    def remember_file(self, path: str, digest: str, summary: str, evidence_id: str) -> None:
        with _lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO file_cache(path, sha256, summary, evidence_id, updated) VALUES (?,?,?,?,?)",
                (path, digest, summary[:800], evidence_id, _now()),
            )
            self._conn.commit()

    def record_metric(self, **fields: object) -> None:
        keys = (
            "tool", "source_type", "raw_tokens", "visible_tokens", "avoided_tokens",
            "local_llm_in", "local_llm_out", "tool_calls", "latency_s", "escalated", "model", "status",
        )
        values = [_now()] + [fields.get(key) for key in keys]
        session = str(fields.get("session_id") or current_session())
        cache_hit = int(fields.get("cache_hit") or 0)
        with _lock:
            self._conn.execute(
                "INSERT INTO metrics(ts, tool, source_type, raw_tokens, visible_tokens, avoided_tokens, "
                "local_llm_in, local_llm_out, tool_calls, latency_s, escalated, model, status, session_id, cache_hit) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                values + [session, cache_hit],
            )
            self._conn.commit()

    def session_stats(self) -> dict:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n, "
            "COALESCE(SUM(raw_tokens),0) AS raw, "
            "COALESCE(SUM(visible_tokens),0) AS visible, "
            "COALESCE(SUM(avoided_tokens),0) AS avoided, "
            "COALESCE(SUM(tool_calls),0) AS tools, "
            "COALESCE(SUM(escalated),0) AS escalated, "
            "COALESCE(AVG(latency_s),0) AS latency "
            "FROM metrics"
        ).fetchone()
        by_source = {
            item["source_type"] or "unknown": item["n"]
            for item in self._conn.execute(
                "SELECT source_type, COUNT(*) AS n FROM metrics GROUP BY source_type"
            )
        }
        tasks = self._conn.execute(
            "SELECT COUNT(*) AS n, "
            "SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS ok, "
            "SUM(CASE WHEN status='needs_claude' THEN 1 ELSE 0 END) AS escalated "
            "FROM tasks"
        ).fetchone()
        llm = self._conn.execute(
            "SELECT COALESCE(SUM(local_llm_in),0) AS inn, COALESCE(SUM(local_llm_out),0) AS out, "
            "COALESCE(SUM(cache_hit),0) AS hits FROM metrics"
        ).fetchone()
        n_metrics = int(row["n"] or 0)
        visible = int(row["visible"] or 0)
        return {
            "session_id": current_session(),
            "metrics_rows": n_metrics,
            "raw_tokens": int(row["raw"] or 0),
            "visible_tokens": visible,
            "avoided_tokens": int(row["avoided"] or 0),
            "tool_calls": int(row["tools"] or 0),
            "local_llm_in": int(llm["inn"] or 0),
            "local_llm_out": int(llm["out"] or 0),
            "cache_hits": int(llm["hits"] or 0),
            "avg_packet_tokens": round(visible / n_metrics, 1) if n_metrics else 0,
            "avg_latency_s": round(float(row["latency"] or 0), 2),
            "by_source": by_source,
            "local_tasks": int(tasks["n"] or 0),
            "completed_without_claude": int(tasks["ok"] or 0),
            "escalated": int(tasks["escalated"] or 0),
        }


def expand(identifier: str, store: Store | None = None) -> dict:
    """Resolve E14 / IMG-E2 / a832-R1. OCR regions stay on the JSON packets."""
    text = str(identifier or "").strip()
    if not text:
        raise GuardrailError("evidence id is required")
    if "-R" in text.upper() or text.startswith("image://"):
        image_id, region = image_evidence.parse_region_id(text)
        packet = image_evidence.load(image_id)
        match = None
        wanted = f"{image_id}-{region}"
        for item in packet.get("regions") or []:
            if str(item.get("id")) in {wanted, region, f"{image_id}-{region}"}:
                match = item
                break
        if match is None:
            raise GuardrailError(f"unknown region {text}")
        return {"id": text, "type": "image_region", "source": packet.get("path"), "payload": match}
    db = store or Store()
    try:
        return db.get(text)
    except GuardrailError:
        if store is None:
            db.close()
        raise
