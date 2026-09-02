"""Interface en ligne de commande du local-agent."""

from __future__ import annotations

import argparse
import json
import sys

from . import agent, compare, doctor, edit, ocr, store, tasks
from .config import get_config
from .files import GuardrailError
from .mlx import MlxClient, MlxError
from .report import Report, render_json, render_markdown


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="local-agent",
        description="Réduit le contexte brut avant qu'il n'entre chez l'orchestrateur.",
    )
    parser.add_argument("--json", action="store_true", help="sortie JSON au lieu du markdown")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("ping", help="teste la connexion au serveur MLX")
    subparsers.add_parser("config", help="affiche la configuration effective")

    search = subparsers.add_parser("search", help="recherche guidée dans le code")
    search.add_argument("query")
    search.add_argument("--path", default=".")
    search.add_argument("--glob", action="append", dest="globs")

    for name, help_text in (
        ("review", "première passe de revue de code"),
        ("summarize", "résume le rôle des fichiers d'un répertoire"),
        ("duplicates", "détecte les implémentations dupliquées"),
        ("inspect", "analyse libre pilotée par --task"),
    ):
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("path", nargs="?", default=".")
        command.add_argument("--task", default=None)
        command.add_argument("--glob", action="append", dest="globs")
        command.add_argument("--max-files", type=int, default=None)

    logs = subparsers.add_parser("logs", help="analyse un fichier ou répertoire de logs volumineux")
    logs.add_argument("path")
    logs.add_argument("--task", default=None)
    logs.add_argument("--pattern", action="append", dest="patterns")

    fix = subparsers.add_parser("fix", help="propose une correction mécanique (mode propose par défaut)")
    fix.add_argument("path")
    fix.add_argument("--task", required=True)
    fix.add_argument("--mode", choices=["propose", "direct"], default="propose")
    fix.add_argument("--glob", action="append", dest="globs")
    fix.add_argument("--max-files", type=int, default=None)
    fix.add_argument("--dry-run", action="store_true")
    fix.add_argument("--allow-dirty", action="store_true")

    apply_parser = subparsers.add_parser("apply", help="applique une proposition figée par fix")
    apply_parser.add_argument("patch_id")

    check = subparsers.add_parser("check", help="exécute un contrôle projet et en synthétise la sortie")
    check.add_argument("kind", nargs="?", default=None, help="nom du contrôle, défaut : le premier disponible")
    check.add_argument("--target", default=None)
    check.add_argument("--filter", dest="filter_expression", default=None)

    diff = subparsers.add_parser("diff", help="revue d'un diff git avec proposition de message de commit")
    diff.add_argument("scope", nargs="?", default="worktree", choices=sorted(tasks.DIFF_SCOPES))
    diff.add_argument("--base", default=None, help="branche de base pour scope=branch")
    diff.add_argument("--task", default=None)

    image = subparsers.add_parser("image", help="OCR local d'une capture, sans modèle")
    image.add_argument("paths", nargs="+", help="chemins d'images, absolus autorisés")
    image.add_argument("--task", default=None, help="filtre de lignes (sous-chaîne, sans modèle)")

    crop = subparsers.add_parser("image-crop", help="extrait une région OCR (id rendu par image)")
    crop.add_argument("id", help="identifiant de région, ex. a832b1c4-R1")

    compare_parser = subparsers.add_parser("image-compare", help="compare deux captures (hash + OCR + pixel)")
    compare_parser.add_argument("reference")
    compare_parser.add_argument("current")

    task_parser = subparsers.add_parser("task", help="mission locale : sources + boucle d'outils")
    task_parser.add_argument("task")
    task_parser.add_argument("--source", action="append", dest="sources")
    task_parser.add_argument("--path", default=None)
    task_parser.add_argument("--autonomy", default=None, choices=["read_only", "patch", "safe", "auto"])
    task_parser.add_argument("--output-budget", type=int, default=None)
    task_parser.add_argument("--local-context-budget", type=int, default=None)
    task_parser.add_argument("--risk-level", default=None, choices=["LOW", "MEDIUM", "HIGH"])

    expand = subparsers.add_parser("expand", help="détail d'une preuve (CODE-E12, a832-R1)")
    expand.add_argument("ids", nargs="+")

    subparsers.add_parser("stats", help="tableau de bord des métriques locales")
    subparsers.add_parser("doctor", help="diagnostique MCP, MLX, OCR, store")
    session = subparsers.add_parser("session", help="affiche ou renouvelle l'id de session locale")
    session.add_argument("--new", action="store_true", help="force a new session id")

    bench = subparsers.add_parser("benchmark", help="mesure contexte, latence et justesse sur des taches reelles")
    bench.add_argument("kind", nargs="?", default="all", help="repo, logs, vision, patch, jira, jira-live, confluence-live, live, tests, cache, sessions, transcript, day, all")
    bench.add_argument("target", nargs="?", default=None, help="jsonl path (transcript) or folder (day)")
    bench.add_argument("--no-llm", action="store_true", help="baselines only, skip local_task")

    eval_parser = subparsers.add_parser("eval", help="note la justesse des taches du banc (meme manifest)")
    eval_parser.add_argument("kind", nargs="?", default="all")

    return parser


def run(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    config = get_config()
    client = MlxClient(config)

    try:
        report = _dispatch(arguments, config, client)
    except (GuardrailError, MlxError, ValueError) as error:
        print(f"local-agent : {error}", file=sys.stderr)
        return 1

    if isinstance(report, dict):
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print(render_json(report, config) if arguments.json else render_markdown(report, config))
    return 1 if report.errors else 0


def _dispatch(arguments: argparse.Namespace, config, client: MlxClient) -> Report | dict:
    command = arguments.command
    if command == "config":
        return config.as_summary()
    if command == "ping":
        from .version import describe

        payload = client.ping()
        if isinstance(payload, dict):
            payload = {**payload, "server": describe()}
        return payload
    if command == "search":
        return tasks.search(config, client, arguments.query, arguments.path, arguments.globs)
    if command in ("review", "summarize", "duplicates", "inspect"):
        return tasks.analyze(
            config,
            client,
            arguments.path,
            arguments.task,
            mode=command,
            globs=arguments.globs,
            max_files=arguments.max_files,
        )
    if command == "logs":
        return tasks.analyze_logs(config, client, arguments.path, arguments.task, arguments.patterns)
    if command == "fix":
        return edit.fix(
            config,
            client,
            arguments.path,
            arguments.task,
            globs=arguments.globs,
            max_files=arguments.max_files,
            dry_run=arguments.dry_run,
            allow_dirty=arguments.allow_dirty,
            mode=arguments.mode,
        )
    if command == "apply":
        return edit.apply_patch(config, arguments.patch_id)
    if command == "check":
        return tasks.check(config, client, arguments.kind, arguments.target, arguments.filter_expression)
    if command == "diff":
        return tasks.diff_review(config, client, scope=arguments.scope, base=arguments.base, task=arguments.task)
    if command == "image":
        paths = list(arguments.paths)
        return ocr.read_images(config, paths[0], paths[1:] or None, arguments.task, client=client)
    if command == "image-crop":
        report, _crop = ocr.crop_region(config, arguments.id)
        return report
    if command == "image-compare":
        return compare.compare_images(config, arguments.reference, arguments.current, client=client)
    if command == "task":
        return agent.run_task(
            config,
            client,
            arguments.task,
            sources=arguments.sources,
            path=arguments.path,
            autonomy=arguments.autonomy,
            output_budget=arguments.output_budget,
            local_context_budget=arguments.local_context_budget,
            risk_level=arguments.risk_level,
        )
    if command == "expand":
        payload = [store.expand(item, config=config) for item in arguments.ids]
        return payload[0] if len(payload) == 1 else payload
    if command == "stats":
        return store.Store().stats()
    if command == "session":
        if arguments.new:
            return {"session_id": store.new_session()}
        return {"session_id": store.current_session()}
    if command == "doctor":
        return doctor.check(config, client)
    if command == "benchmark":
        from . import benchmark as bench_mod

        return bench_mod.run(
            config,
            client,
            arguments.kind,
            no_llm=bool(arguments.no_llm),
            target=getattr(arguments, "target", None),
        )
    if command == "eval":
        from . import benchmark as bench_mod

        return bench_mod.run(config, client, arguments.kind, eval_only=True)
    raise ValueError(f"commande inconnue : {command}")


def main() -> None:
    sys.exit(run())
