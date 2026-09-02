"""Serveur MCP stdio sans dépendance, exposant le local-agent aux clients Claude Code et Cursor."""

from __future__ import annotations

import json
import sys
import time
import traceback
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from . import edit, shell, tasks
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
            "À privilégier dès qu'une réponse exigerait de lire plus de deux ou trois fichiers. "
            "Si tu connais déjà le nom d'une classe, d'un attribut ou d'un champ, greppe : "
            "local_search sert à la question ouverte, pas à localiser un symbole nommé."
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
            "Modification mécanique des fichiers d'un chemin (renommage, boilerplate, docblocks, erreurs "
            "simples), en deux temps par défaut : mode=propose (défaut) génère les changements, renvoie le "
            "diff et un patch_id sans rien écrire ; mode=apply avec patch_id applique la proposition exacte, "
            "refusée si un fichier a changé entre-temps. mode=direct écrit immédiatement, à réserver aux "
            "changements triviaux. Garde-fous : fichiers non committés préservés, syntaxe vérifiée, "
            "réécriture invraisemblable annulée. À réserver au mécanique, jamais aux migrations sensibles."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": _PATH,
                "task": {"type": "string", "description": "Consigne de modification, précise et mécanique"},
                "mode": {"type": "string", "enum": ["propose", "apply", "direct"], "default": "propose"},
                "patch_id": {"type": "string", "description": "Identifiant renvoyé par un mode=propose"},
                "globs": _GLOBS,
                "max_files": {"type": "integer"},
                "allow_dirty": {
                    "type": "boolean",
                    "default": False,
                    "description": "Autorise à toucher des fichiers déjà modifiés, à éviter",
                },
                "repo": _REPO,
            },
            "required": [],
        },
    },
    {
        "name": "local_test_analysis",
        "description": (
            "Exécute un contrôle du projet (tests, lint, analyse statique), filtre les succès répétitifs et ne "
            "renvoie que les échecs classés, les causes probables et les statistiques. Les contrôles disponibles "
            "viennent du fichier .local-agent.json du dépôt ou d'un preset selon le langage (Symfony : phpstan, "
            "phpunit, cs-fixer, twig, yaml, eslint ; Node : test, lint, types ; Python : pytest, ruff, mypy). "
            "local_ping les liste. Aucune commande écrivante n'est accessible."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "description": "Nom du contrôle, défaut : le premier disponible"},
                "target": {"type": "string", "description": "Chemin ciblé, fortement recommandé pour les tests"},
                "filter": {"type": "string", "description": "Filtre sur le nom des tests, si le contrôle l'accepte"},
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
USAGE_TOTALS = Path.home() / ".local-agent" / "usage-totals.json"


def _log_usage(tool: str, start: float, output_chars: int, *, error: bool, saved_chars: int = 0) -> None:
    """Journal d'appels pour mesurer les économies réelles, jamais bloquant pour la réponse."""
    try:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tool": tool,
            "duree_s": round(time.monotonic() - start, 1),
            "sortie_caracteres": output_chars,
            "economise_caracteres": saved_chars,
            "erreur": error,
        }
        USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with USAGE_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _bump_totals(saved_chars: int) -> dict:
    """Cumul de vie du serveur, persistant : c'est la preuve chiffrée de ce que l'outil rapporte."""
    totals = {"calls": 0, "saved_chars": 0}
    try:
        if USAGE_TOTALS.is_file():
            totals.update(json.loads(USAGE_TOTALS.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        pass
    totals["calls"] = int(totals.get("calls") or 0) + 1
    totals["saved_chars"] = int(totals.get("saved_chars") or 0) + saved_chars
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


def _handle_tool(name: str, arguments: dict, config: Config, client: MlxClient) -> tuple[str, int]:
    """Rend le texte de réponse et l'estimation de contexte épargné à l'orchestrateur."""
    config = _with_repo(config, arguments)
    if name == "local_ping":
        try:
            ensure_usable_root(config)
            root_state = "dépôt git valide"
        except GuardrailError as error:
            root_state = f"INUTILISABLE : {error}"
        try:
            checks = sorted(shell.load_checks(config))
        except ValueError as error:
            checks = [f"illisibles : {error}"]
        payload = {
            "mlx": client.ping(),
            "repo_root_state": root_state,
            "checks_disponibles": checks,
            "config": config.as_summary(),
        }
        return json.dumps(payload, ensure_ascii=False, indent=2), 0

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
                raise ValueError("mode=apply exige patch_id, renvoyé par un mode=propose préalable")
            report = edit.apply_patch(config, str(arguments["patch_id"]))
        else:
            if not arguments.get("task"):
                raise ValueError("task est obligatoire en mode propose ou direct")
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
    else:
        raise ValueError(f"outil inconnu : {name}")

    text = render_markdown(report, config)
    source = report.stats.get("source_caracteres")
    saved = max(0, int(source) - len(text)) if isinstance(source, int) else 0
    return text, saved


class Server:
    def __init__(self) -> None:
        self.config = get_config()
        self.client = MlxClient(self.config)
        self.protocol = DEFAULT_PROTOCOL
        self.session_calls = 0
        self.session_saved = 0

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
            text, saved = _handle_tool(name, arguments, self.config, self.client)
            _log_usage(name, start, len(text), error=False, saved_chars=saved)
            if name != "local_ping":
                self.session_calls += 1
                self.session_saved += saved
                totals = _bump_totals(saved)
                text += (
                    f"\n\nÉconomie estimée : ~{saved // 4} tokens cet appel · "
                    f"~{self.session_saved // 4} tokens sur {self.session_calls} appel(s) cette session · "
                    f"~{totals['saved_chars'] // 4} tokens sur {totals['calls']} appel(s) au total."
                )
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
