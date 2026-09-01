#!/usr/bin/env python3
"""Banc de latence : où passe le temps, phase par phase.

Un total ne dit pas quoi optimiser. Une recherche enchaîne deux appels au modèle (dérivation des motifs,
puis synthèse) autour d'opérations disque quasi gratuites : seule la décomposition montre lequel des deux
pèse, et donc s'il y a un levier.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOL_ROOT))
AGENT = str(TOOL_ROOT / "bin" / "local-agent")


def timed(function, *args, **kwargs):
    start = time.monotonic()
    result = function(*args, **kwargs)
    return result, time.monotonic() - start


def phases(repo: Path, query: str, path: str) -> dict[str, float]:
    """Chronomètre chaque étape d'une recherche, en appelant les modules directement."""
    os.environ["LOCAL_AGENT_REPO_ROOT"] = str(repo)
    from local_agent import prompts
    from local_agent.config import get_config
    from local_agent.files import grep, resolve_path
    from local_agent.mlx import MlxClient
    from local_agent.tasks import _select_patterns, _snippet_context

    config = get_config()
    client = MlxClient(config)
    target = resolve_path(config, path)

    (patterns, _), derivation = timed(_select_patterns, config, client, query, target, None)
    (matches, total), grepping = timed(grep, config, target, patterns, balance_by_file=True)
    snippets, windowing = timed(_snippet_context, config, matches, config.chunk_chars)

    prompt = f"Question : {query}\n\nExtraits :\n{snippets}\n\n" + prompts.JSON_CONTRACT
    _, synthesis = timed(client.complete, prompt, prompts.SYSTEM_ANALYST, temperature=0.0)

    return {
        "dérivation des motifs": derivation,
        "grep": grepping,
        "extraction des fenêtres": windowing,
        "synthèse": synthesis,
        "total": derivation + grepping + windowing + synthesis
    }


def runs(repo: Path, argv: list[str], tours: int) -> list[float]:
    durations = []
    for _ in range(tours):
        start = time.monotonic()
        subprocess.run(
            [AGENT, *argv],
            cwd=str(repo),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=900,
            check=False,
            stdin=subprocess.DEVNULL
        )
        durations.append(time.monotonic() - start)
    return durations


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=os.environ.get("LOCAL_AGENT_BENCH_REPO") or os.getcwd())
    parser.add_argument("--cases", default=str(TOOL_ROOT / "tests" / "cases.json"))
    parser.add_argument("--tours", type=int, default=3)
    options = parser.parse_args()

    repo = Path(options.repo).resolve()
    payload = json.loads(Path(options.cases).read_text())
    cases = payload["volume"]

    print(f"dépôt mesuré : {repo}")
    print(f"tours        : {options.tours}\n")
    print(f"{'tâche':<40} {'min':>7} {'médian':>8} {'max':>7}")
    print("-" * 66)
    for case in cases:
        durations = runs(repo, case["agent"], options.tours)
        print(
            f"{case['nom']:<40} {min(durations):>6.1f}s "
            f"{statistics.median(durations):>7.1f}s {max(durations):>6.1f}s"
        )

    first = cases[0]
    if first["agent"][0] == "search":
        query = first["agent"][1]
        path = first["agent"][3] if len(first["agent"]) > 3 else "."
        print(f"\nDécomposition d'une recherche : {query[:60]}")
        print("-" * 66)
        breakdown = phases(repo, query, path)
        total = breakdown.pop("total")
        for step, seconds in breakdown.items():
            share = round(100 * seconds / total) if total else 0
            print(f"  {step:<38} {seconds:>6.2f}s  {share:>3} %")
        print(f"  {'TOTAL':<38} {total:>6.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
