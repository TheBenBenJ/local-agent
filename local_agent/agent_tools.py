"""Outils deterministes du modele local : rg, git, fichiers, checks, OCR. Pas de shell libre."""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import edit, extract, files, ocr, shell, tasks, vision
from .compare import compare_images
from .config import Config
from .files import GuardrailError
from .providers import confluence as confluence_provider
from .providers import data as data_provider
from .providers import jira as jira_provider
from .providers import rules as rules_provider
from .risk import AUTO, PATCH, READ_ONLY
from .store import Store, sha256_bytes, sha256_file

MAX_TOOL_CHARS = 8_000

SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_repo",
            "description": "ripgrep over the repository. Prefer an identifier pattern over a vague phrase.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "description": "Relative path, default repo root"},
                    "globs": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file under the repo root. Secrets and binaries are refused.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start": {"type": "integer", "description": "1-based start line"},
                    "end": {"type": "integer", "description": "1-based end line inclusive"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List analysable files under a path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "globs": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_status",
            "description": "Working tree status: branch, modified, untracked.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": "Unified diff. scope: worktree, staged or branch.",
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "enum": ["worktree", "staged", "branch"]},
                    "base": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_check",
            "description": "Whitelisted project check (tests, lint, types). No write commands.",
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string"},
                    "target": {"type": "string"},
                    "filter": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_log",
            "description": "Filter a log with ripgrep patterns. Deterministic, no LLM.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "patterns": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_image",
            "description": "OCR a screenshot locally. Pixels stay on disk. Returns evidence ids.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "task": {"type": "string"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compare_images",
            "description": "Compare two screenshots: hash, size, OCR text, then a 24x24 pixel grid diff.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reference": {"type": "string"},
                    "current": {"type": "string"},
                },
                "required": ["reference", "current"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_rules",
            "description": "Return ORIGINAL project-rule excerpts that match the task. Never paraphrase.",
            "parameters": {
                "type": "object",
                "properties": {"task": {"type": "string"}, "files": {"type": "array", "items": {"type": "string"}}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_data",
            "description": "Aggregate a csv/json/sqlite file. Deterministic stats, not a dump.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "query": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_issue",
            "description": "Fetch a Jira issue if credentials are configured. Otherwise explains how.",
            "parameters": {
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_page",
            "description": "Fetch a Confluence Cloud page by id or SPACE/Title. Same Atlassian credentials as Jira.",
            "parameters": {
                "type": "object",
                "properties": {"page": {"type": "string"}},
                "required": ["page"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_patch",
            "description": "Propose a mechanical patch (local_fix propose). Requires autonomy patch or auto.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "task": {"type": "string"}},
                "required": ["path", "task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_patch",
            "description": "Apply a previously proposed patch_id. Requires autonomy auto.",
            "parameters": {
                "type": "object",
                "properties": {"patch_id": {"type": "string"}},
                "required": ["patch_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crop_image",
            "description": "Crop one OCR region by id (a832b1c4-R1). Pixels stay on disk.",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_evidence",
            "description": "Fetch a stored evidence item by id (CODE-E12, IMG-E4, LOG-E2).",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_diff",
            "description": "Review a git diff (worktree, staged or branch) without dumping it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "enum": ["worktree", "staged", "branch"]},
                    "base": {"type": "string"},
                    "task": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "visual_reason",
            "description": "Optional VLM pass on one image plus its OCR. No-op if the loaded model has no vision.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "task": {"type": "string"},
                    "ocr_text": {"type": "string"},
                },
                "required": ["path"],
            },
        },
    },
]


def _clip(text: str, budget: int = MAX_TOOL_CHARS) -> str:
    text = text.strip()
    if len(text) <= budget:
        return text
    return text[:budget].rstrip() + f"\n[truncated at {budget} chars]"


def _json(payload: object) -> str:
    return _clip(json.dumps(payload, ensure_ascii=False, indent=2))


class ToolContext:
    def __init__(self, config: Config, client, db: Store, autonomy: str, task_id: int | None) -> None:
        self.config = config
        self.client = client
        self.db = db
        self.autonomy = autonomy
        self.task_id = task_id
        self.evidence_ids: list[str] = []
        self.errors = 0
        self.tests_status: str | None = None
        self.found = False
        self.source_chars = 0
        self.llm_in = 0
        self.llm_out = 0
        self.cache_hits = 0
        self.artifacts: dict[str, object] = {}
        self.trace: list[dict] = []
        self.tool_calls = 0
        self.tool_ms = 0.0
        self.llm_ms = 0.0
        self.llm_calls = 0
        self.redundant_calls = 0
        self.zero_evidence_calls = 0
        self.routing_ms = 0.0
        self.preprocess_ms = 0.0

    def remember(self, kind: str, **kwargs) -> str:
        identifier = self.db.put(kind, task_id=self.task_id, **kwargs)
        self.evidence_ids.append(identifier)
        return identifier


def dispatch(ctx: ToolContext, name: str, arguments: dict) -> str:
    started = time.monotonic()
    try:
        handler = TOOLS[name]
    except KeyError:
        ctx.errors += 1
        ctx.tool_calls += 1
        return _json({"error": f"unknown tool {name}"})
    try:
        result = handler(ctx, arguments or {})
    except (GuardrailError, ValueError, OSError) as error:
        ctx.errors += 1
        result = _json({"error": str(error)})
    elapsed_ms = round((time.monotonic() - started) * 1000)
    ctx.tool_ms += elapsed_ms / 1000
    ctx.tool_calls += 1
    hint = arguments.get("path") or arguments.get("pattern") or arguments.get("kind") or arguments.get("key") or ""
    ctx.trace.append({"tool": name, "ms": elapsed_ms, "hint": str(hint)[:80], "ok": '"error"' not in result[:80]})
    return result


def _search_repo(ctx: ToolContext, arguments: dict) -> str:
    pattern = str(arguments.get("pattern") or "").strip()
    if not pattern:
        raise ValueError("pattern is required")
    target = files.resolve_path(ctx.config, arguments.get("path"))
    matches, total = files.grep(ctx.config, target, [pattern], globs=arguments.get("globs"), max_matches=40)
    ctx.source_chars += sum(len(item.get("text") or "") for item in matches)
    ctx.found = ctx.found or bool(matches)
    head = ""
    if matches:
        first = matches[0]
        head = f"; {first.get('file')}:{first.get('line')} {str(first.get('text') or '')[:80]}"
    identifier = ctx.remember(
        "code",
        source=f"rg:{pattern}",
        summary=f"{len(matches)}/{total} matches for {pattern}{head}",
        payload={"matches": matches[:20], "total": total},
    )
    return _json({"evidence": identifier, "total": total, "matches": matches[:20]})


def _read_file(ctx: ToolContext, arguments: dict) -> str:
    path = files.resolve_path(ctx.config, str(arguments.get("path") or ""))
    if path.is_dir():
        raise GuardrailError("path is a directory, use list_files")
    digest = sha256_file(path)
    relative = files.relative_to_root(ctx.config, path)
    if ctx.config.enable_cache:
        cached = ctx.db.cached_summary(relative, digest)
        if cached:
            ctx.cache_hits += 1
            return _json({"evidence": cached["evidence_id"], "cached": True, "sha256": digest, "summary": cached["summary"]})
    text, truncated = files.read_text(path, ctx.config.max_file_size)
    start = int(arguments.get("start") or 1)
    end = int(arguments.get("end") or 0)
    lines = text.splitlines()
    if end:
        excerpt = "\n".join(f"{index}| {line}" for index, line in enumerate(lines[start - 1 : end], start=start))
        span = f"{start}-{min(end, len(lines))}"
    else:
        excerpt = "\n".join(f"{index}| {line}" for index, line in enumerate(lines, start=1))
        span = f"1-{len(lines)}"
    ctx.source_chars += len(excerpt)
    ctx.found = True
    identifier = ctx.remember(
        "code",
        source=f"repo://{relative}",
        summary=f"{relative} {span}",
        sha256=digest,
        path=relative,
        lines=span,
        payload={"truncated": truncated},
    )
    ctx.db.remember_file(relative, digest, f"{relative} {span}", identifier)
    return _json({"evidence": identifier, "sha256": digest, "lines": span, "truncated": truncated, "content": excerpt})


def _list_files(ctx: ToolContext, arguments: dict) -> str:
    target = files.resolve_path(ctx.config, arguments.get("path"))
    selected, total = files.discover_files(ctx.config, target, globs=arguments.get("globs"), max_files=80)
    names = [item.relative for item in selected]
    return _json({"shown": len(names), "total": total, "files": names})


def _git_status(ctx: ToolContext, arguments: dict) -> str:
    return _json(shell.working_tree_state(ctx.config))


def _git_diff(ctx: ToolContext, arguments: dict) -> str:
    from . import tasks as task_mod

    scope = str(arguments.get("scope") or "worktree")
    argv = task_mod._resolve_diff_args(ctx.config, scope, arguments.get("base"))
    result = shell.git(ctx.config, argv)
    ctx.source_chars += len(result.stdout)
    clipped = _clip(result.stdout, 12_000)
    structured = extract.extract_diff(result.stdout)
    identifier = ctx.remember(
        "diff",
        source=f"git:{scope}",
        summary=(
            f"diff {scope} {structured['file_count']} files "
            f"+{structured['additions']}/-{structured['deletions']}"
        ),
        sha256=sha256_bytes(clipped.encode()),
        payload={"exit": result.exit_code, "diff": clipped, **structured, "high_signal": True},
    )
    ctx.artifacts["diff_id"] = identifier
    ctx.found = ctx.found or bool(structured["file_count"] or result.stdout.strip())
    return _json({"evidence": identifier, "exit": result.exit_code, "diff": clipped, **structured})


def _run_check(ctx: ToolContext, arguments: dict) -> str:
    checks = shell.load_checks(ctx.config)
    kind = str(arguments.get("kind") or next(iter(checks), ""))
    argv, spec = shell.build_check_command(checks, kind, arguments.get("target"), arguments.get("filter"))
    result = shell.run(argv, ctx.config.repo_root, ctx.config.command_timeout)
    ctx.tests_status = "PASS" if result.exit_code == 0 else "FAIL"
    parsed = extract.extract_test_output(result.output, kind=kind)
    identifier = ctx.remember(
        "test",
        source=kind,
        summary=f"{spec.get('label') or kind} exit {result.exit_code}",
        payload={
            "exit": result.exit_code,
            "timed_out": result.timed_out,
            "output": _clip(result.output, 6_000),
            "failures": parsed.get("failures") or [],
            "high_signal": bool(parsed.get("failures")),
        },
    )
    ctx.artifacts["test_run_id"] = identifier
    ctx.found = True
    for failure in parsed.get("failures") or []:
        ctx.remember(
            "test",
            source=kind,
            summary=str(failure.get("failure") or failure.get("test") or "")[:300],
            lines=str(failure.get("lines") or ""),
            payload={**failure, "high_signal": True},
        )
    return _json(
        {
            "evidence": identifier,
            "kind": kind,
            "exit": result.exit_code,
            "timed_out": result.timed_out,
            "output": _clip(result.output, 6_000),
            "failures": parsed.get("failures") or [],
        }
    )


def _inspect_log(ctx: ToolContext, arguments: dict) -> str:
    target = files.resolve_path(ctx.config, str(arguments.get("path") or ""))
    custom = [str(item) for item in (arguments.get("patterns") or []) if str(item).strip()]
    if custom:
        matches, total = files.grep(ctx.config, target, custom, max_matches=80, ignore_case=True)
        ctx.source_chars += sum(len(item.get("text") or "") for item in matches)
        identifier = ctx.remember(
            "log",
            source=str(arguments.get("path")),
            summary=f"{len(matches)}/{total} custom-pattern lines",
            payload={"total": total, "matches": matches[:25], "high_signal": True},
        )
        ctx.found = ctx.found or bool(matches)
        return _json({"evidence": identifier, "total": total, "matches": matches[:25]})
    payload = extract.extract_log(target)
    ctx.source_chars += int(payload.get("bytes") or 0)
    ctx.found = ctx.found or bool(payload.get("hits"))
    digest = sha256_file(target) if target.is_file() else ""
    identifier = ctx.remember(
        "log",
        source=str(arguments.get("path")),
        summary=f"{payload.get('hits') or 0} high-signal / {payload.get('unique_signatures') or 0} signatures",
        sha256=digest,
        path=str(target),
        payload={
            "total": payload.get("hits"),
            "signatures": payload.get("signatures") or [],
            "first_hit": payload.get("first_hit"),
            "last_hit": payload.get("last_hit"),
            "high_signal": True,
        },
    )
    excerpt_ids: list[str] = []
    for excerpt in payload.get("excerpts") or []:
        excerpt_ids.append(
            ctx.remember(
                "log",
                source=str(arguments.get("path")),
                summary=(excerpt.get("content") or excerpt.get("signature") or "")[:300],
                sha256=digest,
                path=str(target),
                lines=str(excerpt.get("lines") or ""),
                payload={**excerpt, "high_signal": True},
            )
        )
    return _json(
        {
            "evidence": identifier,
            "total": payload.get("hits"),
            "signatures": payload.get("signatures") or [],
            "excerpt_ids": excerpt_ids,
        }
    )


def _inspect_image(ctx: ToolContext, arguments: dict) -> str:
    report = ocr.read_images(ctx.config, str(arguments.get("path")), None, arguments.get("task"), client=ctx.client)
    ctx.source_chars += int(report.stats.get("source_caracteres") or 0)
    path = str(arguments.get("path") or "")
    digest = ""
    candidate = Path(path).expanduser()
    if candidate.is_file():
        digest = sha256_file(candidate)
    identifier = ctx.remember(
        "image",
        source=path,
        summary=report.summary[:300],
        sha256=digest,
        path=path,
        payload={"findings": report.findings[:8], "regions": [item.get("id") for item in (report.evidence or [])[:8]]},
    )
    region_ids = []
    for item in (report.evidence or [])[:12]:
        region_ids.append(
            ctx.remember(
                "image",
                source=str(item.get("id") or path),
                summary=str(item.get("content") or item.get("id") or "")[:300],
                sha256=digest,
                path=path,
                payload={**item, "region": item.get("id")},
            )
        )
    return _json(
        {
            "evidence": identifier,
            "regions": region_ids,
            "summary": report.summary,
            "findings": report.findings[:12],
            "vision": report.stats.get("vision"),
        }
    )


def _compare(ctx: ToolContext, arguments: dict) -> str:
    report = compare_images(ctx.config, str(arguments["reference"]), str(arguments["current"]), client=ctx.client)
    ctx.source_chars += int(report.stats.get("source_caracteres") or 0)
    verdict = (
        f"SHA256 {report.stats.get('sha_left')} vs {report.stats.get('sha_right')}; "
        f"{report.stats.get('backend')}"
    )
    items = [
        ctx.remember(
            "image",
            source="compare",
            summary=verdict[:220],
            payload={
                "findings": report.findings[:6],
                "sha_left": report.stats.get("sha_left"),
                "sha_right": report.stats.get("sha_right"),
                "backend": report.stats.get("backend"),
            },
        )
    ]
    for item in report.evidence or []:
        if item.get("type") != "pixel_region":
            continue
        items.append(
            ctx.remember(
                "image",
                source=str(item.get("id") or "compare"),
                summary=str(item.get("content") or verdict)[:220],
                payload={"box": item.get("box"), "type": item.get("type")},
                confidence=item.get("confidence") if isinstance(item.get("confidence"), (int, float)) else None,
            )
        )
        if len(items) >= 3:
            break
    return _json({"evidence": items, "summary": report.summary, "findings": report.findings[:6], "evidence_items": report.evidence[:4]})


def _get_rules(ctx: ToolContext, arguments: dict) -> str:
    hits = rules_provider.select(ctx.config, arguments.get("task") or "", arguments.get("files") or [])
    ids = []
    for hit in hits:
        ids.append(ctx.remember("rule", source=hit["id"], summary=hit["id"], payload=hit))
    return _json({"verbatim": True, "rules": hits, "evidence": ids})


def _query_data(ctx: ToolContext, arguments: dict) -> str:
    payload = data_provider.analyze(ctx.config, str(arguments["path"]), arguments.get("query"))
    identifier = ctx.remember("data", source=str(arguments["path"]), summary=str(payload.get("summary") or "data"), payload=payload)
    return _json({"evidence": identifier, **payload})


def _fetch_issue(ctx: ToolContext, arguments: dict) -> str:
    payload = jira_provider.fetch(str(arguments["key"]), repo_root=ctx.config.repo_root)
    identifier = ctx.remember("jira", source=str(arguments["key"]), summary=str(payload.get("goal") or payload.get("error") or ""), payload=payload)
    return _json({"evidence": identifier, **payload})


def _fetch_page(ctx: ToolContext, arguments: dict) -> str:
    payload = confluence_provider.fetch(str(arguments["page"]), repo_root=ctx.config.repo_root)
    identifier = ctx.remember(
        "doc",
        source=str(arguments["page"]),
        summary=str(payload.get("title") or payload.get("error") or ""),
        payload=payload,
    )
    return _json({"evidence": identifier, **payload})


def _propose_patch(ctx: ToolContext, arguments: dict) -> str:
    if ctx.autonomy not in {PATCH, AUTO}:
        raise GuardrailError(f"propose_patch requires autonomy patch or auto, current={ctx.autonomy}")
    report = edit.fix(ctx.config, ctx.client, arguments.get("path"), str(arguments["task"]), mode="propose")
    identifier = ctx.remember("code", source="patch", summary=report.summary[:300], payload={"changes": report.changes[:8]})
    patch_id = str((report.stats or {}).get("patch_id") or "")
    if patch_id:
        ctx.artifacts["patch_id"] = patch_id
    return _json({"evidence": identifier, "summary": report.summary, "changes": report.changes, "stats": report.stats, "patch_id": patch_id})


def _apply_patch(ctx: ToolContext, arguments: dict) -> str:
    if ctx.autonomy != AUTO:
        raise GuardrailError("apply_patch requires autonomy=auto")
    report = edit.apply_patch(ctx.config, str(arguments["patch_id"]))
    ctx.artifacts["patch_id"] = str(arguments["patch_id"])
    ctx.artifacts["files_applied"] = report.changes[:12]
    return _json({"summary": report.summary, "changes": report.changes, "errors": report.errors})


def _crop_image(ctx: ToolContext, arguments: dict) -> str:
    report, destination = ocr.crop_region(ctx.config, str(arguments.get("id") or ""))
    identifier = ctx.remember(
        "image",
        source=str(arguments.get("id") or ""),
        summary=report.summary[:300],
        path=str(destination),
        payload={"crop": str(destination)},
    )
    return _json({"evidence": identifier, "crop": str(destination), "summary": report.summary})


def _get_evidence(ctx: ToolContext, arguments: dict) -> str:
    from .store import expand as expand_evidence

    identifier = str(arguments.get("id") or "").strip()
    payload = expand_evidence(identifier, ctx.db)
    return _json(payload)


def _inspect_diff(ctx: ToolContext, arguments: dict) -> str:
    report = tasks.diff_review(
        ctx.config,
        ctx.client,
        scope=str(arguments.get("scope") or "worktree"),
        base=arguments.get("base"),
        task=arguments.get("task"),
    )
    ctx.source_chars += int(report.stats.get("source_caracteres") or 0)
    identifier = ctx.remember("diff", source="git", summary=report.summary[:300], payload={"findings": report.findings[:8]})
    return _json({"evidence": identifier, "summary": report.summary, "findings": report.findings[:8], "risks": report.risks[:6]})


def _visual_reason(ctx: ToolContext, arguments: dict) -> str:
    path = ocr.resolve_image_path(ctx.config, str(arguments.get("path") or ""))
    payload = vision.reason(ctx.config, ctx.client, path, str(arguments.get("ocr_text") or ""), arguments.get("task"))
    identifier = ctx.remember("image", source=str(path), summary=str(payload.get("notes") or payload)[:300], payload=payload)
    return _json({"evidence": identifier, **payload})


TOOLS = {
    "search_repo": _search_repo,
    "read_file": _read_file,
    "read_lines": _read_file,
    "list_files": _list_files,
    "git_status": _git_status,
    "git_diff": _git_diff,
    "inspect_diff": _inspect_diff,
    "run_check": _run_check,
    "run_tests": _run_check,
    "run_lint": _run_check,
    "inspect_log": _inspect_log,
    "inspect_image": _inspect_image,
    "crop_image": _crop_image,
    "compare_images": _compare,
    "visual_reason": _visual_reason,
    "get_evidence": _get_evidence,
    "get_rules": _get_rules,
    "query_data": _query_data,
    "fetch_issue": _fetch_issue,
    "fetch_page": _fetch_page,
    "propose_patch": _propose_patch,
    "create_patch": _propose_patch,
    "apply_patch": _apply_patch,
}
