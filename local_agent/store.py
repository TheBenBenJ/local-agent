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

IMAGE_PACKET_ID = re.compile(r"^[0-9a-f]{8}$")

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
    payload TEXT,
    session_id TEXT
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
    created TEXT NOT NULL,
    session_id TEXT
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
    status TEXT,
    session_id TEXT,
    cache_hit INTEGER DEFAULT 0
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
_STORE_ID = re.compile(
    r"^(CODE-E|IMG-E|LOG-E|TEST-E|DIFF-E|JIRA-E|DOC-E|RULE-E|DATA-E|E)\d+$",
    re.IGNORECASE,
)
TRACE_DIR = Path.home() / ".local-agent" / "traces"


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


def _add_column(conn: sqlite3.Connection, table: str, column: str, spec: str) -> None:
    names = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in names:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {spec}")


class Store:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = self._connect()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        except sqlite3.DatabaseError:
            conn.close()
            if self.path.is_file():
                self.path.replace(self.path.with_name(self.path.name + ".corrupt"))
            conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
            conn.row_factory = sqlite3.Row
        with _lock:
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                pass
            conn.executescript(SCHEMA)
            self._migrate_conn(conn)
            conn.commit()
        return conn

    @staticmethod
    def _migrate_conn(conn: sqlite3.Connection) -> None:
        _add_column(conn, "metrics", "session_id", "TEXT")
        _add_column(conn, "metrics", "cache_hit", "INTEGER DEFAULT 0")
        _add_column(conn, "metrics", "tier", "TEXT")
        _add_column(conn, "metrics", "routing_reason", "TEXT")
        _add_column(conn, "metrics", "local_llm_calls", "INTEGER DEFAULT 0")
        _add_column(conn, "metrics", "avoidable_llm", "INTEGER DEFAULT 0")
        _add_column(conn, "tasks", "session_id", "TEXT")
        _add_column(conn, "evidence", "session_id", "TEXT")

    def _migrate(self) -> None:
        self._migrate_conn(self._conn)

    def close(self) -> None:
        self._conn.close()

    def create_task(self, task: str, autonomy: str, model: str = "") -> int:
        with _lock:
            cursor = self._conn.execute(
                "INSERT INTO tasks(created, task, autonomy, status, model, session_id) VALUES (?,?,?,?,?,?)",
                (_now(), task, autonomy, "running", model, current_session()),
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
                "INSERT INTO evidence(id, task_id, type, source, summary, sha256, path, lines, confidence, payload, created, session_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
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
                    current_session(),
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

    def exists(self, identifier: str) -> bool:
        try:
            self.get(identifier)
            return True
        except GuardrailError:
            return False

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
        tier = str(fields.get("tier") or "")
        routing_reason = str(fields.get("routing_reason") or "")[:240]
        local_llm_calls = int(fields.get("local_llm_calls") or 0)
        avoidable_llm = int(fields.get("avoidable_llm") or 0)
        with _lock:
            self._conn.execute(
                "INSERT INTO metrics(ts, tool, source_type, raw_tokens, visible_tokens, avoided_tokens, "
                "local_llm_in, local_llm_out, tool_calls, latency_s, escalated, model, status, session_id, cache_hit, "
                "tier, routing_reason, local_llm_calls, avoidable_llm) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                values + [session, cache_hit, tier, routing_reason, local_llm_calls, avoidable_llm],
            )
            self._conn.commit()

    def _metric_bundle(self, session_id: str | None) -> dict:
        where = "WHERE session_id=?" if session_id else ""
        params: tuple = (session_id,) if session_id else ()
        row = self._conn.execute(
            "SELECT COUNT(*) AS n, "
            "COALESCE(SUM(raw_tokens),0) AS raw, "
            "COALESCE(SUM(visible_tokens),0) AS visible, "
            "COALESCE(SUM(avoided_tokens),0) AS avoided, "
            "COALESCE(SUM(tool_calls),0) AS tools, "
            "COALESCE(SUM(escalated),0) AS escalated, "
            "COALESCE(AVG(latency_s),0) AS latency, "
            "COALESCE(SUM(local_llm_in),0) AS inn, "
            "COALESCE(SUM(local_llm_out),0) AS out, "
            "COALESCE(SUM(cache_hit),0) AS hits, "
            "COALESCE(SUM(local_llm_calls),0) AS llm_calls, "
            "COALESCE(SUM(avoidable_llm),0) AS avoidable "
            f"FROM metrics {where}",
            params,
        ).fetchone()
        by_source = {
            item["source_type"] or "unknown": item["n"]
            for item in self._conn.execute(
                f"SELECT source_type, COUNT(*) AS n FROM metrics {where} GROUP BY source_type",
                params,
            )
        }
        by_tier = {
            item["tier"] or "unset": item["n"]
            for item in self._conn.execute(
                f"SELECT COALESCE(tier,'') AS tier, COUNT(*) AS n FROM metrics {where} GROUP BY tier",
                params,
            )
        }
        task_where = "WHERE session_id=?" if session_id else ""
        tasks = self._conn.execute(
            "SELECT COUNT(*) AS n, "
            "SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS ok, "
            "SUM(CASE WHEN status='needs_claude' THEN 1 ELSE 0 END) AS escalated "
            f"FROM tasks {task_where}",
            params,
        ).fetchone()
        n_metrics = int(row["n"] or 0)
        visible = int(row["visible"] or 0)
        local_tasks = int(tasks["n"] or 0)
        completed = int(tasks["ok"] or 0)
        escalated = int(tasks["escalated"] or 0)
        return {
            "metrics_rows": n_metrics,
            "raw_tokens": int(row["raw"] or 0),
            "visible_tokens": visible,
            "avoided_tokens": int(row["avoided"] or 0),
            "tool_calls": int(row["tools"] or 0),
            "local_llm_in": int(row["inn"] or 0),
            "local_llm_out": int(row["out"] or 0),
            "cache_hits": int(row["hits"] or 0),
            "local_llm_calls": int(row["llm_calls"] or 0),
            "avoidable_local_llm_calls": int(row["avoidable"] or 0),
            "by_tier": by_tier,
            "avg_packet_tokens": round(visible / n_metrics, 1) if n_metrics else 0,
            "avg_latency_s": round(float(row["latency"] or 0), 2),
            "by_source": by_source,
            "local_tasks": local_tasks,
            "completed_without_claude": completed,
            "escalated": escalated,
            "offload_rate": round(completed / local_tasks, 3) if local_tasks else 0,
        }

    def stats(self) -> dict:
        sid = current_session()
        return {"session_id": sid, "current": self._metric_bundle(sid), "lifetime": self._metric_bundle(None)}

    def session_stats(self) -> dict:
        payload = self.stats()
        current = dict(payload["current"])
        current["session_id"] = payload["session_id"]
        current["lifetime"] = payload["lifetime"]
        return current


def _parse_span(text: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", str(text or "").strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _open_source(path: str, config) -> Path | None:
    if not path:
        return None
    candidate = Path(path).expanduser()
    if candidate.is_absolute() and candidate.is_file():
        return candidate
    if config is None:
        return candidate if candidate.is_file() else None
    try:
        from .files import resolve_path

        return resolve_path(config, path)
    except Exception:
        return None


def hydrate(row: dict, config=None) -> dict:
    """Re-read the source when possible. Never silently return a stale excerpt."""
    payload = dict(row.get("payload") or {})
    recorded = str(row.get("sha256") or payload.get("sha256") or "")
    path = str(row.get("path") or payload.get("path") or "")
    region = str(payload.get("region") or "")
    target = _open_source(path, config)
    result = dict(row)
    result["status"] = "stored"

    if target is not None and target.is_file() and recorded:
        current = sha256_file(target)
        result["current_sha256"] = current
        if current != recorded:
            result["status"] = "stale_evidence"
            result["reason"] = "source changed since evidence was recorded."
            result["excerpt"] = ""
            return result

    if region and "-R" in region.upper():
        result["status"] = "current" if target else "stored"
        result["region"] = region
        return result

    span = _parse_span(str(row.get("lines") or payload.get("lines") or ""))
    if target is not None and target.is_file() and span:
        from .files import read_text

        text, _truncated = read_text(target, 400_000)
        lines = text.splitlines()
        start, end = span
        start = max(1, start)
        end = min(len(lines), end)
        excerpt = "\n".join(f"{index}| {lines[index - 1]}" for index in range(start, end + 1))
        result["status"] = "current"
        result["excerpt"] = excerpt
        result["lines"] = f"{start}-{end}"
        return result

    if target is not None and target.is_file() and recorded:
        result["status"] = "current"
    return result


def attach_report_evidence(db: Store, report, kind: str | None = None) -> list[str]:
    """Give every report a Store id so local_expand can resolve what Claude sees."""
    title = str(getattr(report, "title", "") or "").lower()
    if kind is None:
        if "image" in title or "ocr" in title or "compare" in title:
            kind = "image"
        elif "log" in title:
            kind = "log"
        elif "diff" in title:
            kind = "diff"
        elif "check" in title or "test" in title:
            kind = "test"
        else:
            kind = "code"
    ids: list[str] = []
    items = list(getattr(report, "evidence", None) or [])
    files = list(getattr(report, "files", None) or [])
    if not items:
        identifier = db.put(
            kind,
            source=str(files[0] if files else title),
            summary=str(getattr(report, "summary", "") or "")[:300],
            path=str(files[0]) if files else "",
            payload={
                "locations": list(getattr(report, "locations", None) or [])[:20],
                "files": files[:20],
                "findings": list(getattr(report, "findings", None) or [])[:12],
            },
        )
        report.evidence = [{"id": identifier, "type": kind, "content": str(getattr(report, "summary", "") or "")[:220]}]
        return [identifier]
    rewritten = []
    for item in items:
        current = str(item.get("id") or "")
        if _STORE_ID.match(current) and db.exists(current):
            ids.append(current)
            rewritten.append(item)
            continue
        region = ""
        if "-R" in current.upper() or current.startswith("image://"):
            region = current
        region = str(item.get("region") or region)
        identifier = db.put(
            kind,
            source=region or current or str(item.get("source") or ""),
            summary=str(item.get("content") or item.get("summary") or "")[:300],
            path=str(item.get("path") or (files[0] if files else "")),
            sha256=str(item.get("sha256") or ""),
            payload={**item, "region": region or None},
            confidence=item.get("confidence") if isinstance(item.get("confidence"), (int, float)) else None,
        )
        ids.append(identifier)
        rewritten.append({**item, "id": identifier, "region": region or item.get("region")})
    report.evidence = rewritten
    return ids


def write_trace(task_id: int, payload: dict) -> Path:
    TRACE_DIR.mkdir(parents=True, exist_ok=True)
    path = TRACE_DIR / f"{task_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _expand_image_packet(image_id: str) -> dict:
    """Full OCR transcript stored by local_image. Packet itself is only an inventory."""
    packet = image_evidence.load(image_id)
    path = str(packet.get("path") or "")
    status = "current"
    reason = ""
    if path and Path(path).is_file() and packet.get("sha256"):
        if sha256_file(Path(path)) != packet.get("sha256"):
            status = "stale_evidence"
            reason = "source changed since evidence was recorded."
    transcript = str(packet.get("transcript") or "")
    if not transcript:
        lines = packet.get("lines") or []
        transcript = "\n".join(
            str(item.get("text") or "").strip()
            for item in lines
            if isinstance(item, dict) and str(item.get("text") or "").strip()
        )
    regions = packet.get("regions") or []
    return {
        "id": image_id,
        "type": "image",
        "status": status,
        "reason": reason,
        "source": path,
        "payload": {
            "transcript": transcript,
            "regions": [str(item.get("id")) for item in regions if item.get("id")],
            "vision": packet.get("vision") or {},
        },
    }


def expand(identifier: str, store: Store | None = None, config=None) -> dict:
    """Resolve E14 / IMG-E2 / a832-R1 / 8-char image id. Re-read the source; never return a silent stale excerpt."""
    text = str(identifier or "").strip()
    if not text:
        raise GuardrailError("evidence id is required")
    if IMAGE_PACKET_ID.fullmatch(text):
        try:
            return _expand_image_packet(text)
        except GuardrailError:
            pass
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
        path = str(packet.get("path") or "")
        status = "current"
        reason = ""
        if path and Path(path).is_file() and packet.get("sha256"):
            if sha256_file(Path(path)) != packet.get("sha256"):
                status = "stale_evidence"
                reason = "source changed since evidence was recorded."
        return {
            "id": text,
            "type": "image_region",
            "status": status,
            "reason": reason,
            "source": path,
            "payload": match,
        }
    db = store or Store()
    try:
        row = db.get(text)
        return hydrate(row, config)
    except GuardrailError:
        if store is None:
            db.close()
        raise
