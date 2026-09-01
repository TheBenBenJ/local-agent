"""Exécution de commandes locales, restreinte à une liste blanche non destructrice."""

from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import Config

CHECKS_FILE = ".local-agent.json"

DOCKER_PREFIX = ["docker", "compose", "exec", "-T", "symfony"]

# Uniquement des commandes en lecture, aucune n'écrit en base applicative ni ne construit d'assets.
_SYMFONY_CHECKS: dict[str, dict[str, object]] = {
    "phpstan": {
        "label": "PHPStan (analyse statique)",
        "command": ["./vendor/bin/phpstan", "analyse", "--no-progress", "--error-format=raw"],
        "accepts_target": True,
        "in_container": True,
    },
    "phpunit": {
        "label": "PHPUnit",
        "command": ["./vendor/bin/phpunit"],
        "accepts_target": True,
        "accepts_filter": True,
        "in_container": True,
    },
    "cs-fixer": {
        "label": "PHP-CS-Fixer (dry-run)",
        "command": ["./vendor/bin/php-cs-fixer", "fix", "--dry-run", "--diff", "--no-interaction"],
        "accepts_target": True,
        "in_container": True,
    },
    "twig": {
        "label": "Lint Twig",
        "command": ["./vendor/bin/twig-cs-fixer", "lint"],
        "accepts_target": True,
        "default_target": "templates",
        "in_container": True,
    },
    "yaml": {
        "label": "Lint YAML Symfony",
        "command": ["php", "bin/console", "lint:yaml"],
        "accepts_target": True,
        "default_target": "config/packages",
        "in_container": True,
    },
    "eslint": {
        "label": "ESLint",
        "command": ["yarn", "lint"],
        "accepts_target": False,
        "in_container": True,
    },
}

_NODE_CHECKS: dict[str, dict[str, object]] = {
    "test": {"label": "Tests npm", "command": ["npm", "test", "--silent"], "accepts_target": False},
    "lint": {"label": "Lint npm", "command": ["npm", "run", "lint", "--silent"], "accepts_target": False},
    "types": {"label": "TypeScript", "command": ["npx", "tsc", "--noEmit"], "accepts_target": False},
}

_PYTHON_CHECKS: dict[str, dict[str, object]] = {
    "pytest": {"label": "Pytest", "command": ["python3", "-m", "pytest", "-q"], "accepts_target": True, "accepts_filter": True},
    "ruff": {"label": "Ruff", "command": ["python3", "-m", "ruff", "check"], "accepts_target": True},
    "mypy": {"label": "Mypy", "command": ["python3", "-m", "mypy"], "accepts_target": True},
}


def _preset_checks(repo_root: Path) -> dict[str, dict[str, object]]:
    if (repo_root / "composer.json").exists():
        return _SYMFONY_CHECKS
    if (repo_root / "package.json").exists():
        return _NODE_CHECKS
    if (repo_root / "pyproject.toml").exists() or (repo_root / "setup.py").exists():
        return _PYTHON_CHECKS
    return {}


def load_checks(config: Config) -> dict[str, dict[str, object]]:
    """Contrôles du dépôt : `.local-agent.json` à la racine prime, sinon un preset selon le langage.

    Format du fichier : {"checks": {"nom": {"command": "npm test" ou ["npm", "test"],
    "accepts_target": bool, "accepts_filter": bool, "default_target": "chemin", "in_container": bool}}}.
    Seules ces commandes déclarées sont exécutables : le fichier est une liste blanche, pas un shell.
    """
    declared: dict[str, dict[str, object]] = {}
    manifest = config.repo_root / CHECKS_FILE
    if manifest.is_file():
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"{CHECKS_FILE} illisible : {error}")
        for name, spec in (payload.get("checks") or {}).items():
            command = spec.get("command")
            if isinstance(command, str):
                command = shlex.split(command)
            if not isinstance(command, list) or not command:
                continue
            declared[str(name)] = {
                "label": str(spec.get("label") or name),
                "command": [str(part) for part in command],
                "accepts_target": bool(spec.get("accepts_target")),
                "accepts_filter": bool(spec.get("accepts_filter")),
                "default_target": spec.get("default_target"),
                "in_container": bool(spec.get("in_container")),
            }
    return declared or _preset_checks(config.repo_root)


@dataclass
class CommandResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def output(self) -> str:
        return f"{self.stdout}\n{self.stderr}".strip()


def run(argv: list[str], cwd: Path, timeout: int) -> CommandResult:
    """stdin est neutralisé pour qu'aucune commande ne consomme le flux JSON-RPC du serveur MCP."""
    printable = " ".join(argv)
    try:
        process = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return CommandResult(printable, 124, "", f"timeout après {timeout}s", timed_out=True)
    except FileNotFoundError as error:
        return CommandResult(printable, 127, "", str(error))
    return CommandResult(printable, process.returncode, process.stdout, process.stderr)


def build_check_command(
    checks: dict[str, dict[str, object]],
    kind: str,
    target: str | None,
    filter_expression: str | None
) -> tuple[list[str], dict[str, object]]:
    spec = checks.get(kind)
    if spec is None:
        available = ", ".join(sorted(checks)) or f"aucun (déclarer des checks dans {CHECKS_FILE})"
        raise ValueError(f"contrôle inconnu : {kind}. Disponibles : {available}")
    argv = list(spec["command"])  # type: ignore[arg-type]
    effective_target = target or spec.get("default_target")
    if spec.get("accepts_target") and effective_target:
        argv.append(str(effective_target))
    if spec.get("accepts_filter") and filter_expression:
        argv += ["--filter", filter_expression]
    if spec.get("in_container"):
        argv = [*DOCKER_PREFIX, *argv]
    return argv, spec


def git(config: Config, args: list[str], timeout: int = 60) -> CommandResult:
    return run(["git", *args], config.repo_root, timeout)


def working_tree_state(config: Config) -> dict:
    status = git(config, ["status", "--porcelain"])
    dirty: list[str] = []
    untracked: list[str] = []
    for line in status.stdout.splitlines():
        if not line.strip():
            continue
        code, path = line[:2], line[3:].strip()
        (untracked if code.strip() == "??" else dirty).append(path)
    branch = git(config, ["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    return {"branch": branch, "modified": dirty, "untracked": untracked, "clean": not dirty and not untracked}
