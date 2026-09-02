"""Resolve Atlassian credentials. Secrets stay in the repo's local env, never in git."""

from __future__ import annotations

import base64
import os
from pathlib import Path
import urllib.request

# Lysi skills use JIRA_URL / JIRA_API_TOKEN / JIRA_USERNAME in .claude/.env.local.
# local-agent also accepts JIRA_BASE_URL / JIRA_TOKEN / JIRA_EMAIL.
_FILES = (".claude/.env.local", ".env.local")
_KEYS = {
    "JIRA_BASE_URL",
    "JIRA_URL",
    "JIRA_TOKEN",
    "JIRA_API_TOKEN",
    "JIRA_EMAIL",
    "JIRA_USERNAME",
    "ATLASSIAN_BASE_URL",
    "ATLASSIAN_API_TOKEN",
    "ATLASSIAN_EMAIL",
    "CONFLUENCE_BASE_URL",
    "CONFLUENCE_TOKEN",
}


def _parse_env_file(path: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    if not path.is_file():
        return found
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in _KEYS:
            continue
        found[key] = value.strip().strip("'\"")
    return found


def _file_values(repo_root: Path | None) -> dict[str, str]:
    if repo_root is None:
        return {}
    root = Path(repo_root)
    merged: dict[str, str] = {}
    for relative in _FILES:
        merged.update(_parse_env_file(root / relative))
    return merged


def _pick(values: dict[str, str], *names: str) -> str:
    for name in names:
        raw = (os.environ.get(name) or values.get(name) or "").strip()
        if raw:
            return raw
    return ""


def credentials(repo_root: Path | None = None) -> dict[str, str]:
    files = _file_values(repo_root)
    base = _pick(files, "JIRA_BASE_URL", "JIRA_URL", "ATLASSIAN_BASE_URL", "CONFLUENCE_BASE_URL").rstrip("/")
    token = _pick(files, "JIRA_TOKEN", "JIRA_API_TOKEN", "ATLASSIAN_API_TOKEN", "CONFLUENCE_TOKEN")
    email = _pick(files, "JIRA_EMAIL", "JIRA_USERNAME", "ATLASSIAN_EMAIL")
    return {"base": base, "token": token, "email": email}


def authorize(request: urllib.request.Request, creds: dict[str, str]) -> None:
    if creds.get("email"):
        raw = f"{creds['email']}:{creds['token']}".encode()
        request.add_header("Authorization", "Basic " + base64.b64encode(raw).decode("ascii"))
    else:
        request.add_header("Authorization", f"Bearer {creds['token']}")


def status(repo_root: Path | None = None) -> dict:
    creds = credentials(repo_root)
    return {
        "configured": bool(creds["base"] and creds["token"]),
        "base": creds["base"],
        "email_set": bool(creds["email"]),
        "source": ".claude/.env.local" if _file_values(repo_root) else "environment",
    }
