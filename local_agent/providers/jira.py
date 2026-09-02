"""Jira adapter. Credentials come from the environment or the repo's .claude/.env.local."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

from . import atlassian


def _adf_text(node: object) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "\n".join(part for part in (_adf_text(item) for item in node) if part)
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return str(node.get("text") or "")
    chunks = [_adf_text(child) for child in (node.get("content") or [])]
    text = "\n".join(chunk for chunk in chunks if chunk)
    if node.get("type") in {"paragraph", "heading", "blockquote", "listItem"}:
        return text.strip() + "\n"
    return text


def fetch(key: str, repo_root: Path | None = None) -> dict:
    """Return an ISSUE CONTRACT. If Jira is not configured, explain how to add it."""
    creds = atlassian.credentials(repo_root)
    if not creds["base"] or not creds["token"]:
        return {
            "configured": False,
            "error": (
                "Jira is not configured. Put JIRA_URL, JIRA_USERNAME and JIRA_API_TOKEN in "
                "the target repo's .claude/.env.local (lysi skills already use these names), "
                "or JIRA_BASE_URL / JIRA_TOKEN / JIRA_EMAIL in the environment. "
                "No secret is stored in local-agent."
            ),
            "key": key,
        }
    url = f"{creds['base']}/rest/api/3/issue/{key}"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    atlassian.authorize(request, creds)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            issue = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as error:
        return {"configured": True, "error": f"Jira HTTP {error.code} for {key}", "key": key, "base": creds["base"]}
    except urllib.error.URLError as error:
        return {"configured": True, "error": f"Jira request failed: {error.reason}", "key": key, "base": creds["base"]}
    fields = issue.get("fields") or {}
    description = fields.get("description")
    if isinstance(description, dict):
        description = _adf_text(description)
    attachments = [
        str(item.get("filename") or "")
        for item in (fields.get("attachment") or [])
        if item.get("filename")
    ]
    return {
        "configured": True,
        "key": key,
        "goal": fields.get("summary") or "",
        "acceptance_criteria_verbatim": str(description or "").strip()[:4000],
        "status": (fields.get("status") or {}).get("name"),
        "issuetype": (fields.get("issuetype") or {}).get("name"),
        "components": [item.get("name") for item in (fields.get("components") or []) if item.get("name")],
        "attachments": attachments[:20],
        "open_questions": [],
    }
