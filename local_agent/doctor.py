"""Diagnostic local-agent : MCP, serveur LLM, outils systeme, store. Aucune ecriture destructive."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from . import ocr, shell, store
from .config import Config
from .mlx import MlxClient, MlxError


def check(config: Config, client: MlxClient | None = None) -> dict:
    items: list[dict] = []

    def add(name: str, ok: bool, detail: str) -> None:
        items.append({"name": name, "ok": ok, "detail": detail})

    add("python", True, sys.version.split()[0])
    add("rg", bool(shutil.which("rg")), shutil.which("rg") or "ripgrep missing")
    add("git", bool(shutil.which("git")), shutil.which("git") or "git missing")
    mcp = Path(__file__).resolve().parent.parent / "bin" / "local-agent-mcp"
    add("mcp", mcp.is_file(), str(mcp))
    add("repo_root", config.repo_root.is_dir(), str(config.repo_root))
    add("session", True, store.current_session())
    db_path = store.DB_PATH
    writable = os.access(db_path.parent, os.W_OK)
    try:
        db = store.Store(db_path)
        db.close()
        add("database", writable, str(db_path))
    except OSError as error:
        add("database", False, str(error))
    try:
        names = sorted(shell.load_checks(config))
        add("project_checks", True, ", ".join(names) or "none declared")
    except Exception as error:
        add("project_checks", False, str(error))
    status = ocr.backend_status()
    add("ocr", bool(status.get("swiftc") or status.get("tesseract")), str(status))
    mlx = client or MlxClient(config)
    try:
        import urllib.request

        url = config.base_url.replace("/v1", "") + "/health"
        with urllib.request.urlopen(url, timeout=5) as response:
            add("mlx_health", response.status == 200, url)
    except Exception as error:
        add("mlx_health", False, str(error))
    try:
        caps = mlx.capabilities()
        add("model", bool(caps.get("loaded") or caps.get("id")), str(caps.get("id") or "none"))
        add("tool_calling", bool(caps.get("tool_use")), str(caps.get("capabilities")))
        add("vision", bool(caps.get("vision")), str(caps.get("input_modalities")))
        add("json_schema", bool(caps.get("json_schema")), "json_schema" if caps.get("json_schema") else "absent")
        add("context_length", int(caps.get("context_length") or 0) > 0, str(caps.get("context_length")))
    except MlxError as error:
        add("model", False, str(error))
    from .providers import atlassian

    jira = atlassian.status(config.repo_root)
    add("jira", bool(jira["configured"]), jira["base"] if jira["configured"] else "not configured")
    add(
        "confluence",
        bool(jira["configured"]),
        "same Atlassian token as Jira" if jira["configured"] else "not configured",
    )
    ok = all(item["ok"] for item in items if item["name"] in {"rg", "git", "database"})
    return {"ok": ok, "checks": items, "config": config.as_summary()}


def render(payload: dict) -> str:
    lines = ["LOCAL-AGENT DOCTOR", ""]
    for item in payload.get("checks") or []:
        mark = "ok" if item.get("ok") else "FAIL"
        lines.append(f"[{mark}] {item.get('name')}: {item.get('detail')}")
    lines.append("")
    lines.append("ok" if payload.get("ok") else "doctor found blocking issues")
    return "\n".join(lines)
