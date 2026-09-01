"""Serveur MCP stdio sans dépendance, exposant le local-agent aux clients Claude Code et Cursor."""

from __future__ import annotations

import json
import sys
import time
import traceback
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from . import edit, tasks
from .config import Config, get_config
from .files import GuardrailError, ensure_usable_root
from .mlx import MlxClient, MlxError
from .report import clamp, render_markdown

SERVER_NAME = "local-agent"
SERVER_VERSION = "1.0.0"
DEFAULT_PROTOCOL = "2024-11-05"
SUPPORTED_PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18"}

_PATH = {"type": "string", "description": "Chemin relatif au dépôt (défaut : racine)"}
_REPO = {
    "type": "string",
    "description": (
        "Racine absolue du dépôt à analyser. À renseigner uniquement si le dépôt visé n'est pas celui "
        "configuré par défaut, que local_ping affiche."
    ),
}
_GLOBS = {
    "type": "array",
    "items": {"type": "string"},
    "description": "Filtres ripgrep, ex. ['*.php', '!*Test.php']",
}

TOOLS: list[dict] = [
    {
        "name": "local_search",
        "description": (
            "Réponds à une question de localisation dans le code sans charger de fichiers dans ton contexte. "
            "Le modèle local dérive des motifs ripgrep, exécute la recherche, lit lui-même les extraits utiles "
            "et renvoie une synthèse compacte avec fichiers et numéros de ligne. "
            "À privilégier dès qu'une réponse exigerait de lire plus de deux ou trois fichiers."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Question en langage naturel"},
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
            "Analyse un répertoire ou un fichier avec une consigne libre, en découpant automatiquement en lots. "
            "Modes : inspect (consigne libre), summarize (rôle de chaque fichier), duplicates (implémentations "
            "dupliquées). Utilise-le pour l'exploration de gros répertoires, les résumés de nombreux fichiers, "
            "la détection de doublons et la classification en masse."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": _PATH,
                "task": {"type": "string", "description": "Consigne précise adressée au modèle local"},
                "mode": {"type": "string", "enum": ["inspect", "summarize", "duplicates"], "default": "inspect"},
                "globs": _GLOBS,
                "max_files": {"type": "integer", "description": "Plafond de fichiers pour cet appel"},
                "repo": _REPO,
            },
            "required": ["path"],
        },
    },
    {
        "name": "local_review",
        "description": (
            "Première passe de revue de code sur un chemin : bugs probables, incohérences, duplications, écarts "
            "aux conventions. Tu gardes la revue finale et les arbitrages, le modèle local produit le premier tri."
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
            "Applique une modification mécanique aux fichiers d'un chemin (renommage, boilerplate, docblocks, "
            "erreurs simples). Le modèle local réécrit les fichiers sur disque. Garde-fous : les fichiers "
            "non committés ou non suivis par git sont préservés, la syntaxe PHP est vérifiée et toute réécriture "
            "invraisemblable est annulée. Vérifie ensuite `git diff` toi-même avant de valider. "
            "À réserver aux changements mécaniques, jamais aux migrations sensibles ni à la sécurité."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": _PATH,
                "task": {"type": "string", "description": "Consigne de modification, précise et mécanique"},
                "globs": _GLOBS,
                "max_files": {"type": "integer"},
                "dry_run": {"type": "boolean", "default": False, "description": "N'écrit rien, liste les intentions"},
                "allow_dirty": {
                    "type": "boolean",
                    "default": False,
                    "description": "Autorise à toucher des fichiers déjà modifiés, à éviter",
                },
                "repo": _REPO,
            },
            "required": ["path", "task"],
        },
    },
    {
        "name": "local_test_analysis",
        "description": (
            "Exécute un contrôle du projet (phpstan, phpunit, cs-fixer, twig, yaml, eslint) dans le conteneur "
            "Docker, filtre les succès répétitifs et ne renvoie que les échecs classés, les causes probables et "
            "les statistiques. Aucune commande écrivante n'est accessible."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["phpstan", "phpunit", "cs-fixer", "twig", "yaml", "eslint"],
                    "default": "phpstan",
                },
                "target": {"type": "string", "description": "Chemin ciblé, fortement recommandé pour phpunit"},
                "filter": {"type": "string", "description": "Filtre PHPUnit sur le nom des tests"},
                "repo": _REPO,
            },
        },
    },
    {
        "name": "local_log_analysis",
        "description": (
            "Analyse un fichier ou répertoire de logs volumineux. Le filtrage, le regroupement par signature et "
            "le comptage se font localement avec ripgrep, le modèle local ne raisonne que sur les signatures "
            "dominantes. Ne renvoie que les erreurs, motifs, causes probables et extraits utiles."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Chemin du log, ex. var/log/dev.log"},
                "task": {"type": "string"},
                "patterns": {"type": "array", "items": {"type": "string"}, "description": "Motifs ripgrep explicites"},
                "repo": _REPO,
            },
            "required": ["path"],
        },
    },
    {
        "name": "local_diff_review",
        "description": (
            "Passe en revue un diff git sans le charger dans ton contexte : le modèle local lit le diff, "
            "signale bugs probables, restes de débogage et risques de régression, et propose un message de "
            "commit. Périmètres : worktree (tout le non committé), staged (l'index), branch (écart avec la "
            "branche de base). À privilégier dès qu'un diff dépasse quelques dizaines de lignes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope": {"type": "string", "enum": ["worktree", "staged", "branch"], "default": "worktree"},
                "base": {"type": "string", "description": "Branche de base pour scope=branch (défaut : main/master/develop)"},
                "task": {"type": "string", "description": "Consigne de revue spécifique, sinon revue générale"},
                "repo": _REPO,
            },
        },
    },
    {
        "name": "local_ping",
        "description": "Vérifie que le serveur MLX répond et affiche la configuration effective du local-agent.",
        "inputSchema": {"type": "object", "properties": {"repo": _REPO}},
    },
]


USAGE_LOG = Path.home() / ".local-agent" / "usage.jsonl"


def _log_usage(tool: str, start: float, output_chars: int, *, error: bool) -> None:
    """Journal d'appels pour mesurer les économies réelles, jamais bloquant pour la réponse."""
    try:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tool": tool,
            "duree_s": round(time.monotonic() - start, 1),
            "sortie_caracteres": output_chars,
            "erreur": error,
        }
        USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with USAGE_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _with_repo(config: Config, arguments: dict) -> Config:
    """Un dépôt explicite prime sur la racine configurée, les clients MCP ne fournissant pas tous le workspace."""
    override = str(arguments.get("repo") or "").strip()
    if not override or "${" in override:
        return config
    return replace(config, repo_root=Path(override).expanduser().resolve())


def _handle_tool(name: str, arguments: dict, config: Config, client: MlxClient) -> str:
    config = _with_repo(config, arguments)
    if name == "local_ping":
        try:
            ensure_usable_root(config)
            root_state = "dépôt git valide"
        except GuardrailError as error:
            root_state = f"INUTILISABLE : {error}"
        payload = {"mlx": client.ping(), "repo_root_state": root_state, "config": config.as_summary()}
        return json.dumps(payload, ensure_ascii=False, indent=2)

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
        report = edit.fix(
            config,
            client,
            arguments.get("path"),
            str(arguments["task"]),
            globs=arguments.get("globs"),
            max_files=arguments.get("max_files"),
            dry_run=bool(arguments.get("dry_run")),
            allow_dirty=bool(arguments.get("allow_dirty")),
        )
    elif name == "local_test_analysis":
        report = tasks.check(
            config,
            client,
            str(arguments.get("kind") or "phpstan"),
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
    else:
        raise ValueError(f"outil inconnu : {name}")

    return render_markdown(report, config)


class Server:
    def __init__(self) -> None:
        self.config = get_config()
        self.client = MlxClient(self.config)
        self.protocol = DEFAULT_PROTOCOL

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
        return self._error(identifier, -32601, f"méthode non supportée : {method}")

    def _call(self, identifier, params: dict) -> dict:
        name = str(params.get("name") or "")
        arguments = params.get("arguments") or {}
        start = time.monotonic()
        try:
            text = _handle_tool(name, arguments, self.config, self.client)
            _log_usage(name, start, len(text), error=False)
            return self._result(identifier, {"content": [{"type": "text", "text": text}], "isError": False})
        except (GuardrailError, MlxError, ValueError, KeyError) as error:
            message = f"local-agent ({name}) a refusé ou échoué : {error}"
        except Exception as error:  # noqa: BLE001
            print(traceback.format_exc(), file=sys.stderr)
            message = f"local-agent ({name}) erreur interne : {type(error).__name__} {error}"
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
            print("local-agent MCP : ligne JSON invalide ignorée", file=sys.stderr)
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
