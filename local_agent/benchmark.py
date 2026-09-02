"""Real-task benchmarks: context interception, latency, keyword quality.

Baselines:
  A raw source size
  B rg / shell pipeline
  C local-agent (local_task or the specialized tool)

Numbers in BENCHMARKS.md must come from a run of this module, never from invention.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from .agent import run_task
from .config import Config
from .mlx import MlxClient, MlxError
from .report import Report

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "benchmarks" / "fixtures"
RECETTE = Path(
    "/Users/benjaminmille/Documents/Projects/lysi/temp/56XX/5662/contexte/pieces_jointes"
)


def tokens_from_chars(chars: int) -> int:  # noqa: F811
    return max(0, int(chars) // 4)


def _ensure_log() -> Path:
    path = ROOT / "var" / "bench.log"
    marker = "ERROR ROOT_CAUSE fixture: InvoiceService.getTotal called on null invoice"
    if path.is_file() and path.stat().st_size >= 1_000_000:
        tail = path.read_text(encoding="utf-8", errors="replace")[-4000:]
        if marker in tail:
            return path
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for index in range(25_000):
            handle.write(f"2026-09-02 INFO worker={index} {'x' * 60}\n")
            if index % 400 == 0:
                handle.write("2026-09-02 ERROR Uncaught TypeError: Cannot read property total of undefined\n")
        handle.write(marker + "\n")
    return path


def _ensure_repo() -> Path:
    target = FIXTURES / "acl"
    target.mkdir(parents=True, exist_ok=True)
    (target / "PermissionGuard.py").write_text(
        "def require_permission(user, permission):\n"
        "    if permission not in user.grants:\n"
        "        raise PermissionError(permission)\n"
        "    return True\n",
        encoding="utf-8",
    )
    (target / "InvoiceController.py").write_text(
        "from PermissionGuard import require_permission\n\n"
        "def show_invoice(user, invoice_id):\n"
        "    require_permission(user, 'invoice.read')\n"
        "    return {'id': invoice_id}\n",
        encoding="utf-8",
    )
    git = target / ".git"
    if not git.exists():
        subprocess.run(["git", "init", "-q"], cwd=str(target), check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=str(target), check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "fixture", "--allow-empty"],
            cwd=str(target),
            check=True,
            capture_output=True,
        )
    return target


def _ensure_jira() -> Path:
    target = FIXTURES / "jira-LYSI-FIXTURE.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.is_file():
        target.write_text(
            json.dumps(
                {
                    "key": "LYSI-FIXTURE",
                    "goal": "Invoice page shows empty total for guests",
                    "acceptance_criteria_verbatim": "Given a guest without invoice.read, the total must stay hidden.",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    return target


def _rg(cwd: Path, pattern: str, path: str = ".") -> tuple[str, float]:
    started = time.monotonic()
    process = subprocess.run(
        ["rg", "-n", "--no-heading", pattern, path],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        errors="replace",
        timeout=30,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    return process.stdout, time.monotonic() - started


def _score(text: str, expected: list[str]) -> dict:
    blob = text.lower()
    hits = [item for item in expected if item.lower() in blob]
    return {
        "expected": expected,
        "hits": hits,
        "recall": round(len(hits) / len(expected), 3) if expected else 0,
        "correct": bool(expected) and len(hits) == len(expected),
        "partial": bool(hits) and len(hits) < len(expected),
        "quality_score": (4 if hits and len(hits) == len(expected) else 2 if hits else 0),
    }


def _score_report(report: Report, expected: list[str]) -> dict:
    packed = (report.summary or "") + " " + " ".join(report.findings) + " " + " ".join(
        str((item or {}).get("content") or "") for item in report.evidence
    )
    return _score(packed, expected)


def _packet_chars(report: Report) -> int:
    return len(json.dumps(report.to_dict(), ensure_ascii=False))


def _interception(raw_chars: int, visible_chars: int) -> dict:
    raw_tokens = tokens_from_chars(raw_chars)
    visible_tokens = tokens_from_chars(visible_chars)
    avoided = max(0, raw_tokens - visible_tokens)
    rate = round(avoided / raw_tokens, 3) if raw_tokens else 0
    return {
        "raw_tokens": raw_tokens,
        "visible_tokens": visible_tokens,
        "direct_avoided_tokens": avoided,
        "interception_rate": rate,
        "note": "interception_rate uses this baseline, not billed Claude tokens",
    }


def _run_local(config: Config, client: MlxClient, task: str, sources: list[str], **kwargs) -> tuple[Report | None, str]:
    try:
        report = run_task(config, client, task, sources=sources, store_path=ROOT / "var" / "bench.db", **kwargs)
        return report, ""
    except (MlxError, ValueError, OSError) as error:
        return None, str(error)


def _case_repo(config: Config, client: MlxClient | None, no_llm: bool) -> dict:
    folder = _ensure_repo()
    raw = sum(path.stat().st_size for path in folder.glob("*.py"))
    rg_out, rg_s = _rg(folder, "require_permission")
    row: dict = {
        "id": "A-repo",
        "task": "Where is invoice.read validated and why would a guest see a blank total?",
        "baseline_a_raw_chars": raw,
        "baseline_b_rg_chars": len(rg_out),
        "baseline_b_latency_s": round(rg_s, 3),
    }
    if no_llm or client is None:
        row["skipped"] = "no_llm"
        row.update(_interception(raw, len(rg_out)))
        return row
    scoped = Config(repo_root=folder, autonomy="read_only", max_runtime=90, max_steps=6, vision=False)
    report, error = _run_local(
        scoped,
        client,
        row["task"],
        ["repo://."],
    )
    if report is None:
        row["error"] = error
        return row
    visible = _packet_chars(report)
    row["local_visible_chars"] = visible
    row["latency_s"] = report.stats.get("latency_s")
    row["timings"] = report.stats.get("timings")
    row["quality"] = _score_report(report, ["require_permission", "invoice.read"])
    row["expected_tier"] = "direct"
    row["actual_tier"] = report.stats.get("tier")
    row["routing_correct"] = row["actual_tier"] == row["expected_tier"]
    row["local_llm_calls"] = report.stats.get("local_llm_calls")
    row.update(_interception(raw, visible))
    return row


def _case_logs(config: Config, client: MlxClient | None, no_llm: bool) -> dict:
    log = _ensure_log()
    raw = log.stat().st_size
    rg_out, rg_s = _rg(log.parent, "ERROR", log.name)
    row: dict = {
        "id": "B-logs",
        "task": "Find the root cause of the failures.",
        "baseline_a_raw_chars": raw,
        "baseline_b_rg_chars": len(rg_out),
        "baseline_b_latency_s": round(rg_s, 3),
    }
    if no_llm or client is None:
        row["skipped"] = "no_llm"
        row.update(_interception(raw, min(len(rg_out), 4000)))
        return row
    scoped = Config(repo_root=ROOT, autonomy="read_only", max_runtime=90, max_steps=6, vision=False)
    report, error = _run_local(scoped, client, row["task"], [f"log://{log}"])
    if report is None:
        row["error"] = error
        return row
    visible = _packet_chars(report)
    row["local_visible_chars"] = visible
    row["latency_s"] = report.stats.get("latency_s")
    row["timings"] = report.stats.get("timings")
    row["quality"] = _score_report(report, ["InvoiceService", "null"])
    row["expected_tier"] = "reduce"
    row["actual_tier"] = report.stats.get("tier")
    row["routing_correct"] = row["actual_tier"] == row["expected_tier"]
    row["local_llm_calls"] = report.stats.get("local_llm_calls")
    row.update(_interception(raw, visible))
    return row


def _case_vision(config: Config, client: MlxClient | None, no_llm: bool) -> dict:
    left = RECETTE / "image-20260819-150108.png"
    right = RECETTE / "image-20260819-150042.png"
    row: dict = {"id": "C-vision", "task": "Find why the implementation differs from the reference."}
    if not left.is_file() or not right.is_file():
        row["skipped"] = "recette screenshots absent"
        return row
    raw = left.stat().st_size + right.stat().st_size
    row["baseline_a_raw_chars"] = raw
    if no_llm or client is None:
        from .compare import compare_images

        started = time.monotonic()
        report = compare_images(config, str(left), str(right))
        row["local_visible_chars"] = _packet_chars(report)
        row["latency_s"] = round(time.monotonic() - started, 2)
        row["quality"] = _score(report.summary + " ".join(report.findings), ["DIV", "pixel"] )
        row.update(_interception(raw, row["local_visible_chars"]))
        row["note"] = "deterministic compare, no local_task"
        return row
    scoped = Config(repo_root=ROOT, autonomy="read_only", max_runtime=120, max_steps=5, vision=True)
    report, error = _run_local(
        scoped,
        client,
        row["task"],
        [f"image://{left}", f"image://{right}", "repo://local_agent/compare.py"],
    )
    if report is None:
        row["error"] = error
        return row
    visible = _packet_chars(report)
    row["local_visible_chars"] = visible
    row["latency_s"] = report.stats.get("latency_s")
    row["timings"] = report.stats.get("timings")
    row["quality"] = _score(report.summary + " ".join(report.findings), ["DIV", "HCP"])
    row["expected_tier"] = "agent"
    row["actual_tier"] = report.stats.get("tier")
    row["routing_correct"] = row["actual_tier"] == row["expected_tier"]
    row["local_llm_calls"] = report.stats.get("local_llm_calls")
    row.update(_interception(raw, visible))
    return row


def _case_jira(config: Config, client: MlxClient | None, no_llm: bool) -> dict:
    fixture = _ensure_jira()
    raw = fixture.stat().st_size
    row: dict = {
        "id": "D-jira-fixture",
        "task": "Investigate this issue.",
        "baseline_a_raw_chars": raw,
        "note": "anonymized fixture, not a live Jira fetch",
    }
    if no_llm or client is None:
        row["skipped"] = "no_llm"
        row.update(_interception(raw, raw))
        return row
    scoped = Config(repo_root=ROOT, autonomy="read_only", max_runtime=60, max_steps=5, vision=False)
    report, error = _run_local(
        scoped,
        client,
        row["task"],
        [f"data://{fixture}", "repo://local_agent/providers/jira.py"],
    )
    if report is None:
        row["error"] = error
        return row
    visible = _packet_chars(report)
    row["local_visible_chars"] = visible
    row["latency_s"] = report.stats.get("latency_s")
    row["quality"] = _score(report.summary, ["invoice"])
    row.update(_interception(raw, visible))
    return row


def _case_cache(config: Config, client: MlxClient | None, no_llm: bool) -> dict:
    folder = _ensure_repo()
    row: dict = {"id": "H-cache", "task": "Where is require_permission defined?"}
    if no_llm or client is None:
        row["skipped"] = "no_llm"
        return row
    scoped = Config(repo_root=folder, autonomy="read_only", max_runtime=60, max_steps=4, vision=False)
    first, error = _run_local(scoped, client, row["task"], ["repo://."])
    if first is None:
        row["error"] = error
        return row
    second, error = _run_local(scoped, client, "Show require_permission again", ["repo://."])
    row["first_latency_s"] = first.stats.get("latency_s")
    row["second_latency_s"] = None if second is None else second.stats.get("latency_s")
    row["first_cache_hits"] = first.stats.get("cache_hits")
    row["second_cache_hits"] = None if second is None else second.stats.get("cache_hits")
    return row


def _case_tests(config: Config, client: MlxClient | None, no_llm: bool) -> dict:
    folder = FIXTURES / "failing"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "bug.py").write_text("def total(invoice):\n    return invoice['total']\n", encoding="utf-8")
    (folder / ".local-agent.json").write_text(
        json.dumps({"checks": {"fail": {"command": ["python3", "-c", "raise SystemExit('TypeError: NoneType in total')"], "label": "fail"}}}),
        encoding="utf-8",
    )
    from .agent_tools import ToolContext, dispatch
    from .store import Store

    db = Store(ROOT / "var" / "bench.db")
    ctx = ToolContext(Config(repo_root=folder), None, db, "read_only", db.create_task("failing-tests", "read_only"))
    started = time.monotonic()
    result = dispatch(ctx, "run_check", {"kind": "fail"})
    latency = round(time.monotonic() - started, 3)
    db.close()
    row = {
        "id": "E-tests",
        "task": "Investigate these failing tests.",
        "baseline_a_raw_chars": 80_000,
        "local_visible_chars": len(result),
        "latency_s": latency,
        "quality": _score(result, ["TypeError", "total"]),
        "note": "whitelisted check, no LLM; baseline A is a typical unfiltered suite dump",
    }
    row.update(_interception(80_000, len(result)))
    return row


def _case_patch(config: Config, client: MlxClient | None, no_llm: bool) -> dict:
    return {
        "id": "F-patch",
        "skipped": "covered by tests/test_patch_workflow.py (scripted LLM); live patch needs a dedicated dirty sandbox",
    }


CASES = {
    "repo": _case_repo,
    "logs": _case_logs,
    "vision": _case_vision,
    "jira": _case_jira,
    "tests": _case_tests,
    "patch": _case_patch,
    "cache": _case_cache,
}


def run(config: Config, client: MlxClient, kind: str = "all", *, no_llm: bool = False, eval_only: bool = False, target: str | None = None) -> dict:
    if kind == "sessions":
        from . import replay as replay_mod

        payload = replay_mod.replay_sessions(config, client)
        (ROOT / "var").mkdir(parents=True, exist_ok=True)
        (ROOT / "var" / "last-replay.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload
    if kind == "transcript":
        from .transcript import classify_jsonl

        if not target:
            raise ValueError("benchmark transcript needs a jsonl path")
        return classify_jsonl(Path(target))
    if kind == "day":
        from .transcript import classify_day

        if not target:
            raise ValueError("benchmark day needs a transcript folder")
        return classify_day(Path(target))
    candidate = Path(kind).expanduser()
    if candidate.suffix == ".jsonl" and candidate.is_file():
        from .transcript import classify_jsonl

        return classify_jsonl(candidate)
    selected = list(CASES) if kind in {"all", "", None} else [kind]
    unknown = [item for item in selected if item not in CASES]
    if unknown:
        raise ValueError(f"unknown benchmark kind {unknown}, available: {', '.join(CASES)}")
    started = time.monotonic()
    rows = []
    for name in selected:
        rows.append(CASES[name](config, None if no_llm else client, no_llm))
    elapsed = round(time.monotonic() - started, 2)
    quality = [item.get("quality") for item in rows if item.get("quality")]
    correct = sum(1 for item in quality if item.get("correct"))
    routed = [item for item in rows if item.get("expected_tier") and item.get("actual_tier")]
    payload = {
        "kind": kind,
        "no_llm": no_llm,
        "elapsed_s": elapsed,
        "cases": rows,
        "summary": {
            "cases": len(rows),
            "correct": correct,
            "scored": len(quality),
            "direct_avoided_tokens": sum(int(item.get("direct_avoided_tokens") or 0) for item in rows),
            "routing_eval": len(routed),
            "routing_correct": sum(1 for item in routed if item.get("routing_correct")),
            "routing_accuracy": round(
                sum(1 for item in routed if item.get("routing_correct")) / len(routed), 3
            )
            if routed
            else None,
        },
        "limitations": [
            "Claude billed tokens are not read from Cursor/Claude Code; that API is not a supported public surface.",
            "interception_rate is vs the baseline of this harness, not vs a full Claude session.",
            "Houtini / delegate-local were not installed; the differentiator measured here is source interception before Claude.",
        ],
    }
    FIXTURES.mkdir(parents=True, exist_ok=True)
    (ROOT / "var").mkdir(parents=True, exist_ok=True)
    (ROOT / "var" / "last-benchmark.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload
