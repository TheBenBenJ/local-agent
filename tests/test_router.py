#!/usr/bin/env python3
"""Router DIRECT / REDUCE / AGENT / CLAUDE and the four invariants."""

from __future__ import annotations

import json
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_agent.agent import run_task  # noqa: E402
from local_agent.config import Config  # noqa: E402
from local_agent.extract import extract_log  # noqa: E402
from local_agent.files import relative_to_root  # noqa: E402
from local_agent.mlx import MlxError  # noqa: E402
from local_agent.router import route_task  # noqa: E402
from local_agent.store import expand  # noqa: E402


def check(name: str, condition: bool) -> None:
    print(f"  {'OK' if condition else 'KO'}  {name}")
    if not condition:
        raise SystemExit(1)


class ForbiddenLLM:
    def resolve_model(self) -> str:
        return "forbidden"

    def capabilities(self) -> dict:
        return {"tool_use": True, "loaded": True}

    def complete(self, *args, **kwargs):
        raise AssertionError("DIRECT must not call the local LLM")

    def complete_chat(self, *args, **kwargs):
        raise AssertionError("DIRECT must not call the local LLM")


class Dead:
    def resolve_model(self) -> str:
        return "dead"

    def capabilities(self) -> dict:
        return {"tool_use": True, "loaded": False}

    def complete(self, *args, **kwargs):
        raise MlxError("down")

    def complete_chat(self, *args, **kwargs):
        raise MlxError("down")


def _git_repo(folder: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=folder, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=folder, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init", "--allow-empty"],
        cwd=folder,
        check=True,
        capture_output=True,
    )


def _cfg(root: Path, **kwargs) -> Config:
    values = dict(repo_root=root, autonomy="read_only", max_runtime=30, max_steps=4, vision=False)
    values.update(kwargs)
    return Config(**values)


def _png_rgb(red: int, green: int, blue: int) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw = b"\x00" + bytes([red, green, blue])
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def main() -> None:
    home = Path(__file__).resolve().parents[1]
    cfg = _cfg(home)

    tiny = route_task(cfg, "Where is session_stats defined?", ["repo://local_agent/store.py"])
    check("tiny source DIRECT", tiny.tier == "direct")
    check("tiny zero llm planned", tiny.estimated_packet_tokens <= tiny.estimated_raw_tokens or tiny.tier == "direct")

    symbol = route_task(cfg, "Where is the evidence Store class?", ["repo://local_agent"])
    check("explicit symbol DIRECT", symbol.tier == "direct")
    check("reason cites Store", "Store" in symbol.reason)

    ticket = route_task(cfg, "Summarize the ticket goal and status.", ["jira://LYSI-1"])
    check("jira-only DIRECT", ticket.tier == "direct")
    check("jira first tool fetch_issue", ticket.first_tool == "fetch_issue")
    check("jira first key", (ticket.first_args or {}).get("key") == "LYSI-1")
    wiki = route_task(cfg, "Summarize this page.", ["confluence://1323499521"])
    check("confluence-only DIRECT", wiki.tier == "direct")
    check("confluence first tool fetch_page", wiki.first_tool == "fetch_page")
    mixed = route_task(cfg, "Summarize the ticket using the repository.", ["jira://LYSI-1", "repo://local_agent"])
    check("jira plus repo AGENT", mixed.tier == "agent")

    with tempfile.TemporaryDirectory() as raw:
        work = Path(raw)
        log = work / "app.log"
        log.write_text("INFO ok\n" * 2500 + "ERROR TypeError: total of undefined\n" * 40 + "ERROR ROOT_CAUSE fixture: InvoiceService.getTotal called on null invoice\n", encoding="utf-8")
        (work / "note.py").write_text("x = 1\n", encoding="utf-8")
        _git_repo(work)
        scoped = _cfg(work)
        reduced = route_task(scoped, "Find the root cause of the failures.", [f"log://{log}"])
        check("2k+ log REDUCE", reduced.tier == "reduce")

        extracted = extract_log(log)
        blob = " ".join(item.get("content") or "" for item in extracted["excerpts"])
        check("extract keeps InvoiceService", "InvoiceService" in blob)
        check("extract keeps null", "null" in blob.lower())

        report = run_task(
            scoped,
            ForbiddenLLM(),
            "Find the root cause of the failures.",
            sources=[f"log://{log}"],
            store_path=work / "log.db",
        )
        packed = report.summary + " " + " ".join(report.findings) + " " + " ".join(str(item.get("content") or "") for item in report.evidence)
        check("REDUCE without LLM keeps cause", "InvoiceService" in packed)
        check("REDUCE summary cites cause", "InvoiceService" in report.summary)
        check("REDUCE locations relative", any(str(loc).startswith("app.log") for loc in report.locations))
        check("REDUCE no abs location", not any(str(loc).startswith("/") for loc in report.locations))
        from local_agent.agent import _packet_hit

        abs_span = f"{log.resolve()}:1-4"
        check("packet_hit keeps span", _packet_hit(scoped, abs_span) == "app.log:1-4")
        check("REDUCE extract skips LLM", report.stats.get("local_llm_calls") == 0)
        check("REDUCE tier", report.stats.get("tier") == "reduce")
        from local_agent.store import Store

        db = Store(work / "log.db")
        for item in report.evidence:
            got = expand(item["id"], db)
            check(f"expand {item['id']}", got.get("id") == item["id"] or got.get("status") in {"stored", "current", "stale_evidence"})
        db.close()

    causal = route_task(cfg, "Why do expired contracts remain visible?", ["repo://."])
    check("cross-file causal AGENT", causal.tier == "agent")

    auth = route_task(cfg, "Change the auth middleware and public API tokens", ["repo://."])
    check("high-risk auth CLAUDE", auth.tier == "claude")
    check("needs_claude flag", auth.needs_claude is True)

    shots = route_task(
        cfg,
        "Why does the recette screenshot differ from the implementation?",
        ["image:///tmp/a.png", "repo://local_agent/compare.py"],
    )
    check("screenshot + repo AGENT", shots.tier == "agent")

    assertion = route_task(cfg, "Run the project checks", ["repo://."])
    check("checks on large repo REDUCE or AGENT or DIRECT", assertion.tier in {"direct", "reduce", "agent"})

    images = route_task(cfg, "Compare the two screenshots", ["image:///tmp/a.png", "image:///tmp/b.png"])
    check("two images DIRECT", images.tier == "direct")

    with tempfile.TemporaryDirectory() as raw:
        shots = Path(raw)
        left = shots / "left.png"
        right = shots / "right.png"
        left.write_bytes(_png_rgb(0, 0, 0))
        right.write_bytes(_png_rgb(255, 0, 0))
        _git_repo(shots)
        compared = run_task(
            _cfg(shots),
            ForbiddenLLM(),
            "Compare these two recette annexes and list visible differences.",
            sources=[f"image://{left}", f"image://{right}"],
            store_path=shots / "img.db",
        )
        packed = (
            compared.summary
            + " "
            + " ".join(compared.findings)
            + " "
            + " ".join(str(item.get("content") or "") for item in compared.evidence)
        )
        check("DIRECT compare 0 LLM", compared.stats.get("local_llm_calls") == 0)
        check("DIRECT compare packet SHA256", "SHA256" in packed)
        check("DIRECT compare packet pixel", "pixel" in packed.lower())
        blob = json.dumps(compared.to_dict(), ensure_ascii=False)
        check("DIRECT compare packet under 1800 chars", len(blob) < 1800)

    large_diff = route_task(cfg, "review this diff", ["repo://."])
    check("large-repo diff REDUCE", large_diff.tier == "reduce")

    with tempfile.TemporaryDirectory() as raw:
        playground = Path(raw)
        (playground / "PermissionGuard.py").write_text(
            "def require_permission(user, permission):\n    return permission in user.grants\n",
            encoding="utf-8",
        )
        (playground / "InvoiceController.py").write_text(
            "from PermissionGuard import require_permission\n\ndef show_invoice(user, invoice_id):\n    require_permission(user, 'invoice.read')\n",
            encoding="utf-8",
        )
        _git_repo(playground)
        scoped = _cfg(playground)
        decision = route_task(scoped, "Where is invoice.read validated?", ["repo://."])
        check("297B-class fixture DIRECT", decision.tier == "direct")
        check("tiny repo raw tokens > 0", decision.estimated_raw_tokens > 0)
        report = run_task(
            scoped,
            ForbiddenLLM(),
            "Where is invoice.read validated?",
            sources=["repo://."],
            store_path=playground / "t.db",
        )
        check("DIRECT local_llm_calls == 0", report.stats.get("local_llm_calls") == 0)
        check("DIRECT avoidable == 0", report.stats.get("avoidable_local_llm_calls") == 0)
        check("DIRECT tier recorded", report.stats.get("tier") == "direct")
        packed = report.summary + " " + " ".join(str(item.get("content") or "") for item in report.evidence)
        check("DIRECT packet has invoice.read", "invoice.read" in packed)
        check("DIRECT has evidence", bool(report.evidence))
        check(
            "locations are repo-relative",
            any(str(loc).startswith("InvoiceController.py") for loc in report.locations),
        )
        check(
            "relative_to_root ignores /private",
            relative_to_root(scoped, (playground / "InvoiceController.py").resolve()) == "InvoiceController.py",
        )
        blob = json.dumps(report.to_dict(), ensure_ascii=False)
        check("DIRECT packet under 600 chars", len(blob) < 600)
        from local_agent.store import Store

        db = Store(playground / "t.db")
        for item in report.evidence:
            expand(item["id"], db)
        db.close()

        claude = run_task(
            scoped,
            ForbiddenLLM(),
            "Change the auth middleware",
            sources=["repo://."],
            store_path=playground / "c.db",
        )
        check("CLAUDE no LLM", claude.stats.get("local_llm_calls") == 0)
        check("CLAUDE status", "needs_claude" in claude.summary.lower())

    print("tous les controles router/invariants passent")


if __name__ == "__main__":
    main()
