"""Boucle agentique locale : le brut ne quitte pas la machine, Claude recoit le paquet."""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path

from . import agent_tools, extract, gateway, prompts, risk, router, tasks
from .config import Config
from .files import GuardrailError, relative_to_root
from .mlx import MlxClient, MlxError
from .ocr import IMAGE_OUTPUT_TOKENS
from .report import Report
from .store import Store, write_trace

DENIED_REPEAT = 3
STALL_STEPS = 3


def infer_why(sources: list, task: str) -> str:
    schemes = {getattr(item, "scheme", "") for item in sources}
    blob = (task or "").lower()
    if "image" in schemes:
        return "screenshot"
    if "log" in schemes:
        return "large_log"
    if "jira" in schemes or "confluence" in schemes:
        return "ticket_or_docs"
    if "test" in blob or "phpunit" in blob or "pytest" in blob:
        return "failing_tests"
    if "fix" in blob or "patch" in blob:
        return "patch"
    return "repo_exploration"


def _packet_item(db: Store, identifier: str) -> dict:
    row = db.get(identifier)
    payload = row.get("payload") or {}
    preview = str(row.get("summary") or row.get("source") or "").strip()
    if payload.get("high_signal"):
        extra = str(
            payload.get("example")
            or payload.get("content")
            or payload.get("failure")
            or payload.get("stack")
            or ""
        ).strip()
        if extra and extra not in preview:
            preview = f"{preview}\n{extra}" if preview else extra
    matches = payload.get("matches") or []
    if matches and isinstance(matches[0], dict):
        first = matches[0]
        loc = f"{first.get('file')}:{first.get('line')}"
        text = str(first.get("text") or "").strip()
        extra = f"{loc} {text}".strip()
        if extra and extra not in preview:
            preview = f"{preview}; {extra}" if preview else extra
    item = {
        "id": identifier,
        "type": row.get("type") or "evidence",
        "content": preview[:400] if payload.get("high_signal") else preview[:220],
    }
    source = str(row.get("source") or "").strip()
    if source:
        item["source"] = source
    confidence = row.get("confidence")
    if isinstance(confidence, (int, float)):
        item["confidence"] = confidence
    return item


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
    previous = ctx.config
    ctx.config = replace(previous, vision=False)
    try:
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
    finally:
        ctx.config = previous
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
                + "Call a tool if you still need evidence. Do not repeat a tool already executed in this prompt. When done, "
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
        llm_started = time.monotonic()
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
        ctx.llm_ms += time.monotonic() - llm_started
        ctx.llm_in += int(completion.prompt_tokens or 0)
        ctx.llm_out += int(completion.completion_tokens or 0)
        ctx.llm_calls += 1
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
                if signatures.count(sig) > 1:
                    ctx.redundant_calls += 1
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
            ctx.zero_evidence_calls += 1
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


def _packet_hit(config: Config, raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    if not text.startswith("/"):
        return text
    path, span = text, ""
    if ":" in text:
        left, right = text.rsplit(":", 1)
        if right.replace("-", "").isdigit():
            path, span = left, ":" + right
    return relative_to_root(config, Path(path)) + span


def _packet_path(ctx: agent_tools.ToolContext, raw: str) -> str:
    return _packet_hit(ctx.config, raw)


def _facts_from_evidence(ctx: agent_tools.ToolContext) -> tuple[list[str], list[str], list[str], str]:
    findings: list[str] = []
    files: list[str] = []
    locations: list[str] = []
    for identifier in ctx.evidence_ids:
        row = ctx.db.get(identifier)
        payload = row.get("payload") or {}
        summary = str(row.get("summary") or "").strip()
        findings.append(f"{identifier}: {summary[:180]}")
        path = _packet_path(ctx, str(row.get("path") or ""))
        if path and path not in files:
            files.append(path)
        span = str(row.get("lines") or "").strip()
        if path and span:
            loc = f"{path}:{span}"
            if loc not in locations:
                locations.append(loc)
        for match in payload.get("matches") or []:
            if not isinstance(match, dict):
                continue
            name = _packet_path(ctx, str(match.get("file") or ""))
            line = match.get("line")
            if name and name not in files:
                files.append(name)
            loc = f"{name}:{line}" if name and line else name
            if loc and loc not in locations:
                locations.append(loc)
    return findings, files, locations, _root_from_evidence(ctx)


def _root_from_evidence(ctx: agent_tools.ToolContext) -> str:
    best = ""
    best_score = -1
    for identifier in ctx.evidence_ids:
        row = ctx.db.get(identifier)
        payload = row.get("payload") or {}
        if not payload.get("high_signal"):
            continue
        example = str(payload.get("example") or payload.get("failure") or "").strip()
        content = str(payload.get("content") or "").strip()
        summary = str(row.get("summary") or "").strip()
        if "high-signal /" in summary and not example:
            continue
        blob = example or content or summary
        if not blob:
            continue
        score = 0
        if extract.KEEP_SIGNATURE.search(blob):
            score += 8
        if "ERROR" in blob or "Exception" in blob or "TypeError" in blob:
            score += 2
        if payload.get("occurrence") == "last":
            score += 1
        if score > best_score:
            best_score = score
            best = blob
    return best[:240]


def _merge_payload(payload: dict, ctx: agent_tools.ToolContext) -> dict:
    facts, files, locations, root = _facts_from_evidence(ctx)
    merged = dict(payload)
    existing = [str(item) for item in (payload.get("findings") or []) if str(item)]
    seen = set(existing)
    for fact in facts:
        if fact not in seen:
            existing.append(fact)
            seen.add(fact)
    merged["findings"] = existing
    if not merged.get("files"):
        merged["files"] = files
    if not merged.get("locations"):
        merged["locations"] = locations
    if not str(merged.get("root_cause") or "").strip():
        merged["root_cause"] = root
    return merged


def _evidence_digest(ctx: agent_tools.ToolContext, budget: int = 12_000) -> str:
    parts = [f"Evidence ids (runtime, must stay in the packet): {', '.join(ctx.evidence_ids) or '(none)'}"]
    for identifier in ctx.evidence_ids:
        row = ctx.db.get(identifier)
        payload = row.get("payload") or {}
        chunk = f"\n{identifier} ({row.get('type')} {row.get('lines') or ''})\n{row.get('summary')}\n"
        extra = payload.get("content") or payload.get("failure") or payload.get("stack")
        if extra:
            chunk += str(extra)[:800] + "\n"
        for signature in payload.get("signatures") or []:
            if isinstance(signature, dict):
                chunk += f"  {signature.get('count')}x {signature.get('signature')}\n"
        parts.append(chunk)
        if sum(len(item) for item in parts) >= budget:
            break
    return "".join(parts)[:budget]


def _extract_suffices(ctx: agent_tools.ToolContext) -> bool:
    """Verbatim high-signal already answers; a 9B sentence would only rephrase it."""
    for identifier in ctx.evidence_ids:
        payload = (ctx.db.get(identifier).get("payload") or {})
        if payload.get("high_signal"):
            return True
    return False


def _already_ran(ctx: agent_tools.ToolContext, name: str) -> bool:
    return any(item.get("tool") == name for item in ctx.trace)


def _tool_payload(result: str) -> dict:
    try:
        payload = json.loads(result)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _run_direct(ctx: agent_tools.ToolContext, task: str, decision: router.RouteDecision) -> dict:
    ctx.config = replace(ctx.config, vision=False)
    started = time.monotonic()
    tool_payload: dict = {}
    if decision.first_tool == "search_repo" and decision.symbols:
        focus = str(decision.first_args.get("path") or ".")
        for symbol in decision.symbols[:3]:
            tool_payload = _tool_payload(agent_tools.dispatch(ctx, "search_repo", {"pattern": symbol, "path": focus}))
    elif decision.first_tool:
        result = agent_tools.dispatch(ctx, decision.first_tool, decision.first_args or {})
        tool_payload = _tool_payload(result)
        if not ctx.evidence_ids:
            ctx.remember(
                "code",
                source=decision.first_tool,
                summary=str(result)[:300],
                payload={"tool": decision.first_tool, "raw": str(result)[:1500]},
            )
    ctx.preprocess_ms += time.monotonic() - started
    findings, files, locations, root = _facts_from_evidence(ctx)
    tool_findings = [str(item) for item in (tool_payload.get("findings") or []) if str(item)]
    tool_summary = str(
        tool_payload.get("summary") or tool_payload.get("goal") or tool_payload.get("title") or ""
    ).strip()
    return {
        "status": "success",
        "summary": tool_summary or (locations[0] if locations else (findings[0] if findings else f"DIRECT: {decision.reason}")),
        "findings": tool_findings[:8] or locations[:8] or findings[:6],
        "files": files[:12],
        "locations": locations[:12],
        "root_cause": root,
        "questions": [],
        "confidence": "HIGH" if ctx.found or ctx.evidence_ids else "LOW",
    }


def _run_reduce(
    config: Config,
    client: MlxClient,
    ctx: agent_tools.ToolContext,
    task: str,
    sources: list[gateway.Source],
    decision: router.RouteDecision,
    deadline: float,
) -> dict:
    started = time.monotonic()
    _ingest_sources(config, client, ctx, sources)
    if decision.first_tool and not _already_ran(ctx, decision.first_tool):
        agent_tools.dispatch(ctx, decision.first_tool, decision.first_args or {})
    ctx.preprocess_ms += time.monotonic() - started
    digest = _evidence_digest(ctx)
    if _extract_suffices(ctx):
        findings, files, locations, root = _facts_from_evidence(ctx)
        payload = {
            "status": "success",
            "summary": root or (findings[0] if findings else "High-signal extract, no local LLM."),
            "findings": findings[:12],
            "files": files,
            "locations": locations,
            "root_cause": root,
            "confidence": "HIGH",
            "questions": [],
        }
        return _merge_payload(payload, ctx)
    remaining = max(15, int(deadline - time.monotonic()))
    try:
        llm_started = time.monotonic()
        completion = client.complete(
            f"Task:\n{task}\n\nHigh-signal evidence (verbatim, do not drop these ids):\n{digest}\n\n{prompts.JSON_TASK}",
            prompts.SYSTEM_ANALYST,
            max_tokens=min(config.output_budget, config.max_completion_tokens),
        )
        ctx.llm_ms += time.monotonic() - llm_started
        ctx.llm_in += int(completion.prompt_tokens or 0)
        ctx.llm_out += int(completion.completion_tokens or 0)
        ctx.llm_calls += 1
        payload = _final_from_text(completion.text)
    except (MlxError, TypeError, AttributeError):
        findings, files, locations, root = _facts_from_evidence(ctx)
        payload = {
            "status": "success",
            "summary": "Deterministic high-signal only; local LLM unavailable for synthesis.",
            "findings": findings[:12],
            "files": files,
            "locations": locations,
            "root_cause": root,
            "confidence": "MEDIUM",
            "questions": ["optional synthesis skipped"],
        }
    return _merge_payload(payload, ctx)


def _run_agent(
    config: Config,
    client: MlxClient,
    ctx: agent_tools.ToolContext,
    task: str,
    sources: list[gateway.Source],
    decision: router.RouteDecision,
    focus: str | None,
    deadline: float,
) -> tuple[dict, str]:
    started = time.monotonic()
    seed = _ingest_sources(config, client, ctx, sources)
    if decision.first_tool and not _already_ran(ctx, decision.first_tool):
        result = agent_tools.dispatch(ctx, decision.first_tool, decision.first_args or {})
        seed.append(
            f"Already executed {decision.first_tool} {json.dumps(decision.first_args, ensure_ascii=False)}:\n"
            f"{result[:800]}\nDo not repeat this call."
        )
    ctx.preprocess_ms += time.monotonic() - started
    caps = {}
    try:
        caps = client.capabilities() if hasattr(client, "capabilities") else {}
    except Exception:
        caps = {}
    if caps.get("tool_use") is False:
        return _fallback_search(config, client, ctx, task, focus), "no tool use"
    payload, stop_reason = _loop_with_tools(config, client, ctx, task, seed, deadline)
    return _merge_payload(payload, ctx), stop_reason


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
    trace: bool = False,
    why: str | None = None,
) -> Report:
    if not str(task or "").strip():
        raise ValueError("task is required")
    level = risk.normalize_autonomy(autonomy or config.autonomy)
    parsed = gateway.parse_sources(sources)
    if path:
        parsed = [gateway.Source("repo", path, f"repo://{path}"), *parsed]
    reason = why or infer_why(parsed, task)
    if any(item.scheme == "image" for item in parsed):
        config = replace(config, max_output_tokens=max(config.max_output_tokens, IMAGE_OUTPUT_TOKENS))
    focus = next((item.reference for item in parsed if item.scheme in {"repo", "file"} and item.reference not in {".", ""}), None)
    started = time.monotonic()
    deadline = started + max(20, config.max_runtime)
    if output_budget or local_context_budget:
        config = replace(
            config,
            output_budget=int(output_budget or config.output_budget),
            max_completion_tokens=min(config.max_completion_tokens, int(output_budget or config.output_budget)),
            local_context_budget=int(local_context_budget or config.local_context_budget),
        )

    declared_risk = (risk_level or "").upper()
    computed_risk = risk.task_risk(task)
    if declared_risk == "HIGH" or computed_risk == "HIGH":
        computed_risk = "HIGH"
    elif declared_risk == "MEDIUM" and computed_risk != "HIGH":
        computed_risk = "MEDIUM"

    routed_at = time.monotonic()
    decision = router.route_task(config, task, parsed, autonomy=level, risk=computed_risk)
    routing_ms = time.monotonic() - routed_at

    db = Store(store_path) if store_path is not None else Store()
    model = config.model
    if decision.tier not in {"direct", "claude"}:
        try:
            model = client.resolve_model()
        except Exception:
            model = config.model
    task_id = db.create_task(task, level, model)
    ctx = agent_tools.ToolContext(config, client, db, level, task_id)
    ctx.routing_ms = routing_ms
    stop_reason = ""
    payload: dict
    try:
        if decision.tier == "claude":
            payload = {
                "status": "needs_claude",
                "summary": decision.reason,
                "questions": [decision.reason],
                "confidence": "LOW",
            }
        elif decision.tier == "direct":
            payload = _run_direct(ctx, task, decision)
        elif decision.tier == "reduce":
            payload = _run_reduce(config, client, ctx, task, parsed, decision, deadline)
        else:
            payload, stop_reason = _run_agent(config, client, ctx, task, parsed, decision, focus, deadline)
    except (MlxError, TypeError, AttributeError):
        if decision.tier == "direct":
            raise
        try:
            payload = _merge_payload(_fallback_search(config, client, ctx, task, focus), ctx)
        except (MlxError, TypeError, AttributeError, GuardrailError, OSError) as error:
            payload = _merge_payload(
                {
                    "status": "needs_claude",
                    "summary": f"local LLM unavailable: {error}",
                    "questions": ["retry when mlx-serve is up"],
                    "confidence": "LOW",
                },
                ctx,
            )
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
    db.finish_task(
        task_id,
        status=status,
        confidence=confidence,
        risk=computed_risk,
        payload={**payload, "tier": decision.tier, "routing_reason": decision.reason},
    )
    elapsed = round(time.monotonic() - started, 1)
    findings = [str(item) for item in (payload.get("findings") or []) if str(item)]
    root = str(payload.get("root_cause") or "").strip()
    if root:
        findings = [f"Root cause: {root}"] + findings
    body = str(payload.get("summary") or "").strip() or "Local task finished."
    root_line = f"ROOT CAUSE: {root}" if root else "ROOT CAUSE: (none stated)"
    files, locations = _backfill_hits(
        db,
        ctx.evidence_ids,
        [str(item) for item in (payload.get("files") or [])],
        [str(item) for item in (payload.get("locations") or [])],
    )
    files = [_packet_hit(config, item) for item in files if item]
    locations = [_packet_hit(config, item) for item in locations if item]
    if decision.tier == "direct":
        extra = f"\nTESTS: {tests}" if tests else ""
        summary = (
            f"STATUS: {status}\nTIER: direct{extra}\n"
            f"CONFIDENCE: {band} ({confidence}, heuristic)"
        )
        if not locations and body and body not in summary:
            summary += "\n" + body[:400]
    else:
        summary = (
            f"STATUS: {status}\n"
            f"CONFIDENCE: {band} ({confidence}, heuristic)\n"
            f"RISK: {computed_risk}\n"
            f"TESTS: {tests or 'n/a'}\n"
            f"AUTONOMY: {level}\n"
            f"TIER: {decision.tier}\n"
            f"WHY LOCAL: {reason}\n\n"
            f"{body}\n\n"
            f"{root_line}"
        )
    evidence = [_packet_item(db, item) for item in ctx.evidence_ids]
    artifacts = dict(ctx.artifacts)
    timings = {
        "routing_s": round(ctx.routing_ms, 4),
        "preprocess_s": round(ctx.preprocess_ms, 3),
        "local_llm_s": round(ctx.llm_ms, 2),
        "tools_s": round(ctx.tool_ms, 2),
        "total_s": elapsed,
        "other_s": round(max(0.0, elapsed - ctx.llm_ms - ctx.tool_ms - ctx.routing_ms - ctx.preprocess_ms), 2),
    }
    trace_path = ""
    if trace or os.environ.get("LOCAL_AGENT_TRACE") == "1":
        stored = write_trace(
            task_id,
            {
                "task": task,
                "why": reason,
                "tier": decision.tier,
                "routing_reason": decision.reason,
                "status": status,
                "steps": ctx.trace,
                "timings": timings,
                "artifacts": artifacts,
            },
        )
        trace_path = str(stored)
    useful = len(ctx.evidence_ids)
    total_calls = max(1, ctx.tool_calls)
    avoidable = 1 if decision.tier == "direct" and ctx.llm_calls else 0
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
        artifacts=artifacts,
        stats={
            "status": status,
            "confidence": confidence,
            "confidence_band": band,
            "risk": computed_risk,
            "tests": tests or "n/a",
            "autonomy": level,
            "task_id": task_id,
            "why": reason,
            "tier": decision.tier,
            "routing_reason": decision.reason,
            "steps_evidence": len(ctx.evidence_ids),
            "tool_calls": ctx.tool_calls,
            "tool_efficiency": round(useful / total_calls, 3),
            "redundant_tool_calls": ctx.redundant_calls,
            "zero_evidence_calls": ctx.zero_evidence_calls,
            "local_llm_calls": ctx.llm_calls,
            "avoidable_local_llm_calls": avoidable,
            "cache_hits": ctx.cache_hits,
            "latency_s": elapsed,
            "timings": timings,
            "trace_path": trace_path,
            "source_caracteres": max(ctx.source_chars, ctx.llm_in * 4),
            "local_llm_in": ctx.llm_in,
            "local_llm_out": ctx.llm_out,
            "stop": stop_reason or "ok",
        },
        errors=[],
        details="",
    )
    if decision.tier == "direct":
        report.title = ""
        report.next_actions = []
        report.risks = [] if computed_risk == "LOW" and status != "needs_claude" else report.risks
        if locations:
            report.findings = []
            loc_files = []
            for loc in locations:
                name = str(loc).split(":")[0]
                if name and name not in loc_files:
                    loc_files.append(name)
            if files and all(name in loc_files for name in files):
                report.files = []
        report.stats = {
            "tier": decision.tier,
            "local_llm_calls": ctx.llm_calls,
            "avoidable_local_llm_calls": avoidable,
            "latency_s": elapsed,
        }
        for item in report.evidence:
            item["content"] = str(item.get("content") or "")[:220]
    if status == "needs_claude":
        report.risks = [f"NEEDS_CLAUDE: {computed_risk} / {band} ({confidence})"] + report.risks
    visible = len(summary) + sum(len(item) for item in findings) + sum(len(str(item.get("content") or "")) for item in evidence)
    raw_tokens = max(ctx.source_chars // 4, ctx.llm_in, decision.estimated_raw_tokens)
    visible_tokens = visible // 4
    db.record_metric(
        tool="local_task",
        source_type=parsed[0].scheme if parsed else "repo",
        raw_tokens=raw_tokens,
        visible_tokens=visible_tokens,
        avoided_tokens=max(0, raw_tokens - visible_tokens),
        local_llm_in=ctx.llm_in,
        local_llm_out=ctx.llm_out,
        tool_calls=ctx.tool_calls,
        latency_s=elapsed,
        escalated=1 if status == "needs_claude" else 0,
        model=model,
        status=status,
        cache_hit=ctx.cache_hits,
        tier=decision.tier,
        routing_reason=decision.reason,
        local_llm_calls=ctx.llm_calls,
        avoidable_llm=avoidable,
    )
    return report
