"""Exécution de commandes locales, restreinte à une liste blanche non destructrice."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import Config

DOCKER_PREFIX = ["docker", "compose", "exec", "-T", "symfony"]

# Uniquement des commandes en lecture, aucune n'écrit en base applicative ni ne construit d'assets.
CHECK_COMMANDS: dict[str, dict[str, object]] = {
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


def build_check_command(kind: str, target: str | None, filter_expression: str | None) -> list[str]:
    spec = CHECK_COMMANDS.get(kind)
    if spec is None:
        raise ValueError(f"contrôle inconnu : {kind}. Disponibles : {', '.join(sorted(CHECK_COMMANDS))}")
    argv = list(spec["command"])  # type: ignore[arg-type]
    effective_target = target or spec.get("default_target")
    if spec.get("accepts_target") and effective_target:
        argv.append(str(effective_target))
    if spec.get("accepts_filter") and filter_expression:
        argv += ["--filter", filter_expression]
    if spec.get("in_container"):
        argv = [*DOCKER_PREFIX, *argv]
    return argv


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
