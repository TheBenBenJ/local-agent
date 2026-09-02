#!/usr/bin/env python3
"""Scénarios A–E du prompt architecture, sans serveur LLM (client scripté)."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_agent.agent import run_task  # noqa: E402
from local_agent.config import Config  # noqa: E402
from local_agent.mlx import Completion, MlxError  # noqa: E402
from local_agent.store import Store, sha256_bytes  # noqa: E402

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


class Dead:
    def capabilities(self) -> dict:
        return {"tool_use": True, "loaded": False, "id": "dead"}

    def resolve_model(self) -> str:
        return "dead"

    def complete_chat(self, *args, **kwargs):
        raise MlxError("down")

    def complete(self, *args, **kwargs):
        raise MlxError("down")


def _git_repo(folder: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=folder, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=folder, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init"],
        cwd=folder,
        check=True,
        capture_output=True,
    )


def _tool(name: str, arguments: dict, call_id: str = "c1") -> dict:
    return {
        "id": call_id,
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _final(summary: str, confidence: float = 0.8) -> Completion:
    return Completion(
        text=json.dumps(
            {
                "status": "success",
                "summary": summary,
                "root_cause": "",
                "findings": [],
                "changes": [],
                "questions": [],
                "confidence": confidence,
            }
        ),
        tool_calls=None,
    )


def main() -> None:
    home = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as raw:
        work = Path(raw)

        db = Store(work / "a.db")
        task_id = db.create_task("A", "read_only", "scripted")
        eid = db.put("code", source="repo://store.py", summary="sqlite evidence", sha256=sha256_bytes(b"x"), task_id=task_id)
        check("A id CODE-E1", eid == "CODE-E1")
        check("A alias E1", db.get("E1")["summary"] == "sqlite evidence")
        db.close()

        shot_a = work / "a.png"
        shot_b = work / "b.png"
        shot_a.write_bytes(PNG_1X1)
        shot_b.write_bytes(PNG_1X1)
        playground = work / "repo"
        playground.mkdir()
        (playground / "note.txt").write_text("hello\n", encoding="utf-8")
        (playground / "store.py").write_text("class Store:\n    pass\n", encoding="utf-8")
        (playground / ".local-agent.json").write_text(
            json.dumps({"checks": {"fail": {"command": ["python3", "-c", "raise SystemExit(1)"], "label": "fail"}}}),
            encoding="utf-8",
        )
        _git_repo(playground)
        vision_off = Config(repo_root=playground, autonomy="read_only", max_runtime=30, max_steps=6, vision=False)

        compared = run_task(
            vision_off,
            Scripted([_final("two screenshots compared")]),
            "Compare the two screenshots",
            sources=[f"image://{shot_a}", f"image://{shot_b}"],
            store_path=work / "b.db",
        )
        check("B STATUS", "STATUS:" in compared.summary)
        check("B preuves image", bool(compared.evidence))

        fail_call = _tool("run_check", {"kind": "fail"})
        failed = run_task(
            vision_off,
            Scripted(
                [
                    Completion(text="", tool_calls=[fail_call], raw_message={"role": "assistant", "tool_calls": [fail_call]}),
                    _final("tests ran", 0.9),
                ]
            ),
            "Run the project checks",
            sources=["repo://."],
            store_path=work / "c.db",
        )
        check("C tests FAIL escalate", "needs_claude" in failed.summary.lower())
        check("C TESTS FAIL", "FAIL" in failed.summary)

        search_call = _tool("search_repo", {"pattern": "class Store"})
        found = run_task(
            Config(repo_root=home, autonomy="read_only", max_runtime=30, max_steps=6),
            Scripted(
                [
                    Completion(
                        text="",
                        tool_calls=[search_call],
                        raw_message={"role": "assistant", "tool_calls": [search_call]},
                    ),
                    _final("Store lives in store.py"),
                ]
            ),
            "Where is the evidence Store class?",
            sources=["repo://local_agent"],
            store_path=work / "d.db",
        )
        check("D STATUS", "STATUS:" in found.summary)
        check("D evidence", bool(found.evidence))

        envelope = "CHANGED: yes\nREASON: marker\n---BEGIN FILE---\nhello\n# patched\n---END FILE---\n"
        patch_call = _tool("propose_patch", {"path": "note.txt", "task": "add patched marker"})
        patched = run_task(
            Config(repo_root=playground, autonomy="patch", max_runtime=30, max_steps=6),
            Scripted(
                [
                    Completion(text="", tool_calls=[patch_call], raw_message={"role": "assistant", "tool_calls": [patch_call]}),
                    _final("patch proposed"),
                ],
                file_reply=envelope,
            ),
            "Add a patched marker in note.txt",
            sources=["repo://."],
            autonomy="patch",
            store_path=work / "e.db",
        )
        check("E fichier intact", (playground / "note.txt").read_text() == "hello\n")
        check("E propose", "STATUS:" in patched.summary)

        down = run_task(
            Config(repo_root=home, autonomy="read_only", max_runtime=30, max_steps=6, vision=False),
            Dead(),
            "Where is Store?",
            sources=["repo://local_agent"],
            store_path=work / "dead.db",
        )
        check("DIRECT ignore LLM down", down.stats.get("tier") == "direct")
        check("DIRECT llm_calls 0", down.stats.get("local_llm_calls") == 0)
        check("DIRECT status", "STATUS:" in down.summary)

        down_agent = run_task(
            Config(repo_root=home, autonomy="read_only", max_runtime=30, max_steps=6, vision=False, force_tier="agent"),
            Dead(),
            "Why do expired contracts remain visible?",
            sources=["repo://."],
            store_path=work / "dead-agent.db",
        )
        check("AGENT LLM down escalate", "needs_claude" in down_agent.summary.lower())
        check("AGENT LLM down message", "unavailable" in down_agent.summary.lower())

        list_call = _tool("list_files", {"path": "."})
        budget = run_task(
            Config(repo_root=playground, autonomy="read_only", max_runtime=30, max_steps=1, vision=False, force_tier="agent"),
            Scripted(
                [
                    Completion(text="", tool_calls=[list_call], raw_message={"role": "assistant", "tool_calls": [list_call]}),
                ]
            ),
            "Why do expired contracts remain visible?",
            sources=["repo://."],
            store_path=work / "budget.db",
        )
        check("budget steps", "needs_claude" in budget.summary.lower() or budget.stats.get("stop") == "budget")

        from local_agent.report import Report, render_json

        bulky = Report(
            title="Local task",
            summary="STATUS: success",
            evidence=[{"id": f"IMG-E{i}", "type": "image", "content": "x" * 400} for i in range(30)],
        )
        encoded = render_json(bulky, Config(max_output_tokens=200))
        json.loads(encoded)
        check("json clamp reste valide", True)

    print("tous les scenarios A-E passent")


if __name__ == "__main__":
    main()
