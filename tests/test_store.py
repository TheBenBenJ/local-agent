#!/usr/bin/env python3
"""Session vs lifetime metrics, stale expand, Store ids are resolvable."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_agent.config import Config  # noqa: E402
from local_agent.report import Report  # noqa: E402
from local_agent.store import Store, attach_report_evidence, expand, sha256_file  # noqa: E402


def check(name: str, condition: bool) -> None:
    print(f"  {'OK' if condition else 'KO'}  {name}")
    if not condition:
        raise SystemExit(1)


def main() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        db_path = root / "context.db"
        os.environ["LOCAL_AGENT_SESSION"] = "sess-a"
        db = Store(db_path)
        db.record_metric(tool="local_search", source_type="repo", raw_tokens=10, visible_tokens=2, avoided_tokens=8)
        db.create_task("one", "read_only")
        os.environ["LOCAL_AGENT_SESSION"] = "sess-b"
        db.record_metric(tool="local_task", source_type="log", raw_tokens=90, visible_tokens=9, avoided_tokens=81)
        db.create_task("two", "read_only")
        payload = db.stats()
        check("current raw", payload["current"]["raw_tokens"] == 90)
        check("lifetime raw", payload["lifetime"]["raw_tokens"] == 100)
        check("current tasks", payload["current"]["local_tasks"] == 1)
        check("lifetime tasks", payload["lifetime"]["local_tasks"] == 2)
        check("session_id is B", payload["session_id"] == "sess-b")
        mixed = db.session_stats()
        check("compat raw is current", mixed["raw_tokens"] == 90)
        check("compat lifetime nested", mixed["lifetime"]["raw_tokens"] == 100)

        source = root / "note.py"
        source.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
        digest = sha256_file(source)
        eid = db.put("code", path=str(source), sha256=digest, lines="1-2", summary="note", payload={})
        fresh = expand(eid, db, Config(repo_root=root))
        check("expand current", fresh.get("status") == "current")
        check("expand excerpt", "1| alpha" in (fresh.get("excerpt") or ""))
        source.write_text("changed\n", encoding="utf-8")
        stale = expand(eid, db, Config(repo_root=root))
        check("stale status", stale.get("status") == "stale_evidence")
        check("stale reason", "source changed" in str(stale.get("reason") or "").lower())
        check("stale no silent excerpt", not stale.get("excerpt"))

        report = Report(title="Local search", summary="hits", files=["note.py"], locations=["note.py:1"])
        ids = attach_report_evidence(db, report)
        check("attach created store id", bool(ids) and ids[0].startswith("CODE-E"))
        check("expand attached id", expand(ids[0], db)["id"] == ids[0])

        fake = Report(
            title="Image compare",
            summary="diff",
            files=[str(source)],
            evidence=[{"type": "pixel_region", "content": "score=0.4", "box": {"x": 0}}],
        )
        img_ids = attach_report_evidence(db, fake)
        check("compare ids are store ids", img_ids[0].startswith("IMG-E"))
        check("no synthetic IMG-E1", fake.evidence[0]["id"] == img_ids[0])

        from local_agent.evidence import store as store_image

        shot = root / "board.png"
        shot.write_bytes(b"png")
        store_image(
            "baddcafe",
            {
                "path": str(shot),
                "sha256": sha256_file(shot),
                "transcript": "| Type | Valeur |\n| a | 1 |",
                "grid": [["Type", "Valeur"], ["a", "1"]],
                "regions": [{"id": "baddcafe-R1"}],
            },
        )
        image_full = expand("baddcafe", db)
        check("expand 8-char image packet", image_full.get("type") == "image")
        check("expand returns OCR transcript", "Type" in str((image_full.get("payload") or {}).get("transcript") or ""))
        db.close()

        garbage = root / "broken.db"
        garbage.write_bytes(b"not a database")
        recovered = Store(garbage)
        recovered.record_metric(tool="t", raw_tokens=1)
        check("corrupt db recovered", recovered.stats()["current"]["raw_tokens"] == 1)
        recovered.close()
        check("corrupt backup kept", garbage.with_name("broken.db.corrupt").is_file())

    print("tous les controles store/expand passent")


if __name__ == "__main__":
    main()
