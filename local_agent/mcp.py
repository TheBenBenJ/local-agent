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
from . import edit, ocr, shell, tasks

SERVER_NAME = "local-agent"
SERVER_VERSION = "1.0.0"
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
            "detection and bulk classification. Not for screenshots, tickets, or instructions you "
            "must follow verbatim."
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
            "context and without swapping the local LLM. Uses macOS Vision OCR (Tesseract fallback). "
            "Pass a filesystem path; absolute paths are allowed because captures rarely live in the "
            "git repo. One call can take several images. Inventory only: labels, errors, buttons, "
            "empty states. Not a recette verdict. For layout or colour, crop a region with "
            "local_image_crop instead of attaching the full screenshot."
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
        "description": "Check that the local LLM server responds and show the effective local-agent configuration.",
        "inputSchema": {"type": "object", "properties": {"repo": _REPO}},
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


def _result_from_report(report: Report, config: Config) -> ToolResult:
    text = render_markdown(report, config)
    source = report.stats.get("source_caracteres")
    source_i = int(source) if isinstance(source, int) else 0
    saved = max(0, source_i - len(text)) if source_i else 0
    return ToolResult(text=text, saved=saved, source_chars=source_i)


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
        payload = {
            "mlx": client.ping(),
            "repo_root_state": root_state,
            "checks_disponibles": checks,
            "ocr": ocr.backend_status(),
            "config": config.as_summary(),
        }
        return ToolResult(text=json.dumps(payload, ensure_ascii=False, indent=2))

    if name == "local_search":
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
        )
        return _result_from_report(report, ocr.image_config(config))
    elif name == "local_image_crop":
        if not arguments.get("id"):
            raise ValueError("id is required, e.g. a832b1c4-R1")
        report, crop_path = ocr.crop_region(config, str(arguments["id"]))
        result = _result_from_report(report, ocr.image_config(config))
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

    return _result_from_report(report, config)


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
            remaining = max(1, self.config.compound_turns)
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
