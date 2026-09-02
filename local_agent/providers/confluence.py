"""Confluence Cloud adapter. Same Atlassian credentials as Jira (.claude/.env.local)."""

from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import atlassian

_TAG = re.compile(r"<[^>]+>", re.DOTALL)
_MAX_BODY = 4000


def storage_text(raw: str) -> str:
    text = re.sub(r"<ac:structured-macro[^>]*ac:name=\"([^\"]+)\"", r" [\1] ", raw or "")
    text = _TAG.sub(" ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _request(creds: dict[str, str], path: str, params: dict | None = None) -> dict:
    query = f"?{urllib.parse.urlencode(params)}" if params else ""
    url = f"{creds['base']}{path}{query}"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    atlassian.authorize(request, creds)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def _error(creds: dict[str, str], page: str, exc: BaseException) -> dict:
    if isinstance(exc, urllib.error.HTTPError):
        return {
            "configured": True,
            "error": f"Confluence HTTP {exc.code} for {page}",
            "page": page,
            "base": creds["base"],
        }
    if isinstance(exc, urllib.error.URLError):
        return {
            "configured": True,
            "error": f"Confluence request failed: {exc.reason}",
            "page": page,
            "base": creds["base"],
        }
    return {"configured": True, "error": str(exc), "page": page, "base": creds["base"]}


def _pack(page: str, item: dict) -> dict:
    body = ((item.get("body") or {}).get("storage") or {}).get("value") or ""
    space = (item.get("space") or {}).get("key") or ""
    return {
        "configured": True,
        "page": page,
        "id": str(item.get("id") or ""),
        "title": item.get("title") or "",
        "space": space,
        "version": (item.get("version") or {}).get("number"),
        "body": storage_text(str(body))[:_MAX_BODY],
    }


def _by_id(creds: dict[str, str], page_id: str) -> dict:
    item = _request(
        creds,
        f"/wiki/rest/api/content/{urllib.parse.quote(page_id, safe='')}",
        {"expand": "body.storage,version,space"},
    )
    return _pack(page_id, item)


def _by_cql(creds: dict[str, str], page: str, space: str | None, title: str) -> dict:
    title = title.replace('"', " ").strip()
    clauses = [f'title="{title}"']
    if space:
        clauses.insert(0, f'space="{space}"')
    payload = _request(
        creds,
        "/wiki/rest/api/content/search",
        {"cql": " AND ".join(clauses), "limit": "5", "expand": "body.storage,version,space"},
    )
    results = payload.get("results") or []
    if not results:
        return {
            "configured": True,
            "error": f"Confluence page not found: {page}",
            "page": page,
            "base": creds["base"],
        }
    return _pack(page, results[0])


def ping(repo_root: Path | None = None) -> dict:
    creds = atlassian.credentials(repo_root)
    if not creds["base"] or not creds["token"]:
        return {"configured": False, "error": "Confluence is not configured"}
    try:
        payload = _request(creds, "/wiki/rest/api/space", {"limit": "1"})
    except (urllib.error.HTTPError, urllib.error.URLError) as error:
        return _error(creds, "space", error)
    results = payload.get("results") or []
    first = results[0] if results else {}
    return {
        "configured": True,
        "ok": True,
        "spaces": payload.get("size") or len(results),
        "sample": first.get("key") or "",
        "base": creds["base"],
    }


def fetch(page: str, repo_root: Path | None = None) -> dict:
    creds = atlassian.credentials(repo_root)
    if not creds["base"] or not creds["token"]:
        return {
            "configured": False,
            "error": (
                "Confluence is not configured. The lysi .claude/.env.local Jira token is reused "
                "when present. Until a page id is fetched, pass a local export as docs://."
            ),
            "page": page,
        }
    ident = (page or "").strip().strip("/")
    if not ident:
        return {
            "configured": True,
            "error": "Confluence page id or SPACE/Title is required",
            "page": page,
        }
    try:
        if ident.isdigit():
            return _by_id(creds, ident)
        if "/" in ident:
            space, _, title = ident.partition("/")
            return _by_cql(creds, ident, space.strip() or None, title.strip())
        return _by_cql(creds, ident, None, ident)
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError, TimeoutError) as error:
        return _error(creds, ident, error)
