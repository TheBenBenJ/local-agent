#!/usr/bin/env python3
"""Banc d'essai : volume entrant dans le contexte de l'orchestrateur, et latence.

Pour chaque tâche, deux chemins sont chiffrés :
  - local-agent : le nombre de caractères que l'outil renvoie, seul volume reçu.
  - à la main   : ce que l'orchestrateur aurait dû charger pour répondre lui-même.

Le second chemin est modélisé au plus favorable pour lui : pour une recherche, on suppose un grep suivi
de la lecture des seuls fichiers les plus denses, pas de tout le répertoire. Pour un log volumineux, les
erreurs greppées plus la fin du fichier, personne ne lisant deux cents mégaoctets de log.

Les cas vivent dans un fichier JSON (`tests/cases.json` par défaut), pour que le banc ne dépende pas
d'un projet particulier.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parent.parent
AGENT = str(TOOL_ROOT / "bin" / "local-agent")
DEFAULT_CASES = TOOL_ROOT / "tests" / "cases.json"


def ensure_bench_log(repo: Path) -> Path:
    """Génère un log >1 Mo dans var/ (gitignore) pour le cas de compression, sans le versionner."""
    path = repo / "var" / "bench.log"
    if path.is_file() and path.stat().st_size >= 1_000_000:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for index in range(25_000):
            handle.write(f"2026-09-02 INFO worker={index} {'x' * 60}\n")
            if index % 400 == 0:
                handle.write(f"2026-09-02 ERROR Uncaught Exception at line {index}\n")
    return path


def run(command: list[str], cwd: Path, timeout: int = 900) -> tuple[str, float, int]:
    start = time.monotonic()
    process = subprocess.run(
        command,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        check=False,
        stdin=subprocess.DEVNULL
    )
    return process.stdout, time.monotonic() - start, process.returncode


def grep_cost(repo: Path, patterns: list[str], path: str, opened: int = 3) -> int:
    """Coût d'un grep suivi de l'ouverture des fichiers les plus denses."""
    args = ["rg", "--line-number", "--no-heading", "--with-filename", "--ignore-case", "--max-columns", "220"]
    for pattern in patterns:
        args += ["-e", pattern]
    args.append(path)
    output, _, _ = run(args, repo, timeout=120)
    counts: dict[str, int] = {}
    for line in output.splitlines():
        name = line.split(":", 1)[0]
        counts[name] = counts.get(name, 0) + 1
    top = sorted(counts.items(), key=lambda item: item[1], reverse=True)[:opened]
    read = 0
    for name, _ in top:
        candidate = repo / name
        if candidate.is_file():
            read += candidate.stat().st_size
    return len(output) + read


def tree_cost(repo: Path, path: str, globs: tuple[str, ...] = ("*.php", "*.py", "*.ts", "*.yaml", "*.yml", "*.md")) -> int:
    target = repo / path
    if target.is_file():
        return target.stat().st_size
    total = 0
    for item in target.rglob("*"):
        if item.is_file() and any(item.match(pattern) for pattern in globs):
            total += item.stat().st_size
    return total


def command_cost(repo: Path, command: list[str]) -> int:
    output, _, _ = run(command, repo, timeout=900)
    return len(output)


def log_cost(repo: Path, path: str, lines: int = 300) -> int:
    """Coût réaliste face à un log volumineux : les erreurs greppées, plus la fin du fichier."""
    errors, _, _ = run(
        ["rg", "--no-heading", "--with-filename", "--line-number", "-e", "ERROR", "-e", "CRITICAL", "-e", "EMERGENCY", path],
        repo,
        timeout=300
    )
    kept = "\n".join(errors.splitlines()[-lines:])
    tail, _, _ = run(["tail", "-n", str(lines), path], repo, timeout=120)
    return len(kept) + len(tail)


def baseline_cost(repo: Path, spec: dict) -> int:
    kind = spec.get("type")
    if kind == "grep":
        return grep_cost(repo, spec["patterns"], spec["path"])
    if kind == "tree":
        return tree_cost(repo, spec["path"])
    if kind == "log":
        return log_cost(repo, spec["path"])
    if kind == "command":
        return command_cost(repo, spec["command"])
    raise ValueError(f"type de référence inconnu : {kind}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repo",
        default=os.environ.get("LOCAL_AGENT_BENCH_REPO") or os.getcwd(),
        help="dépôt à mesurer (défaut : LOCAL_AGENT_BENCH_REPO, sinon répertoire courant)"
    )
    parser.add_argument("--cases", default=str(DEFAULT_CASES), help="fichier JSON des cas de mesure")
    parser.add_argument("--json", action="store_true")
    options = parser.parse_args()

    repo = Path(options.repo).resolve()
    ensure_bench_log(repo)
    cases = json.loads(Path(options.cases).read_text())["volume"]

    results = []
    for case in cases:
        output, seconds, code = run([AGENT, *case["agent"]], repo)
        agent_size = len(output.strip())
        baseline_size = baseline_cost(repo, case["baseline"])
        results.append({
            "nom": case["nom"],
            "agent_caracteres": agent_size,
            "manuel_caracteres": baseline_size,
            "facteur": round(baseline_size / agent_size, 1) if agent_size else 0.0,
            "secondes": round(seconds, 1),
            "code_sortie": code
        })

    if options.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0

    print(f"dépôt mesuré : {repo}")
    print(f"cas          : {options.cases}\n")
    print(f"{'tâche':<38} {'local-agent':>12} {'à la main':>12} {'facteur':>9} {'durée':>7}")
    print("-" * 82)
    for row in results:
        print(
            f"{row['nom']:<38} {row['agent_caracteres']:>12} {row['manuel_caracteres']:>12} "
            f"{row['facteur']:>8}x {row['secondes']:>6}s"
        )
    total_agent = sum(row["agent_caracteres"] for row in results)
    total_manual = sum(row["manuel_caracteres"] for row in results)
    print("-" * 82)
    print(
        f"{'TOTAL':<38} {total_agent:>12} {total_manual:>12} "
        f"{round(total_manual / total_agent, 1) if total_agent else 0:>8}x "
        f"{round(sum(row['secondes'] for row in results), 1):>6}s"
    )
    print(f"\nen tokens (~4 car.) : local-agent {total_agent // 4}, à la main {total_manual // 4}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
