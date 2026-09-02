#!/usr/bin/env python3
"""Patch propose/apply: dirty tree, source drift, second attempt, artifacts."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_agent.agent import run_task  # noqa: E402
from local_agent.agent_tools import ToolContext, dispatch  # noqa: E402
from local_agent.config import Config  # noqa: E402
from local_agent.edit import apply_patch, fix  # noqa: E402
from local_agent.mlx import Completion  # noqa: E402
from local_agent.store import Store  # noqa: E402


def check(name: str, condition: bool) -> None:
    print(f"  {'OK' if condition else 'KO'}  {name}")
    if not condition:
        raise SystemExit(1)


class Scripted:
    def __init__(self, replies: list[Completion], file_reply: str) -> None:
        self.replies = list(replies)
        self.file_reply = file_reply

    def capabilities(self) -> dict:
        return {"tool_use": True, "vision": False, "loaded": True, "id": "scripted"}

    def resolve_model(self) -> str:
        return "scripted"

    def complete_chat(self, messages, **kwargs):
        if not self.replies:
            raise AssertionError("plus de reponses scriptees")
        return self.replies.pop(0)

    def complete(self, *args, **kwargs):
        return Completion(text=self.file_reply)


def _git(folder: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=folder, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=folder, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init"],
        cwd=folder,
        check=True,
        capture_output=True,
    )


def main() -> None:
    envelope = "CHANGED: yes\nREASON: marker\n---BEGIN FILE---\nhello\n# patched\n---END FILE---\n"
    with tempfile.TemporaryDirectory() as raw:
        playground = Path(raw).resolve() / "repo"
        playground.mkdir()
        target = playground / "note.txt"
        target.write_text("hello\n", encoding="utf-8")
        _git(playground)
        config = Config(repo_root=playground, autonomy="patch", max_runtime=30, max_steps=6)
        client = Scripted([], envelope)
        report = fix(config, client, "note.txt", "add patched marker", mode="propose")
        patch_id = str((report.stats or {}).get("patch_id") or "")
        check("propose patch_id", bool(patch_id))
        check("propose n'ecrit pas", target.read_text() == "hello\n")

        target.write_text("hello\ndirty\n", encoding="utf-8")
        drifted = apply_patch(config, patch_id)
        check("source changee refusee", any("changed since" in item.lower() for item in drifted.risks))
        check("fichier non ecrase par apply", "dirty" in target.read_text())

        target.write_text("hello\n", encoding="utf-8")
        report2 = fix(config, client, "note.txt", "add patched marker", mode="propose")
        second_id = str((report2.stats or {}).get("patch_id") or "")
        check("seconde proposition", bool(second_id))

        call = {
            "id": "p1",
            "function": {"name": "propose_patch", "arguments": json.dumps({"path": "note.txt", "task": "marker"})},
        }
        agent_client = Scripted(
            [
                Completion(text="", tool_calls=[call], raw_message={"role": "assistant", "tool_calls": [call]}),
                Completion(
                    text='{"status":"success","summary":"proposed","confidence":0.8}',
                    tool_calls=None,
                ),
            ],
            envelope,
        )
        tasked = run_task(
            config,
            agent_client,
            "Fix the marker in note.txt",
            sources=["repo://."],
            autonomy="patch",
            store_path=Path(raw) / "t.db",
            trace=True,
        )
        check("artifact patch_id", bool(tasked.artifacts.get("patch_id")))
        check("trace locale", bool((tasked.stats or {}).get("trace_path")))
        check("why patch", tasked.stats.get("why") == "patch")

        db = Store(Path(raw) / "dirty.db")
        ctx = ToolContext(Config(repo_root=playground, autonomy="read_only"), None, db, "read_only", db.create_task("d", "read_only"))
        dirty = playground / "extra.txt"
        dirty.write_text("untracked\n", encoding="utf-8")
        refused = dispatch(ctx, "propose_patch", {"path": "note.txt", "task": "nope"})
        check("read_only refuse propose", "autonomy" in refused.lower() or "error" in refused.lower())
        db.close()

    print("tous les controles patch passent")


if __name__ == "__main__":
    main()
