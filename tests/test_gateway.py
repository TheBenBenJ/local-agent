#!/usr/bin/env python3
"""Gateway, store, risk, agent loop, image compare. Pas de serveur LLM requis."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_agent.agent import run_task  # noqa: E402
from local_agent.agent_tools import ToolContext, dispatch  # noqa: E402
from local_agent.compare import compare_images, pixel_diff, png_size  # noqa: E402
from local_agent.config import Config  # noqa: E402
from local_agent.gateway import parse_source, parse_sources  # noqa: E402
from local_agent.mlx import Completion  # noqa: E402
from local_agent.providers import jira as jira_provider  # noqa: E402
from local_agent.risk import needs_claude, normalize_autonomy, score_confidence, task_risk  # noqa: E402
from local_agent.store import Store, expand, sha256_bytes  # noqa: E402

PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000"
    "907753de0000000c4944415408d763f8cf0000020101e2d26d3f0000000049454e44ae426082"
)


def check(name: str, condition: bool) -> None:
    status = "OK" if condition else "KO"
    print(f"  {status}  {name}")
    if not condition:
        raise SystemExit(1)


class Scripted:
    def __init__(self, replies: list[Completion], file_reply: str | None = None) -> None:
        self.replies = list(replies)
        self.file_reply = file_reply
        self.n = 0

    def capabilities(self) -> dict:
        return {"tool_use": True, "vision": False, "loaded": True, "id": "scripted"}

    def resolve_model(self) -> str:
        return "scripted"

    def complete_chat(self, messages, **kwargs):
        if not self.replies:
            raise AssertionError("plus de reponses scriptees")
        self.n += 1
        return self.replies.pop(0)

    def complete(self, *args, **kwargs):
        if self.file_reply:
            return Completion(text=self.file_reply)
        raise AssertionError("complete() ne doit pas servir si complete_chat existe")


def main() -> None:
    check("repo implicite", parse_source("src/Foo").scheme == "repo")
    check("image par suffixe", parse_source("/tmp/a.png").scheme == "image")
    check("repo://.", parse_source("repo://.").reference == ".")
    check("jira key", parse_source("jira://LYSI-1").reference == "LYSI-1")
    check("sources vides = repo", parse_sources(None)[0].scheme == "repo")
    try:
        parse_source("http://example.com")
        check("refuse http", False)
    except ValueError:
        check("refuse http", True)

    check("safe = patch", normalize_autonomy("safe") == "patch")
    check("auto explicite", normalize_autonomy("auto") == "auto")
    check("defaut read_only", normalize_autonomy(None) == "read_only")
    check("auth HIGH", task_risk("changer l'authentification oauth") == "HIGH")
    check("typo LOW", task_risk("rename a local variable") == "LOW")
    check("HIGH force escalate", needs_claude(0.99, "HIGH", 0.7) is True)
    check("MEDIUM ne force pas escalate", needs_claude(0.65, "LOW", 0.7) is False)
    check("refactor MEDIUM", task_risk("refactor a local helper") == "MEDIUM")
    check("heuristic bornée", 0.2 <= score_confidence(found=True, tests="PASS", risk="LOW", loop_stopped=False, tool_errors=0, steps=3) <= 0.95)

    check("sans credentials Jira", jira_provider.fetch("LYSI-1")["configured"] is False)

    with tempfile.TemporaryDirectory() as raw:
        db = Store(Path(raw) / "context.db")
        task_id = db.create_task("t", "read_only", "scripted")
        eid = db.put("code", source="repo://a.py", summary="hello", sha256=sha256_bytes(b"x"), task_id=task_id)
        check("id CODE-E1", eid == "CODE-E1")
        got = db.get("E1")
        check("alias E1", got["summary"] == "hello")
        check("get CODE-E1", db.get("CODE-E1")["summary"] == "hello")
        db.remember_file("a.py", "abc", "cached", eid)
        check("cache hit", db.cached_summary("a.py", "abc")["evidence_id"] == eid)
        check("cache miss", db.cached_summary("a.py", "zzz") is None)
        db.record_metric(tool="local_task", source_type="repo", raw_tokens=100, visible_tokens=10, avoided_tokens=90)
        stats = db.session_stats()
        check("stats raw", stats["raw_tokens"] == 100)
        db.finish_task(task_id, status="success", confidence=0.8, risk="LOW")
        check("expand sqlite", expand("E1", db)["id"] == "CODE-E1")
        try:
            expand("E999", db)
            check("expand inconnu", False)
        except Exception:
            check("expand inconnu", True)

        config = Config(repo_root=Path(__file__).resolve().parents[1], autonomy="read_only", max_runtime=30, max_steps=6)
        ctx = ToolContext(config, Scripted([]), db, "read_only", task_id)
        refused = dispatch(ctx, "propose_patch", {"path": "README.md", "task": "nope"})
        check("read_only refuse patch", "autonomy" in refused.lower() or "error" in refused.lower())

        playground = Path(raw) / "repo"
        playground.mkdir()
        (playground / "note.txt").write_text("hello\n", encoding="utf-8")
        subprocess.run(["git", "init"], cwd=playground, check=True, capture_output=True)
        subprocess.run(["git", "add", "note.txt"], cwd=playground, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init"],
            cwd=playground,
            check=True,
            capture_output=True,
        )
        envelope = (
            "CHANGED: yes\nREASON: marker\n---BEGIN FILE---\nhello\n# patched\n---END FILE---\n"
        )
        patch_call = {
            "id": "p1",
            "function": {"name": "propose_patch", "arguments": '{"path":"note.txt","task":"ajoute # patched"}'},
        }
        patcher = Scripted(
            [
                Completion(text="", tool_calls=[patch_call], raw_message={"role": "assistant", "tool_calls": [patch_call]}),
                Completion(
                    text='{"status":"success","summary":"patch propose","root_cause":"","findings":[],"changes":["note.txt"],"questions":["relire le diff"],"confidence":0.8}',
                    tool_calls=None,
                ),
            ],
            file_reply=envelope,
        )
        patch_config = Config(repo_root=playground, autonomy="patch", max_runtime=30, max_steps=6)
        patched = run_task(
            patch_config,
            patcher,
            "Add a patched marker in note.txt",
            sources=["repo://."],
            autonomy="patch",
            store_path=Path(raw) / "patch.db",
        )
        check("patch ne reecrit pas le fichier", (playground / "note.txt").read_text() == "hello\n")
        check("patch propose sans auto", "STATUS:" in patched.summary)
        check("patch n'applique pas", "auto" not in str(patched.stats.get("autonomy")))

        recette = Path("/Users/benjaminmille/Documents/Projects/lysi/temp/56XX/5662/contexte/pieces_jointes")
        shot_a = recette / "image-20260819-150108.png"
        shot_b = recette / "image-20260819-150042.png"
        if shot_a.is_file() and shot_b.is_file():
            same = pixel_diff(shot_a, shot_a)
            check("pixel identique ratio 0", float(same.get("changedRatio") or 0) == 0)
            delta = pixel_diff(shot_a, shot_b)
            check("pixel different ratio > 0", float(delta.get("changedRatio") or 0) > 0)
            compared = compare_images(config, str(shot_a), str(shot_b))
            check("compare cite le diff pixel", any("pixel" in item.lower() for item in compared.findings))
        else:
            print("  SKIP  captures recette absentes")

        shot = Path(raw) / "a.png"
        shot.write_bytes(PNG_1X1)
        check("png 1x1", png_size(shot) == (1, 1))
        identical = compare_images(config, str(shot), str(shot))
        check("compare identique", "identical" in identical.summary.lower() or "SHA256" in identical.summary)

        tool_call = {
            "id": "c1",
            "type": "function",
            "function": {"name": "search_repo", "arguments": '{"pattern": "class Store"}'},
        }
        client = Scripted(
            [
                Completion(text="", tool_calls=[tool_call], raw_message={"role": "assistant", "content": None, "tool_calls": [tool_call]}),
                Completion(
                    text='{"status":"success","summary":"Store vit dans store.py","root_cause":"","findings":["sqlite evidence"],"changes":[],"questions":[],"confidence":0.8}',
                    tool_calls=None,
                ),
            ]
        )
        report = run_task(config, client, "Where is the evidence Store class?", sources=["repo://local_agent"], store_path=Path(raw) / "t2.db")
        check("local_task status dans le resume", "STATUS:" in report.summary)
        check("a cherche dans le repo", client.n >= 1)
        check("evidence non vide", bool(report.evidence))
        check("evidence a un extrait", bool(str((report.evidence[0] or {}).get("content") or "").strip()))
        check("backfill locations", any("store.py" in str(loc) for loc in report.locations))
        check("heuristic documentee", "heuristic" in report.summary.lower())

        looping = {
            "id": "c2",
            "function": {"name": "search_repo", "arguments": '{"pattern": "zzzz"}'},
        }
        looper = Scripted(
            [
                Completion(text="", tool_calls=[looping], raw_message={"role": "assistant", "tool_calls": [looping]}),
                Completion(text="", tool_calls=[looping], raw_message={"role": "assistant", "tool_calls": [looping]}),
                Completion(text="", tool_calls=[looping], raw_message={"role": "assistant", "tool_calls": [looping]}),
            ]
        )
        stuck = run_task(config, looper, "find zzzz", sources=["repo://."], store_path=Path(raw) / "t3.db")
        check("boucle detectee", "needs_claude" in stuck.summary.lower() or stuck.stats.get("stop") == "repeated tool call")

        stalling = [
            Completion(
                text="",
                tool_calls=[{"id": f"s{i}", "function": {"name": "list_files", "arguments": json.dumps({"path": path})}}],
                raw_message={"role": "assistant", "tool_calls": [{"id": f"s{i}", "function": {"name": "list_files", "arguments": json.dumps({"path": path})}}]},
            )
            for i, path in enumerate((".", "local_agent", "tests"))
        ]
        stall_client = Scripted(stalling)
        stalled = run_task(config, stall_client, "map the tree", sources=["repo://."], store_path=Path(raw) / "t4.db")
        check("stall sans preuve", stalled.stats.get("stop") == "no new evidence" or "needs_claude" in stalled.summary.lower())

    print("tous les controles gateway/store/agent passent")


if __name__ == "__main__":
    main()
