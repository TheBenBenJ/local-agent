"""Modifications mécaniques appliquées par le modèle local, sous contrôle git."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from . import prompts, shell
from .config import Config
from .files import SelectedFile, discover_files, read_text, resolve_path
from .mlx import MlxClient, MlxError
from .report import Report


LINT_FAILURE_MARKERS = ("Parse error", "syntax error", "Errors parsing", "Fatal error")

PATCH_DIR = Path.home() / ".local-agent" / "patches"
PATCH_RETENTION_SECONDS = 7 * 24 * 3600


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _prune_patches() -> None:
    try:
        for bundle in PATCH_DIR.glob("*.json"):
            if time.time() - bundle.stat().st_mtime > PATCH_RETENTION_SECONDS:
                bundle.unlink(missing_ok=True)
    except OSError:
        pass


def _persist_bundle(config: Config, task: str, proposals: list[dict]) -> str:
    """Fige la proposition sur disque : l'application n'écrira que ce contenu exact, vérifié par hash."""
    PATCH_DIR.mkdir(parents=True, exist_ok=True)
    _prune_patches()
    body = json.dumps(proposals, ensure_ascii=False, sort_keys=True)
    identifier = _sha256(body)[:12]
    payload = {
        "id": identifier,
        "integrity": _sha256(body),
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "repo": str(config.repo_root),
        "task": task,
        "files": proposals,
    }
    (PATCH_DIR / f"{identifier}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    return identifier


def apply_patch(config: Config, patch_id: str) -> Report:
    """Applique une proposition figée, en refusant toute source modifiée entre-temps."""
    bundle_path = PATCH_DIR / f"{patch_id}.json"
    if not bundle_path.is_file():
        raise ValueError(f"proposition inconnue : {patch_id}. Relancer un mode propose.")
    payload = json.loads(bundle_path.read_text(encoding="utf-8"))
    body = json.dumps(payload["files"], ensure_ascii=False, sort_keys=True)
    if _sha256(body) != payload["integrity"]:
        raise ValueError(f"proposition {patch_id} corrompue : intégrité invalide, ne pas appliquer.")
    if str(config.repo_root) != payload["repo"]:
        raise ValueError(
            f"proposition {patch_id} établie pour {payload['repo']}, pas pour {config.repo_root}."
        )

    changes: list[str] = []
    risks: list[str] = []
    touched: list[str] = []
    for entry in payload["files"]:
        target = (config.repo_root / entry["path"]).resolve()
        try:
            current, _ = read_text(target, config.fix_max_file_size * 3)
        except Exception as error:
            risks.append(f"{entry['path']} : illisible ({error}), non appliqué")
            continue
        if _sha256(current) != entry["before_sha256"]:
            risks.append(
                f"{entry['path']} : modifié depuis la proposition, non appliqué. Reproposer sur la version actuelle."
            )
            continue
        _write_atomic(target, entry["after"])
        valid, detail = _syntax_check(config, target)
        if not valid:
            _write_atomic(target, current)
            risks.append(f"{entry['path']} : écriture annulée, {detail}")
            continue
        changes.append(f"{entry['path']} : appliqué ({entry['reason']})")
        touched.append(entry["path"])

    diff_stat = shell.git(config, ["diff", "--stat", "--", *touched]).stdout.strip() if touched else ""
    bundle_path.unlink(missing_ok=True)
    return Report(
        title="Application de proposition",
        summary=(
            f"Proposition {patch_id} : {len(touched)} fichier(s) appliqué(s), "
            f"{len(risks)} refusé(s)."
        ),
        files=touched,
        changes=changes,
        risks=risks,
        stats={"patch_id": patch_id, "files_applied": len(touched)},
        details=diff_stat,
        next_actions=["Valider avec `git diff` avant tout commit"] if touched else [],
    )


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
    mode: str = "propose",
) -> Report:
    """Mode par défaut : propose. Rien n'est écrit tant que l'orchestrateur n'a pas relu le diff et
    demandé l'application exacte de la proposition, identifiée et vérifiée par hash."""
    if mode not in ("propose", "direct"):
        raise ValueError(f"mode inconnu : {mode}. Disponibles : propose, direct (apply passe par apply_patch).")
    propose = mode == "propose" or dry_run
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
    proposals: list[dict] = []

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
        if propose:
            normalized = candidate if candidate.endswith("\n") else candidate + "\n"
            changes.append(f"{item.relative} : {reason} (proposé, non écrit)")
            touched.append(item.relative)
            previews.append(_preview_diff(item.relative, original, candidate))
            proposals.append({
                "path": item.relative,
                "reason": reason,
                "before_sha256": _sha256(original),
                "after": normalized,
            })
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
        "mode": "propose" if propose else "direct",
    }
    patch_id = ""
    if proposals:
        patch_id = _persist_bundle(config, task, proposals)
        stats["patch_id"] = patch_id
    diff_stat = ""
    if touched and not propose:
        diff = shell.git(config, ["diff", "--stat", "--", *touched])
        diff_stat = diff.stdout.strip()
    elif previews:
        diff_stat = "\n".join(previews)

    report = Report(
        title="Correction locale",
        summary=(
            f"{len(touched)} fichier(s) {'proposé(s)' if propose else 'modifié(s)'} sur {len(files)} "
            f"examiné(s), {len(skipped)} ignoré(s), branche {tree['branch']}."
            + (f" Aucune écriture : proposition {patch_id} en attente d'application." if patch_id else "")
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
    elif patch_id:
        report.next_actions = [
            f"Relire le diff ci-dessus puis appliquer tel quel : local_fix mode=apply patch_id={patch_id}",
            "La proposition sera refusée si un fichier a changé entre-temps"
        ]
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
