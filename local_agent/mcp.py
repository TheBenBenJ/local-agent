"""Serveur MCP stdio sans dépendance, exposant le local-agent aux clients Claude Code et Cursor."""

from __future__ import annotations

import base64
import json
import sys
import time
import traceback
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from .budget import billed_chars, savings_footer
from .config import Config, get_config
from .files import GuardrailError, ensure_usable_root
from .mlx import MlxClient, MlxError
from .report import Report, clamp, render_markdown
from .version import SERVER_NAME, SERVER_VERSION, describe
from . import agent, compare, doctor, edit, ocr, shell, store, tasks

DEFAULT_PROTOCOL = "2024-11-05"
SUPPORTED_PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18"}

_PATH = {"type": "string", "description": "Path relative to the repository root (default: root)"}
_REPO = {
    "type": "string",
    "description": (
        "Absolute path of the repository to analyse. Set this only when it is not the default root "
        "shown by local_ping."
    ),
}
_GLOBS = {
    "type": "array",
    "items": {"type": "string"},
    "description": "ripgrep globs, e.g. ['*.php', '!*Test.php']",
}

TOOLS: list[dict] = [
    {
        "name": "local_task",
        "description": (
            "Primary entry: send a mission and sources (repo://, image://, log://, jira://, confluence://, data://). "
            "Prefer this before acquiring large repo, log, image or doc context yourself. "
            "Known symbol or tiny source: deterministic tools, no local LLM. "
            "If you already know a class or function name, put it in the mission so DIRECT can grep it. "
            "Large reducible source: extract then one local synthesis. "
            "High-risk or architecture: skip, keep Claude. "
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Mission in natural language"},
                "sources": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Pointers such as repo://src, image:///abs/shot.png, jira://LYSI-1, confluence://SPACE/Title",
                },
                "path": {"type": "string", "description": "Optional repo-relative focus path"},
                "autonomy": {
                    "type": "string",
                    "enum": ["read_only", "patch", "safe", "auto"],
                    "description": "read_only default; auto must be explicit",
                },
                "output_budget": {"type": "integer", "description": "Max tokens in the local model completion"},
                "local_context_budget": {"type": "integer", "description": "Max characters kept in the local tool-loop context"},
                "risk_level": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH"]},
                "trace": {
                    "type": "boolean",
                    "description": "Store a local tool-call trace. Not returned in the MCP payload.",
                },
                "why": {
                    "type": "string",
                    "description": "Optional reason this was delegated (screenshot, large_log, failing_tests, ...)",
                },
                "repo": _REPO,
            },
            "required": ["task"],
        },
    },
    {
        "name": "local_expand",
        "description": (
            "Fetch one or more evidence items by id (CODE-E12, E12, IMG-E2, a832b1c4-R1, "
            "or the 8-char image id from local_image). Raw excerpts stay local until you ask. "
            "Prefer this over re-reading the file. Ask for what you need: fields=[\"comments\"] "
            "or max_chars=600 rather than the whole payload, which can be several thousand tokens "
            "for a wiki page. An 8-char image id returns the full OCR table."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Evidence id, e.g. CODE-E12 or a832b1c4-R1"},
                "ids": {"type": "array", "items": {"type": "string"}},
                "fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Payload keys to return, e.g. [\"comments\"] or [\"body\"]. Everything by default.",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Trim each returned string to this length. No trim by default.",
                },
                "repo": _REPO,
            },
        },
    },
    {
        "name": "local_metrics",
        "description": (
            "Current-session dashboard (LOCAL-AGENT CURRENT SESSION) plus lifetime totals. "
            "Direct context avoided only. Exposure estimate is not shown here."
        ),
        "inputSchema": {"type": "object", "properties": {"repo": _REPO}},
    },
    {
        "name": "local_image_compare",
        "description": (
            "Compare two screenshots without loading either into your context. Hash, dimensions, "
            "OCR text, then a 24x24 pixel grid diff; returns material differences and region ids to expand."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "reference": {"type": "string", "description": "Reference image path"},
                "current": {"type": "string", "description": "Current image path"},
                "repo": _REPO,
            },
            "required": ["reference", "current"],
        },
    },
    {
        "name": "local_search",
        "description": (
            "Answer a code-location question without loading files into your context. "
            "The local model derives ripgrep patterns, runs the search, reads useful excerpts itself "
            "and returns a compact synthesis with files and line numbers. "
            "Prefer this as soon as an answer would require reading more than two or three files. "
            "Not for screenshots, tickets, or instructions you must follow verbatim: only bulky "
            "repository text you would otherwise load. "
            "If you already know a class, attribute or field name, grep: local_search is for open "
            "questions, not for locating a named symbol."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Question in natural language"},
                "path": _PATH,
                "globs": _GLOBS,
                "repo": _REPO,
            },
            "required": ["query"],
        },
    },
    {
        "name": "local_analyze",
        "description": (
            "Analyse a directory or file against a free-form task, chunking automatically. "
            "Modes: inspect (free-form), summarize (role of each file), duplicates (repeated "
            "implementations). Use it for large-tree exploration, multi-file summaries, duplicate "
            "detection and bulk classification. A path that is an image, or a folder of images, is "
            "OCR'd locally (same as local_image); if the loaded model has vision, a layout pass "
            "uses the same weights. Not for tickets or "
            "instructions you must follow verbatim."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": _PATH,
                "task": {"type": "string", "description": "Precise instruction for the local model"},
                "mode": {"type": "string", "enum": ["inspect", "summarize", "duplicates"], "default": "inspect"},
                "globs": _GLOBS,
                "max_files": {"type": "integer", "description": "File cap for this call"},
                "repo": _REPO,
            },
            "required": ["path"],
        },
    },
    {
        "name": "local_review",
        "description": (
            "First-pass code review on a path: likely bugs, inconsistencies, duplication, convention "
            "drift. You keep the final review and the calls; the local model does the first triage."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"path": _PATH, "task": {"type": "string"}, "globs": _GLOBS, "repo": _REPO},
            "required": ["path"],
        },
    },
    {
        "name": "local_fix",
        "description": (
            "Mechanical edits on files under a path (renames, boilerplate, docblocks, simple errors), "
            "in two steps by default: mode=propose (default) generates the changes, returns the diff "
            "and a patch_id without writing; mode=apply with patch_id applies that exact proposal, "
            "refused if a file changed in between. mode=direct writes immediately, for trivial edits "
            "only. Guardrails: dirty files preserved, syntax checked, implausible rewrites rolled back. "
            "Mechanical only, never sensitive migrations."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": _PATH,
                "task": {"type": "string", "description": "Precise, mechanical edit instruction"},
                "mode": {"type": "string", "enum": ["propose", "apply", "direct"], "default": "propose"},
                "patch_id": {"type": "string", "description": "Id returned by a mode=propose call"},
                "globs": _GLOBS,
                "max_files": {"type": "integer"},
                "allow_dirty": {
                    "type": "boolean",
                    "default": False,
                    "description": "Allow touching already-dirty files, avoid this",
                },
                "repo": _REPO,
            },
            "required": [],
        },
    },
    {
        "name": "local_test_analysis",
        "description": (
            "Run a project check (tests, lint, static analysis), filter repetitive successes and return "
            "only classified failures, likely causes and stats. Available checks come from the repo's "
            ".local-agent.json or a language preset (Symfony: phpstan, phpunit, cs-fixer, twig, yaml, "
            "eslint; Node: test, lint, types; Python: pytest, ruff, mypy). local_ping lists them. "
            "No write commands are exposed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "description": "Check name, default: the first available"},
                "target": {"type": "string", "description": "Target path, strongly recommended for tests"},
                "filter": {"type": "string", "description": "Test-name filter, if the check accepts one"},
                "repo": _REPO,
            },
        },
    },
    {
        "name": "local_log_analysis",
        "description": (
            "Analyse a large log file or directory. Filtering, grouping by signature and counting run "
            "locally with ripgrep; the local model only reasons over dominant signatures. Returns errors, "
            "patterns, likely causes and useful excerpts."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Log path, e.g. var/log/dev.log"},
                "task": {"type": "string"},
                "patterns": {"type": "array", "items": {"type": "string"}, "description": "Explicit ripgrep patterns"},
                "repo": _REPO,
            },
            "required": ["path"],
        },
    },
    {
        "name": "local_image",
        "description": (
            "Extract on-screen text from a screenshot or image without loading the pixels into your "
            "context. Uses macOS Vision OCR (Tesseract fallback) first. If the loaded local model "
            "has vision, a second pass on the same weights fills layout gaps (merged headers, "
            "selected filters, disabled buttons). Pixels stay local; pass a filesystem path. "
            "Inventory only: labels, errors, buttons, empty states. Not a recette verdict. "
            "The full OCR table stays on disk: local_expand the 8-char image id. "
            "For colour of one region, crop with local_image_crop instead of attaching the full screenshot."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Image file path (absolute allowed, screenshots are often outside the repo)",
                },
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Additional images, e.g. a recette set of three screenshots",
                },
                "task": {
                    "type": "string",
                    "description": "Optional focus: matching OCR lines are listed first (substring filter, no model)",
                },
                "repo": _REPO,
            },
        },
    },
    {
        "name": "local_image_crop",
        "description": (
            "After local_image, fetch one region by id (e.g. a832b1c4-R1). Returns a cropped PNG "
            "so you can inspect layout or colour of that region only. No LLM, no full screenshot. "
            "Use this instead of attaching the original capture."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "Region id from local_image, e.g. a832b1c4-R1",
                },
                "repo": _REPO,
            },
            "required": ["id"],
        },
    },
    {
        "name": "local_diff_review",
        "description": (
            "Review a git diff without loading it into your context: the local model reads the diff, "
            "flags likely bugs, leftover debug and regression risks, and suggests a commit message. "
            "Added calls are checked against the repo: a method already defined elsewhere is not flagged "
            "as missing. Scopes: worktree (all uncommitted), staged (index), branch (delta from the base "
            "branch). Prefer this as soon as a diff is more than a few dozen lines. Cheap second "
            "look even when a finding later proves false: verify, do not skip the review."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope": {"type": "string", "enum": ["worktree", "staged", "branch"], "default": "worktree"},
                "base": {"type": "string", "description": "Base branch for scope=branch (default: main/master/develop)"},
                "task": {"type": "string", "description": "Specific review instruction, otherwise a general review"},
                "repo": _REPO,
            },
        },
    },
    {
        "name": "local_ping",
        "description": (
            "Liveness check: model, vision, OCR backend, repo state. "
            "full=true adds the effective configuration and the model capabilities; "
            "counters live in local_metrics."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "full": {"type": "boolean", "description": "Return configuration, capabilities and counters too"},
                "repo": _REPO,
            },
        },
    },
]


USAGE_LOG = Path.home() / ".local-agent" / "usage.jsonl"
USAGE_TOTALS = Path.home() / ".local-agent" / "usage-totals.json"


def _log_usage(
    tool: str,
    start: float,
    output_chars: int,
    *,
    error: bool,
    saved_chars: int = 0,
    compounded_chars: int = 0,
) -> None:
    """Journal d'appels pour mesurer les économies réelles, jamais bloquant pour la réponse."""
    try:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tool": tool,
            "duree_s": round(time.monotonic() - start, 1),
            "sortie_caracteres": output_chars,
            "economise_caracteres": saved_chars,
            "economise_compose_caracteres": compounded_chars,
            "erreur": error,
        }
        USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with USAGE_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _bump_totals(saved_chars: int, compounded_chars: int = 0) -> dict:
    """Cumul de vie du serveur, persistant : c'est la preuve chiffrée de ce que l'outil rapporte."""
    totals = {"calls": 0, "saved_chars": 0, "compounded_chars": 0}
    try:
        if USAGE_TOTALS.is_file():
            totals.update(json.loads(USAGE_TOTALS.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        pass
    totals["calls"] = int(totals.get("calls") or 0) + 1
    totals["saved_chars"] = int(totals.get("saved_chars") or 0) + saved_chars
    totals["compounded_chars"] = int(totals.get("compounded_chars") or 0) + compounded_chars
    try:
        USAGE_TOTALS.parent.mkdir(parents=True, exist_ok=True)
        USAGE_TOTALS.write_text(json.dumps(totals), encoding="utf-8")
    except OSError:
        pass
    return totals


def _with_repo(config: Config, arguments: dict) -> Config:
    """Un dépôt explicite prime sur la racine configurée, les clients MCP ne fournissant pas tous le workspace."""
    override = str(arguments.get("repo") or "").strip()
    if not override or "${" in override:
        return config
    return replace(config, repo_root=Path(override).expanduser().resolve())


@dataclass
class ToolResult:
    text: str
    saved: int = 0
    source_chars: int = 0
    media: list[dict] = field(default_factory=list)


def _result_from_report(report: Report, config: Config, tool: str = "") -> ToolResult:
    db = store.Store()
    try:
        store.attach_report_evidence(db, report)
        text = render_markdown(report, config, with_savings=False)
        source = report.stats.get("source_caracteres")
        source_i = int(source) if isinstance(source, int) else 0
        saved = max(0, source_i - len(text)) if source_i else 0
        if tool:
            db.record_metric(
                tool=tool,
                source_type=tool.replace("local_", "", 1),
                raw_tokens=source_i // 4,
                visible_tokens=len(text) // 4,
                avoided_tokens=max(0, source_i - len(text)) // 4,
                latency_s=float(report.stats.get("latency_s") or 0),
                status="ok",
            )
    finally:
        db.close()
    return ToolResult(text=text, saved=saved, source_chars=source_i)


def _trim(value: object, limit: int | None) -> object:
    if not limit or limit <= 0:
        return value
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + " [... tronqué, relancer sans max_chars ...]"
    if isinstance(value, list):
        return [_trim(item, limit) for item in value]
    if isinstance(value, dict):
        return {key: _trim(item, limit) for key, item in value.items()}
    return value


def _select_fields(payload: dict, fields: object, max_chars: object) -> dict:
    """Rendre un champ demandé plutôt que le payload entier : c'est là que part le contexte."""
    limit = int(max_chars) if isinstance(max_chars, (int, float)) and max_chars else None
    interieur = payload.get("payload")
    noms = [str(item) for item in (fields or []) if str(item).strip()]
    if noms and isinstance(interieur, dict):
        garde = {nom: interieur[nom] for nom in noms if nom in interieur}
        absents = [nom for nom in noms if nom not in interieur]
        payload = dict(payload)
        payload["payload"] = _trim(garde, limit)
        payload["payload_keys"] = sorted(interieur)
        if absents:
            payload["fields_absents"] = absents
        return payload
    if limit:
        payload = dict(payload)
        payload["payload"] = _trim(interieur, limit)
    return payload


def _handle_tool(name: str, arguments: dict, config: Config, client: MlxClient) -> ToolResult:
    """Rend le texte de réponse et l'estimation de contexte épargné à l'orchestrateur."""
    config = _with_repo(config, arguments)
    if name == "local_ping":
        try:
            ensure_usable_root(config)
            root_state = "valid git repository"
        except GuardrailError as error:
            root_state = f"UNUSABLE: {error}"
        try:
            checks = sorted(shell.load_checks(config))
        except ValueError as error:
            checks = [f"unreadable: {error}"]
        mlx = client.ping()
        if arguments.get("full"):
            payload = {
                "mlx": mlx,
                "server": describe(),
                "repo_root_state": root_state,
                "checks_disponibles": checks,
                "ocr": ocr.backend_status(),
                "config": config.as_summary(),
                "metrics": store.Store().stats(),
            }
            return ToolResult(text=json.dumps(payload, ensure_ascii=False, indent=2))
        # Un contrôle de vie n'a pas à coûter la configuration entière : full=true la rend.
        capacites = (mlx.get("capabilities") or {}) if isinstance(mlx, dict) else {}
        payload = {
            "alive": bool(mlx.get("echo")) if isinstance(mlx, dict) else False,
            "model": mlx.get("model") if isinstance(mlx, dict) else None,
            "vision": bool(mlx.get("vision")) if isinstance(mlx, dict) else False,
            "context_length": capacites.get("context_length"),
            "server_version": describe().get("version"),
            "repo_root": str(config.repo_root),
            "repo_root_state": root_state,
            "ocr_backend": (ocr.backend_status() or {}).get("preferred"),
            "checks": checks,
            "more": "full=true for config and capabilities, local_metrics for counters",
        }
        return ToolResult(text=json.dumps(payload, ensure_ascii=False, indent=2))

    if name == "local_task":
        report = agent.run_task(
            config,
            client,
            str(arguments["task"]),
            sources=arguments.get("sources"),
            path=arguments.get("path"),
            autonomy=arguments.get("autonomy"),
            output_budget=arguments.get("output_budget"),
            local_context_budget=arguments.get("local_context_budget"),
            risk_level=arguments.get("risk_level"),
            trace=bool(arguments.get("trace")),
            why=arguments.get("why"),
        )
    elif name == "local_expand":
        ids: list[str] = []
        if arguments.get("id"):
            ids.append(str(arguments["id"]))
        ids.extend(str(item) for item in (arguments.get("ids") or []) if str(item).strip())
        if not ids:
            raise ValueError("id or ids is required")
        chunks = []
        media = []
        source_chars = 0
        db = store.Store()
        for identifier in ids[:8]:
            try:
                if "-R" in identifier.upper():
                    crop_report, crop_path = ocr.crop_region(config, identifier)
                    chunks.append(render_markdown(crop_report, ocr.image_config(config)))
                    source_chars += int(crop_report.stats.get("source_caracteres") or 0)
                    size = crop_path.stat().st_size
                    if 0 < size <= ocr.MAX_EMBED_BYTES:
                        media.append(
                            {
                                "type": "image",
                                "mimeType": "image/png",
                                "data": base64.standard_b64encode(crop_path.read_bytes()).decode("ascii"),
                            }
                        )
                else:
                    payload = store.expand(identifier, db, config)
                    source_chars += len(json.dumps(payload.get("payload") or {}))
                    payload = _select_fields(payload, arguments.get("fields"), arguments.get("max_chars"))
                    chunks.append(json.dumps(payload, ensure_ascii=False, indent=2))
            except (GuardrailError, ValueError) as error:
                chunks.append(f"{identifier}: {error}")
        text = "\n\n".join(chunks)
        result = ToolResult(text=text, saved=max(0, source_chars - len(text)), source_chars=source_chars, media=media)
        return result
    elif name == "local_metrics":
        stats = store.Store().stats()
        current = stats["current"]
        lifetime = stats["lifetime"]

        def _block(title: str, bundle: dict) -> list[str]:
            return [
                title,
                f"Tasks                             {bundle['local_tasks']}",
                f"Completed locally                 {bundle['completed_without_claude']}",
                f"Escalated to Claude               {bundle['escalated']}",
                f"Local completion rate             {bundle['offload_rate']}",
                "",
                f"Local tool calls                  {bundle['tool_calls']}",
                f"Local LLM in/out tokens           {bundle['local_llm_in']}/{bundle['local_llm_out']}",
                f"Local LLM calls                   {bundle.get('local_llm_calls') or 0}",
                f"Avoidable local LLM calls         {bundle.get('avoidable_local_llm_calls') or 0}",
                f"Tiers (direct/reduce/agent)       {bundle.get('by_tier') or {}}",
                f"Cache hits                        {bundle['cache_hits']}",
                "",
                f"Raw context processed             {bundle['raw_tokens']}",
                f"Claude-visible context            {bundle['visible_tokens']}",
                f"Direct context avoided            {bundle['avoided_tokens']}",
                f"Average packet tokens             {bundle['avg_packet_tokens']}",
                f"Average latency s                 {bundle['avg_latency_s']}",
            ]

        lines = [
            f"Session                           {stats['session_id']}",
            "",
            *_block("LOCAL-AGENT CURRENT SESSION", current),
            "",
            *_block("LOCAL-AGENT LIFETIME", lifetime),
            "",
            "Direct context avoided is not billed Claude usage.",
            "No exposure estimate here; see tool footers (disabled if LOCAL_AGENT_COMPOUND_TURNS=0).",
        ]
        text = "\n".join(lines)
        return ToolResult(text=text)
    elif name == "local_image_compare":
        report = compare.compare_images(
            config, str(arguments["reference"]), str(arguments["current"]), client=client
        )
        return _result_from_report(report, ocr.image_config(config), tool=name)
    elif name == "local_search":
        report = tasks.search(
            config, client, str(arguments["query"]), arguments.get("path"), arguments.get("globs")
        )
    elif name == "local_analyze":
        report = tasks.analyze(
            config,
            client,
            arguments.get("path"),
            arguments.get("task"),
            mode=str(arguments.get("mode") or "inspect"),
            globs=arguments.get("globs"),
            max_files=arguments.get("max_files"),
        )
    elif name == "local_review":
        report = tasks.analyze(
            config,
            client,
            arguments.get("path"),
            arguments.get("task"),
            mode="review",
            globs=arguments.get("globs"),
        )
    elif name == "local_fix":
        mode = str(arguments.get("mode") or "propose")
        if mode == "apply":
            if not arguments.get("patch_id"):
                raise ValueError("mode=apply requires patch_id, returned by a prior mode=propose call")
            report = edit.apply_patch(config, str(arguments["patch_id"]))
        else:
            if not arguments.get("task"):
                raise ValueError("task is required in propose or direct mode")
            report = edit.fix(
                config,
                client,
                arguments.get("path"),
                str(arguments["task"]),
                globs=arguments.get("globs"),
                max_files=arguments.get("max_files"),
                dry_run=bool(arguments.get("dry_run")),
                allow_dirty=bool(arguments.get("allow_dirty")),
                mode=mode,
            )
    elif name == "local_test_analysis":
        report = tasks.check(
            config,
            client,
            arguments.get("kind"),
            arguments.get("target"),
            arguments.get("filter"),
        )
    elif name == "local_log_analysis":
        report = tasks.analyze_logs(
            config, client, str(arguments["path"]), arguments.get("task"), arguments.get("patterns")
        )
    elif name == "local_diff_review":
        report = tasks.diff_review(
            config,
            client,
            scope=str(arguments.get("scope") or "worktree"),
            base=arguments.get("base"),
            task=arguments.get("task"),
        )
    elif name == "local_image":
        report = ocr.read_images(
            config,
            arguments.get("path"),
            arguments.get("paths"),
            arguments.get("task"),
            client=client,
        )
        return _result_from_report(report, ocr.image_config(config), tool=name)
    elif name == "local_image_crop":
        if not arguments.get("id"):
            raise ValueError("id is required, e.g. a832b1c4-R1")
        report, crop_path = ocr.crop_region(config, str(arguments["id"]))
        result = _result_from_report(report, ocr.image_config(config), tool=name)
        size = crop_path.stat().st_size
        if 0 < size <= ocr.MAX_EMBED_BYTES:
            result.media.append(
                {
                    "type": "image",
                    "mimeType": "image/png",
                    "data": base64.standard_b64encode(crop_path.read_bytes()).decode("ascii"),
                }
            )
            extra = size * 4 // 3
            result.saved = max(0, result.source_chars - len(result.text) - extra)
        return result
    else:
        raise ValueError(f"unknown tool: {name}")

    return _result_from_report(report, config, tool=name)


class Server:
    def __init__(self) -> None:
        self.config = get_config()
        self.client = MlxClient(self.config)
        self.protocol = DEFAULT_PROTOCOL
        self.session_calls = 0
        self.session_saved = 0
        self.session_compounded = 0

    def handle(self, message: dict) -> dict | None:
        method = message.get("method")
        identifier = message.get("id")
        params = message.get("params") or {}

        if method == "initialize":
            requested = str(params.get("protocolVersion") or "")
            self.protocol = requested if requested in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL
            return self._result(
                identifier,
                {
                    "protocolVersion": self.protocol,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                },
            )
        if method in ("notifications/initialized", "notifications/cancelled"):
            return None
        if method == "ping":
            return self._result(identifier, {})
        if method == "tools/list":
            return self._result(identifier, {"tools": TOOLS})
        if method in ("resources/list", "resources/templates/list"):
            return self._result(identifier, {"resources": [], "resourceTemplates": []})
        if method == "prompts/list":
            return self._result(identifier, {"prompts": []})
        if method == "tools/call":
            return self._call(identifier, params)
        if identifier is None:
            return None
        return self._error(identifier, -32601, f"unsupported method: {method}")

    def _call(self, identifier, params: dict) -> dict:
        name = str(params.get("name") or "")
        arguments = params.get("arguments") or {}
        start = time.monotonic()
        try:
            result = _handle_tool(name, arguments, self.config, self.client)
            remaining = max(0, int(self.config.compound_turns))
            compounded = billed_chars(result.saved, remaining)
            _log_usage(
                name,
                start,
                len(result.text),
                error=False,
                saved_chars=result.saved,
                compounded_chars=compounded,
            )
            text = result.text
            if name != "local_ping":
                self.session_calls += 1
                self.session_saved += result.saved
                self.session_compounded += compounded
                totals = _bump_totals(result.saved, compounded)
                source = result.source_chars or (result.saved + len(result.text))
                text += savings_footer(
                    source,
                    len(result.text),
                    remaining,
                    session_saved=self.session_saved,
                    session_compounded=self.session_compounded,
                    lifetime_saved=int(totals.get("saved_chars") or 0),
                    lifetime_compounded=int(totals.get("compounded_chars") or 0),
                )
            content: list[dict] = [{"type": "text", "text": text}, *result.media]
            return self._result(identifier, {"content": content, "isError": False})
        except (GuardrailError, MlxError, ValueError, KeyError) as error:
            message = f"local-agent ({name}) refused or failed: {error}"
        except Exception as error:  # noqa: BLE001
            print(traceback.format_exc(), file=sys.stderr)
            message = f"local-agent ({name}) internal error: {type(error).__name__} {error}"
        _log_usage(name, start, len(message), error=True)
        return self._result(
            identifier,
            {"content": [{"type": "text", "text": clamp(message, self.config)}], "isError": True},
        )

    @staticmethod
    def _result(identifier, payload: dict) -> dict:
        return {"jsonrpc": "2.0", "id": identifier, "result": payload}

    @staticmethod
    def _error(identifier, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": identifier, "error": {"code": code, "message": message}}


def serve() -> None:
    server = Server()
    stream = sys.stdin
    while True:
        line = stream.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            print("local-agent MCP: ignored invalid JSON line", file=sys.stderr)
            continue
        response = server.handle(message)
        if response is None:
            continue
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def main() -> None:
    try:
        serve()
    except (KeyboardInterrupt, BrokenPipeError):
        pass
