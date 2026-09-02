"""Boucle agentique locale : le brut ne quitte pas la machine, Claude recoit le paquet."""

from __future__ import annotations

import json
import time
from collections import Counter

from dataclasses import replace

from . import agent_tools, gateway, prompts, risk, tasks
from .config import Config
from .files import GuardrailError
from .mlx import MlxClient, MlxError
from .ocr import IMAGE_OUTPUT_TOKENS
from .report import Report
from .store import Store

DENIED_REPEAT = 3
STALL_STEPS = 3


def _packet_item(db: Store, identifier: str) -> dict:
    row = db.get(identifier)
    preview = str(row.get("summary") or row.get("source") or "").strip()
    matches = (row.get("payload") or {}).get("matches") or []
    if matches and isinstance(matches[0], dict):
        first = matches[0]
        loc = f"{first.get('file')}:{first.get('line')}"
        text = str(first.get("text") or "").strip()
        extra = f"{loc} {text}".strip()
        if extra and extra not in preview:
            preview = f"{preview}; {extra}" if preview else extra
    return {
        "id": identifier,
        "type": row.get("type") or "evidence",
        "content": preview[:220],
        "source": row.get("source") or "",
        "confidence": row.get("confidence"),
    }


def _backfill_hits(
    db: Store,
    identifiers: list[str],
    files: list[str],
    locations: list[str],
) -> tuple[list[str], list[str]]:
    if files and locations:
        return files, locations
    seen_files = list(files)
    seen_locations = list(locations)
    for identifier in identifiers:
        row = db.get(identifier)
        path = str(row.get("path") or "").strip()
        if path and path not in seen_files:
            seen_files.append(path)
        for match in (row.get("payload") or {}).get("matches") or []:
            if not isinstance(match, dict):
                continue
            name = str(match.get("file") or "").strip()
            line = match.get("line")
            if name and name not in seen_files:
                seen_files.append(name)
            loc = f"{name}:{line}" if name and line else name
            if loc and loc not in seen_locations:
                seen_locations.append(loc)
    return seen_files, seen_locations


def _parse_args(raw: object) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {"_malformed": True}
        return payload if isinstance(payload, dict) else {}
    return {}


def _signature(name: str, arguments: dict) -> str:
    return name + ":" + json.dumps(arguments, sort_keys=True, ensure_ascii=False)[:240]


def _final_from_text(text: str) -> dict:
    payload = prompts.extract_raw(text) or prompts.extract_json(text)
    return payload if isinstance(payload, dict) else {"summary": (text or "")[:800]}


def _ingest_sources(config: Config, client: MlxClient, ctx: agent_tools.ToolContext, sources: list[gateway.Source]) -> list[str]:
    notes: list[str] = []
    for source in sources:
        if source.scheme == "image":
            result = agent_tools.dispatch(ctx, "inspect_image", {"path": source.reference})
            notes.append(f"image {source.reference}: {result[:400]}")
        elif source.scheme == "log":
            result = agent_tools.dispatch(ctx, "inspect_log", {"path": source.reference})
            notes.append(f"log {source.reference}: {result[:400]}")
        elif source.scheme == "jira":
            result = agent_tools.dispatch(ctx, "fetch_issue", {"key": source.reference})
            notes.append(f"jira {source.reference}: {result[:400]}")
        elif source.scheme == "data":
            result = agent_tools.dispatch(ctx, "query_data", {"path": source.reference})
            notes.append(f"data {source.reference}: {result[:400]}")
        elif source.scheme == "docs":
            result = agent_tools.dispatch(ctx, "get_rules", {"task": source.reference})
            notes.append(f"docs: {result[:400]}")
        elif source.scheme == "confluence":
            result = agent_tools.dispatch(ctx, "fetch_page", {"page": source.reference})
            notes.append(f"confluence {source.reference}: {result[:400]}")
        elif source.scheme in {"repo", "file"} and source.reference not in {".", ""}:
            notes.append(f"focus path: {source.reference}")
    images = [item for item in sources if item.scheme == "image"]
    if len(images) >= 2:
        result = agent_tools.dispatch(
            ctx,
            "compare_images",
            {"reference": images[0].reference, "current": images[1].reference},
        )
        notes.append(f"compare: {result[:500]}")
    return notes


def _loop_with_tools(
    config: Config,
    client: MlxClient,
    ctx: agent_tools.ToolContext,
    task: str,
    seed: list[str],
    deadline: float,
) -> tuple[dict, str]:
    messages = [
        {"role": "system", "content": prompts.SYSTEM_AGENT},
        {
            "role": "user",
            "content": (
                f"Task:\n{task}\n\n"
                + ("\n".join(seed) + "\n\n" if seed else "")
                + "Use tools. When done, "
                + prompts.JSON_TASK
            ),
        },
    ]
    signatures: list[str] = []
    file_reads: Counter[str] = Counter()
    steps = 0
    calls = 0
    stop_reason = ""
    stalled = 0
    consecutive_errors = 0
    while steps < config.max_steps and calls < config.max_tool_calls and time.monotonic() < deadline:
        steps += 1
        remaining = max(15, int(deadline - time.monotonic()))
        try:
            completion = client.complete_chat(
                messages,
                tools=agent_tools.SCHEMAS,
                max_tokens=min(config.output_budget, config.max_completion_tokens),
                timeout=min(config.timeout, remaining),
            )
        except (MlxError, TypeError, AttributeError) as error:
            try:
                completion = client.complete(
                    messages[-1]["content"] if isinstance(messages[-1]["content"], str) else task,
                    prompts.SYSTEM_AGENT,
                    max_tokens=min(config.output_budget, config.max_completion_tokens),
                )
            except (MlxError, TypeError, AttributeError):
                if steps == 1:
                    return {
                        "status": "needs_claude",
                        "summary": f"local LLM unavailable: {error}",
                        "questions": ["retry when mlx-serve is up"],
                        "confidence": "LOW",
                    }, "llm unavailable"
                raise
        ctx.llm_in += int(completion.prompt_tokens or 0)
        ctx.llm_out += int(completion.completion_tokens or 0)
        tool_calls = completion.tool_calls or []
        if not tool_calls:
            return _final_from_text(completion.text), stop_reason or "final"
        assistant = completion.raw_message or {
            "role": "assistant",
            "content": completion.text or None,
            "tool_calls": tool_calls,
        }
        messages.append(assistant)
        before_evidence = len(ctx.evidence_ids)
        for call in tool_calls:
            calls += 1
            function = call.get("function") or {}
            name = str(function.get("name") or call.get("name") or "")
            arguments = _parse_args(function.get("arguments") or call.get("arguments"))
            if arguments.get("_malformed"):
                ctx.errors += 1
                consecutive_errors += 1
                result = json.dumps({"error": "malformed tool arguments"})
            elif not name:
                ctx.errors += 1
                consecutive_errors += 1
                result = json.dumps({"error": "missing tool name"})
            else:
                sig = _signature(name, arguments)
                signatures.append(sig)
                if signatures.count(sig) >= DENIED_REPEAT:
                    stop_reason = "repeated tool call"
                    return _final_from_text(completion.text or json.dumps({"status": "needs_claude", "summary": stop_reason})), stop_reason
                if name in {"read_file", "read_lines"}:
                    path = str(arguments.get("path") or "")
                    file_reads[path] += 1
                    if file_reads[path] >= 4:
                        stop_reason = f"repeated read of {path}"
                        return {"status": "needs_claude", "summary": stop_reason, "questions": [stop_reason]}, stop_reason
                result = agent_tools.dispatch(ctx, name, arguments)
                if '"error"' in result[:120]:
                    consecutive_errors += 1
                else:
                    consecutive_errors = 0
            if consecutive_errors > config.max_retries:
                stop_reason = "repeated tool errors"
                return {"status": "needs_claude", "summary": stop_reason, "questions": [stop_reason]}, stop_reason
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id") or name or "unknown",
                    "content": result,
                }
            )
        if len(ctx.evidence_ids) == before_evidence:
            stalled += 1
            if stalled >= STALL_STEPS:
                stop_reason = "no new evidence"
                return {"status": "needs_claude", "summary": stop_reason, "questions": [stop_reason]}, stop_reason
        else:
            stalled = 0
        if len(json.dumps(messages, ensure_ascii=False)) > config.local_context_budget:
            messages = [messages[0], messages[1], *messages[-6:]]
    return {"status": "needs_claude", "summary": "step or runtime budget exhausted", "questions": ["continue from evidence ids"]}, stop_reason or "budget"


def _fallback_search(config: Config, client: MlxClient, ctx: agent_tools.ToolContext, task: str, path: str | None) -> dict:
    report = tasks.search(config, client, task, path)
    ctx.source_chars += int(report.stats.get("source_caracteres") or 0)
    ctx.found = bool(report.locations or report.files)
    ctx.remember("code", source="search", summary=report.summary[:300], payload={"locations": report.locations[:12]})
    return {
        "status": "success",
        "summary": report.summary,
        "findings": report.findings,
        "root_cause": "",
        "changes": report.changes,
        "questions": report.next_actions[:4],
        "confidence": 0.6,
        "locations": report.locations,
        "files": report.files,
        "risks": report.risks,
    }


def run_task(
    config: Config,
    client: MlxClient,
    task: str,
    *,
    sources: list | None = None,
    path: str | None = None,
    autonomy: str | None = None,
    output_budget: int | None = None,
    local_context_budget: int | None = None,
    risk_level: str | None = None,
    store_path=None,
) -> Report:
    if not str(task or "").strip():
        raise ValueError("task is required")
    level = risk.normalize_autonomy(autonomy or config.autonomy)
    parsed = gateway.parse_sources(sources)
    if path:
        parsed = [gateway.Source("repo", path, f"repo://{path}"), *parsed]
    if any(item.scheme == "image" for item in parsed):
        config = replace(config, max_output_tokens=max(config.max_output_tokens, IMAGE_OUTPUT_TOKENS))
    focus = next((item.reference for item in parsed if item.scheme in {"repo", "file"} and item.reference not in {".", ""}), None)
    db = Store(store_path) if store_path is not None else Store()
    model = ""
    try:
        model = client.resolve_model()
    except Exception:
        model = config.model
    task_id = db.create_task(task, level, model)
    ctx = agent_tools.ToolContext(config, client, db, level, task_id)
    started = time.monotonic()
    deadline = started + max(20, config.max_runtime)
    if output_budget or local_context_budget:
        config = replace(
            config,
            output_budget=int(output_budget or config.output_budget),
            max_completion_tokens=min(config.max_completion_tokens, int(output_budget or config.output_budget)),
            local_context_budget=int(local_context_budget or config.local_context_budget),
        )
        ctx.config = config

    declared_risk = (risk_level or "").upper()
    computed_risk = risk.task_risk(task)
    if declared_risk == "HIGH" or computed_risk == "HIGH":
        computed_risk = "HIGH"
    elif declared_risk == "MEDIUM" and computed_risk != "HIGH":
        computed_risk = "MEDIUM"

    seed = _ingest_sources(config, client, ctx, parsed)
    stop_reason = ""
    caps = {}
    try:
        caps = client.capabilities() if hasattr(client, "capabilities") else {}
    except Exception:
        caps = {}
    try:
        if caps.get("tool_use") is False:
            payload = _fallback_search(config, client, ctx, task, focus)
        else:
            payload, stop_reason = _loop_with_tools(config, client, ctx, task, seed, deadline)
    except (MlxError, TypeError, AttributeError):
        try:
            payload = _fallback_search(config, client, ctx, task, focus)
        except (MlxError, TypeError, AttributeError, GuardrailError, OSError) as error:
            payload = {
                "status": "needs_claude",
                "summary": f"local LLM unavailable: {error}",
                "questions": ["retry when mlx-serve is up"],
                "confidence": "LOW",
            }
            stop_reason = "llm unavailable"

    tests = ctx.tests_status
    parsed_conf = risk.parse_confidence(payload.get("confidence"))
    if parsed_conf is not None:
        confidence = min(0.95, max(0.2, parsed_conf))
    else:
        confidence = risk.score_confidence(
            found=ctx.found,
            tests=tests,
            risk=computed_risk,
            loop_stopped=bool(stop_reason),
            tool_errors=ctx.errors,
            steps=len(ctx.evidence_ids),
        )
    band = risk.confidence_band(confidence)
    escalate = risk.needs_claude(confidence, computed_risk, config.confidence_threshold, tests)
    status = str(payload.get("status") or "success")
    if escalate:
        status = "needs_claude"
    questions = [str(item) for item in (payload.get("questions") or []) if str(item).strip()]
    if escalate and not questions:
        questions = [
            f"Escalated: risk={computed_risk}, confidence={band} ({confidence}, heuristic, not a probability).",
        ]
        if stop_reason:
            questions.append(stop_reason)
    db.finish_task(task_id, status=status, confidence=confidence, risk=computed_risk, payload=payload)
    elapsed = round(time.monotonic() - started, 1)
    findings = [str(item) for item in (payload.get("findings") or []) if str(item)]
    root = str(payload.get("root_cause") or "").strip()
    if root:
        findings = [f"Root cause: {root}"] + findings
    summary = str(payload.get("summary") or "").strip() or "Local task finished."
    root_line = f"ROOT CAUSE: {root}" if root else "ROOT CAUSE: (none stated)"
    summary = (
        f"STATUS: {status}\n"
        f"CONFIDENCE: {band} ({confidence}, heuristic)\n"
        f"RISK: {computed_risk}\n"
        f"TESTS: {tests or 'n/a'}\n"
        f"AUTONOMY: {level}\n\n"
        f"{summary}\n\n"
        f"{root_line}"
    )
    evidence = [_packet_item(db, item) for item in ctx.evidence_ids]
    files, locations = _backfill_hits(
        db,
        ctx.evidence_ids,
        [str(item) for item in (payload.get("files") or [])],
        [str(item) for item in (payload.get("locations") or [])],
    )
    report = Report(
        title="Local task",
        summary=summary,
        findings=findings[:12],
        files=files[:20],
        locations=locations[:20],
        risks=[computed_risk] + [str(item) for item in (payload.get("risks") or [])][:6],
        changes=[str(item) for item in (payload.get("changes") or [])][:12],
        evidence=evidence,
        next_actions=questions[:8] or ["Inspect an evidence id with local_expand if you need the raw excerpt."],
        stats={
            "status": status,
            "confidence": confidence,
            "confidence_band": band,
            "risk": computed_risk,
            "tests": tests or "n/a",
            "autonomy": level,
            "task_id": task_id,
            "steps_evidence": len(ctx.evidence_ids),
            "latency_s": elapsed,
            "source_caracteres": max(ctx.source_chars, ctx.llm_in * 4),
            "local_llm_in": ctx.llm_in,
            "local_llm_out": ctx.llm_out,
            "stop": stop_reason or "ok",
        },
        errors=[],
        details="",
    )
    if status == "needs_claude":
        report.risks = [f"NEEDS_CLAUDE: {computed_risk} / {band} ({confidence})"] + report.risks
    visible = len(summary) + sum(len(item) for item in findings)
    raw_tokens = max(ctx.source_chars // 4, ctx.llm_in)
    visible_tokens = visible // 4
    db.record_metric(
        tool="local_task",
        source_type=parsed[0].scheme if parsed else "repo",
        raw_tokens=raw_tokens,
        visible_tokens=visible_tokens,
        avoided_tokens=max(0, raw_tokens - visible_tokens),
        local_llm_in=ctx.llm_in,
        local_llm_out=ctx.llm_out,
        tool_calls=len(ctx.evidence_ids),
        latency_s=elapsed,
        escalated=1 if status == "needs_claude" else 0,
        model=model,
        status=status,
        cache_hit=ctx.cache_hits,
    )
    return report
