"""Modifications mécaniques appliquées par le modèle local, sous contrôle git."""

from __future__ import annotations

import difflib
import os
import tempfile
from pathlib import Path

from . import prompts, shell
from .config import Config
from .files import SelectedFile, discover_files, read_text, resolve_path
from .mlx import MlxClient, MlxError
from .report import Report


LINT_FAILURE_MARKERS = ("Parse error", "syntax error", "Errors parsing", "Fatal error")


def _syntax_check(config: Config, path: Path) -> tuple[bool, str]:
    """Contrôle syntaxique quand un vérificateur est disponible, sinon abstention explicite.

    Un Docker éteint ne doit jamais faire passer une écriture valide pour une erreur de syntaxe.
    """
    if path.suffix != ".php":
        return True, "non vérifié"
    relative = path.relative_to(config.repo_root)
    result = shell.run([*shell.DOCKER_PREFIX, "php", "-l", str(relative)], config.repo_root, 60)
    if result.exit_code == 0:
        return True, "php -l ok"
    output = result.output.strip()
    if any(marker in output for marker in LINT_FAILURE_MARKERS):
        return False, output.splitlines()[0][:200]
    return True, "php -l indisponible"


def _preview_diff(relative: str, original: str, candidate: str, max_lines: int = 60) -> str:
    """Rend visible une proposition en dry-run, qu'aucun `git diff` ne peut montrer faute d'écriture."""
    lines = list(
        difflib.unified_diff(
            original.splitlines(),
            candidate.splitlines(),
            fromfile=relative,
            tofile=f"{relative} (proposé)",
            lineterm="",
            n=2
        )
    )
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"... diff tronqué, {len(lines) - max_lines} lignes supplémentaires"]
    return "\n".join(lines)


def _write_atomic(path: Path, content: str) -> None:
    directory = path.parent
    mode = path.stat().st_mode
    handle, temporary = tempfile.mkstemp(dir=str(directory), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.chmod(temporary, mode & 0o777)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _plausible(original: str, candidate: str) -> tuple[bool, str]:
    if not candidate.strip():
        return False, "contenu vide"
    if candidate.strip() == original.strip():
        return False, "contenu identique"
    original_first = original.lstrip().splitlines()[0] if original.strip() else ""
    if original_first.startswith("<?php") and not candidate.lstrip().startswith("<?php"):
        return False, "ouverture <?php perdue"
    if len(candidate) < len(original) * 0.5:
        return False, f"contenu réduit de plus de moitié ({len(original)} -> {len(candidate)})"
    if len(candidate) > len(original) * 2.5:
        return False, f"contenu plus que doublé ({len(original)} -> {len(candidate)})"
    return True, "vraisemblable"


def fix(
    config: Config,
    client: MlxClient,
    path: str | None,
    task: str,
    *,
    globs: list[str] | None = None,
    max_files: int | None = None,
    dry_run: bool = False,
    allow_dirty: bool = False,
) -> Report:
    target = resolve_path(config, path)
    tree = shell.working_tree_state(config)
    protected = set(tree["modified"]) | set(tree["untracked"])

    limit = max_files or min(config.max_files, 12)
    files, total = discover_files(config, target, globs=globs, max_files=limit)
    if not files:
        return Report(
            title="Correction locale",
            summary=f"Aucun fichier éligible sous {path or '.'}.",
            next_actions=["Vérifier le chemin ou les filtres --glob"],
        )

    changes: list[str] = []
    skipped: list[str] = []
    risks: list[str] = []
    previews: list[str] = []
    errors: list[str] = []
    touched: list[str] = []

    for item in files:
        verdict = _screen(config, item, protected, allow_dirty)
        if verdict:
            skipped.append(verdict)
            continue
        try:
            original, truncated = read_text(item.path, config.fix_max_file_size)
        except Exception as error:
            skipped.append(f"{item.relative} : illisible ({error})")
            continue
        if truncated:
            skipped.append(f"{item.relative} : trop volumineux pour une réécriture sûre")
            continue

        numbered = "\n".join(f"{index}| {line}" for index, line in enumerate(original.splitlines(), start=1))
        prompt = (
            f"Consigne : {task}\n"
            f"Fichier : {item.relative}\n\n"
            f"Contenu actuel (numéros de ligne à retirer dans ta réponse) :\n{numbered}\n\n"
            + prompts.FILE_ENVELOPE_CONTRACT
        )
        try:
            completion = client.complete(
                prompt,
                prompts.SYSTEM_EDITOR,
                max_tokens=max(config.max_completion_tokens, len(original) // 3 + 500),
            )
        except MlxError as error:
            errors.append(f"{item.relative} : {error}")
            continue

        changed, reason, candidate = prompts.parse_file_envelope(completion.text)
        if not changed or candidate is None:
            skipped.append(f"{item.relative} : inchangé ({reason})")
            continue

        ok, why = _plausible(original, candidate)
        if not ok:
            risks.append(f"{item.relative} : proposition rejetée ({why})")
            continue
        if dry_run:
            changes.append(f"{item.relative} : {reason} (dry-run, non écrit)")
            touched.append(item.relative)
            previews.append(_preview_diff(item.relative, original, candidate))
            continue

        _write_atomic(item.path, candidate if candidate.endswith("\n") else candidate + "\n")
        valid, detail = _syntax_check(config, item.path)
        if not valid:
            _write_atomic(item.path, original)
            risks.append(f"{item.relative} : écriture annulée, {detail}")
            continue
        changes.append(f"{item.relative} : {reason}")
        touched.append(item.relative)

    stats: dict[str, object] = {
        "branch": tree["branch"],
        "files_examined": len(files),
        "files_found": total,
        "files_changed": len(touched),
        "dry_run": dry_run,
    }
    diff_stat = ""
    if touched and not dry_run:
        diff = shell.git(config, ["diff", "--stat", "--", *touched])
        diff_stat = diff.stdout.strip()
    elif previews:
        diff_stat = "\n".join(previews)

    report = Report(
        title="Correction locale",
        summary=(
            f"{len(touched)} fichier(s) modifié(s) sur {len(files)} examiné(s), "
            f"{len(skipped)} ignoré(s), branche {tree['branch']}."
            + (" Aucune écriture (dry-run)." if dry_run else "")
        ),
        files=touched,
        changes=changes,
        risks=risks,
        errors=errors,
        stats=stats,
        details=diff_stat,
    )
    if skipped:
        report.findings = skipped[:12]
    if not touched:
        report.next_actions = []
    elif dry_run:
        report.next_actions = ["Relire le diff proposé, puis relancer sans --dry-run pour appliquer"]
    else:
        report.next_actions = ["Valider avec `git diff` avant tout commit"]
    return report


def _screen(config: Config, item: SelectedFile, protected: set[str], allow_dirty: bool) -> str | None:
    if item.size > config.fix_max_file_size:
        return f"{item.relative} : au-delà de LOCAL_AGENT_FIX_MAX_FILE_SIZE"
    if item.relative in protected and not allow_dirty:
        return f"{item.relative} : modifications utilisateur non committées, préservées"
    tracked = shell.git(config, ["ls-files", "--error-unmatch", item.relative], timeout=20)
    if tracked.exit_code != 0 and not allow_dirty:
        return f"{item.relative} : non suivi par git, préservé"
    return None
