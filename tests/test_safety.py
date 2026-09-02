#!/usr/bin/env python3
"""Garde-fous : confinement, secrets, pas de shell, cache, providers absents."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_agent.agent_tools import ToolContext, dispatch  # noqa: E402
from local_agent.config import Config  # noqa: E402
from local_agent.files import GuardrailError, resolve_path  # noqa: E402
from local_agent.gateway import parse_source  # noqa: E402
from local_agent.providers import confluence as confluence_provider  # noqa: E402
from local_agent.providers import jira as jira_provider  # noqa: E402
from local_agent.store import Store, sha256_file  # noqa: E402
from local_agent.vision import reason  # noqa: E402


def check(name: str, condition: bool) -> None:
    status = "OK" if condition else "KO"
    print(f"  {status}  {name}")
    if not condition:
        raise SystemExit(1)


def _raises(fn) -> bool:
    try:
        fn()
    except (GuardrailError, ValueError, OSError):
        return True
    return False


def _git_repo(folder: Path) -> Config:
    subprocess.run(["git", "init", "-q"], cwd=folder, check=True, capture_output=True)
    (folder / "ok.py").write_text("print(1)\n", encoding="utf-8")
    return Config(repo_root=folder.resolve())


def main() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        config = _git_repo(root)
        check("fichier du dépôt", resolve_path(config, "ok.py").name == "ok.py")
        check("traversal", _raises(lambda: resolve_path(config, "../../etc")))
        (root / ".env").write_text("SECRET=1\n")
        check(".env", _raises(lambda: resolve_path(config, ".env")))
        (root / "id_rsa").write_text("fake")
        check("cle privee", _raises(lambda: resolve_path(config, "id_rsa")))
        (root / "token.pem").write_text("fake")
        check("pem", _raises(lambda: resolve_path(config, "token.pem")))

        outside = Path(raw).parent / f"la-secret-{os.getpid()}"
        try:
            outside.write_text("nope\n")
            link = root / "escape.py"
            link.symlink_to(outside)
            check("symlink hors repo", _raises(lambda: resolve_path(config, "escape.py")))
        finally:
            if outside.exists():
                outside.unlink()

        db = Store(root / "context.db")
        ctx = ToolContext(config, None, db, "read_only", db.create_task("t", "read_only"))
        unknown = dispatch(ctx, "bash", {"cmd": "rm -rf /"})
        check("pas de shell", "unknown tool" in unknown)
        malformed = dispatch(ctx, "search_repo", {})
        check("appel rg vide", "error" in malformed.lower())

        digest = sha256_file(root / "ok.py")
        eid = db.put("code", path="ok.py", sha256=digest, summary="v1")
        db.remember_file("ok.py", digest, "v1", eid)
        check("cache valide", db.cached_summary("ok.py", digest)["evidence_id"] == eid)
        (root / "ok.py").write_text("print(2)\n")
        check("cache invalide apres ecriture", db.cached_summary("ok.py", sha256_file(root / "ok.py")) is None)

    check("jira non configure", jira_provider.fetch("LYSI-1")["configured"] is False)
    check("confluence non configure", confluence_provider.fetch("page")["configured"] is False)
    check("confluence:// parse", parse_source("confluence://SPACE/page").scheme == "confluence")

    from local_agent.providers import atlassian
    from local_agent.providers.jira import _adf_text

    with tempfile.TemporaryDirectory() as envdir:
        root = Path(envdir)
        (root / ".claude").mkdir()
        (root / ".claude" / ".env.local").write_text(
            "JIRA_URL=https://example.atlassian.net\n"
            "JIRA_USERNAME=dev@example.com\n"
            "JIRA_API_TOKEN=fake-token\n"
            "LYSI_PASSWORD=must-not-load\n",
            encoding="utf-8",
        )
        creds = atlassian.credentials(root)
        check("mappe JIRA_URL", creds["base"] == "https://example.atlassian.net")
        check("mappe JIRA_USERNAME", creds["email"] == "dev@example.com")
        check("mappe JIRA_API_TOKEN", creds["token"] == "fake-token")
        check("ignore LYSI_PASSWORD", "LYSI_PASSWORD" not in atlassian._file_values(root))
        check("status configured", atlassian.status(root)["configured"] is True)
        check("status sans token", "token" not in atlassian.status(root))
    check("ADF en texte", "AC verbatim" in _adf_text(
        {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "AC verbatim"}]}]}
    ))
    check("HTML storage", confluence_provider.storage_text("<p>Hi &amp; you</p>") == "Hi & you")
    check("macro storage", "[children]" in confluence_provider.storage_text(
        '<ac:structured-macro ac:name="children"><ac:parameter ac:name="depth">3</ac:parameter></ac:structured-macro>'
    ))

    def fake_request(creds, path, params=None):
        if "/content/42" in path:
            return {
                "id": "42",
                "title": "Doc",
                "space": {"key": "LYSI"},
                "version": {"number": 3},
                "body": {"storage": {"value": "<h1>Title</h1><p>AC &amp; more</p>"}},
            }
        if path.endswith("/content/search"):
            return {"results": []}
        if path.endswith("/space"):
            return {"size": 1, "results": [{"key": "LYSI"}]}
        raise AssertionError(path)

    original_request = confluence_provider._request
    confluence_provider._request = fake_request
    try:
        with tempfile.TemporaryDirectory() as wiki:
            wiki_root = Path(wiki)
            (wiki_root / ".claude").mkdir()
            (wiki_root / ".claude" / ".env.local").write_text(
                "JIRA_URL=https://example.atlassian.net\n"
                "JIRA_USERNAME=dev@example.com\n"
                "JIRA_API_TOKEN=fake-token\n",
                encoding="utf-8",
            )
            page = confluence_provider.fetch("42", repo_root=wiki_root)
            check("fetch id", page.get("title") == "Doc" and "AC & more" in (page.get("body") or ""))
            missing = confluence_provider.fetch("LYSI/Missing", repo_root=wiki_root)
            check("page absente", "not found" in str(missing.get("error") or "").lower())
            ping = confluence_provider.ping(wiki_root)
            check("ping espaces", ping.get("ok") is True and ping.get("sample") == "LYSI")
    finally:
        confluence_provider._request = original_request

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        config = _git_repo(root)
        db = Store(root / "ctx.db")
        ctx = ToolContext(config, None, db, "auto", db.create_task("t", "auto"))
        broken = root / "broken.png"
        broken.write_bytes(b"not a png")
        result = dispatch(ctx, "inspect_image", {"path": str(broken)})
        check("png corrompu ne plante pas", "error" in result.lower() or "evidence" in result.lower())
        heavy = root / "heavy.png"
        heavy.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8_000_000)
        result = dispatch(ctx, "inspect_image", {"path": str(heavy)})
        check("image trop lourde", "trop lourde" in result.lower() or "error" in result.lower())
        unknown = dispatch(ctx, "apply_patch", {"patch_id": "does-not-exist"})
        check("patch inconnu", "unknown" in unknown.lower())
        from local_agent import edit as editmod

        patch_dir = root / "patches"
        patch_dir.mkdir()
        (patch_dir / "oldone.json").write_text("{}", encoding="utf-8")
        os.utime(patch_dir / "oldone.json", (time.time() - 30, time.time() - 30))
        previous_dir = editmod.PATCH_DIR
        previous_ret = editmod.PATCH_RETENTION_SECONDS
        editmod.PATCH_DIR = patch_dir
        editmod.PATCH_RETENTION_SECONDS = 5
        try:
            expired = dispatch(ctx, "apply_patch", {"patch_id": "oldone"})
        finally:
            editmod.PATCH_DIR = previous_dir
            editmod.PATCH_RETENTION_SECONDS = previous_ret
        check("patch expire", "expired" in expired.lower())

    class Blind:
        def supports_vision(self) -> bool:
            return False

        def complete(self, *args, **kwargs):
            raise AssertionError("no VLM")

    payload = reason(Config(vision=True), Blind(), Path("/etc/hosts"), "OCR", "layout")
    check("VLM absent : OCR continue", payload.get("vision") == "unavailable")
    disabled = reason(Config(vision=False), Blind(), Path("/etc/hosts"), "OCR", None)
    check("LOCAL_AGENT_VISION=0", disabled.get("vision") == "disabled")

    print("tous les controles de securite passent")


if __name__ == "__main__":
    main()
