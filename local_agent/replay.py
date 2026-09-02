"""Replay real Claude sessions from artifacts on disk, not from billed usage."""

from __future__ import annotations

from pathlib import Path

from .agent import run_task
from .benchmark import _interception, _packet_chars, _score_report, tokens_from_chars
from .config import Config
from .mlx import MlxClient

LYSI = Path("/Users/benjaminmille/Documents/Projects/lysi")
RECETTE_5177 = Path("/Users/benjaminmille/projects/lysi-recettes/LYSI-5177/screens_recette")
RECETTE_5662 = LYSI / "temp/56XX/5662/contexte/pieces_jointes"
LOCAL_AGENT = Path(__file__).resolve().parent.parent
BENCH_LOG = LOCAL_AGENT / "var" / "bench.log"


def _existing(paths: list[Path]) -> list[Path]:
    return [path for path in paths if path.is_file()]


def _raw_bytes(paths: list[Path]) -> int:
    return sum(path.stat().st_size for path in paths)


def replay_sessions(config: Config, client: MlxClient) -> dict:
    rows = [_session_a(config, client), _session_b(config, client), _session_c(config, client)]
    return {
        "kind": "sessions",
        "note": (
            "Reconstructed from artifacts still on disk. "
            "jsonl transcripts are not interceptable (already in Claude). "
            "Not billed Claude tokens."
        ),
        "cases": rows,
    }


def _session_a(config: Config, client: MlxClient) -> dict:
    """Type A: recette UI, screenshots on disk (LYSI-5177)."""
    shots = _existing(
        [
            RECETTE_5177 / "02_rousselot_annexe_planning.png",
            RECETTE_5177 / "06_aubert_annexe_planning.png",
        ]
    )
    row = {
        "id": "session-A-ui-5177",
        "type": "recette_ui",
        "transcript": "a64d3fc5-3d03-4dd7-a605-6a4740661fa0",
        "must_remain_claude_visible": ["RECETTE.md instructions", "Jira LYSI-5177 if already opened"],
        "non_interceptable": ["1.1 MB jsonl already in the Claude thread"],
        "expected_tier": "direct",
    }
    if len(shots) < 2:
        row["skipped"] = "LYSI-5177 screenshots absent"
        return row
    raw = _raw_bytes(shots)
    scoped = Config(repo_root=LOCAL_AGENT, autonomy="read_only", max_runtime=60, vision=False)
    report = run_task(
        scoped,
        client,
        "Compare these two recette annexes and list visible differences.",
        sources=[f"image://{shots[0]}", f"image://{shots[1]}"],
        store_path=LOCAL_AGENT / "var" / "replay.db",
    )
    visible = _packet_chars(report)
    row.update(
        {
            "raw_chars": raw,
            "claude_visible_chars": visible,
            "actual_tier": report.stats.get("tier"),
            "local_llm_calls": report.stats.get("local_llm_calls"),
            "latency_s": report.stats.get("latency_s"),
            "quality": _score_report(report, ["pixel", "SHA256"] ),
        }
    )
    row.update(_interception(raw, visible))
    return row


def _session_b(config: Config, client: MlxClient) -> dict:
    """Type B: repo exploration, few images (local-agent router + agent)."""
    files = _existing(
        [
            LOCAL_AGENT / "local_agent" / "router.py",
            LOCAL_AGENT / "local_agent" / "agent.py",
        ]
    )
    raw = _raw_bytes(files)
    scoped = Config(repo_root=LOCAL_AGENT, autonomy="read_only", max_runtime=60, vision=False)
    report = run_task(
        scoped,
        client,
        "Where is route_task defined?",
        sources=["repo://local_agent/router.py"],
        store_path=LOCAL_AGENT / "var" / "replay.db",
    )
    visible = _packet_chars(report)
    row = {
        "id": "session-B-module",
        "type": "repo_exploration",
        "transcript": "a8ca4c10-98de-4765-970a-616dd2d12b55",
        "must_remain_claude_visible": ["architecture decisions", "this chat's instructions"],
        "non_interceptable": ["2.5 MB jsonl already in the Claude thread"],
        "expected_tier": "direct",
        "raw_chars": raw,
        "claude_visible_chars": visible,
        "actual_tier": report.stats.get("tier"),
        "local_llm_calls": report.stats.get("local_llm_calls"),
        "latency_s": report.stats.get("latency_s"),
        "quality": _score_report(report, ["route_task", "router.py"]),
    }
    row.update(_interception(raw, visible))
    return row


def _session_c(config: Config, client: MlxClient) -> dict:
    """Type C: large log incident (harness fixture used as the interceptable source)."""
    from .benchmark import _ensure_log

    log = _ensure_log()
    raw = log.stat().st_size
    scoped = Config(repo_root=LOCAL_AGENT, autonomy="read_only", max_runtime=60, vision=False)
    report = run_task(
        scoped,
        client,
        "Find the root cause of the failures.",
        sources=[f"log://{log}"],
        store_path=LOCAL_AGENT / "var" / "replay.db",
    )
    visible = _packet_chars(report)
    row = {
        "id": "session-C-log",
        "type": "logs_incident",
        "transcript": "reconstructed from interceptable log artifact, not a billed day",
        "must_remain_claude_visible": ["evidence packet", "expand on demand"],
        "non_interceptable": ["Claude reasoning"],
        "expected_tier": "reduce",
        "raw_chars": raw,
        "claude_visible_chars": visible,
        "actual_tier": report.stats.get("tier"),
        "local_llm_calls": report.stats.get("local_llm_calls"),
        "latency_s": report.stats.get("latency_s"),
        "quality": _score_report(report, ["InvoiceService", "null"]),
    }
    row.update(_interception(raw, visible))
    return row
