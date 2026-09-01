#!/usr/bin/env python3
"""Banc d'exactitude : le volume économisé ne vaut rien si la réponse est fausse.

Chaque question porte une vérité terrain établie au grep. La notation est un proxy par mots clés, pas une
évaluation sémantique : elle attrape les échecs francs (conclusion inverse, entité manquante), pas les
nuances. Un `attendu` manquant vaut échec, un `interdit` présent aussi.

Les `interdits` doivent rester des formulations sans équivoque. Une phrase trop générale produit de faux
négatifs : « aucun trait ne combine UUID et timestamps » est une réponse juste et nuancée.

Les cas vivent dans un fichier JSON (`tests/cases.json` par défaut), pour que le banc ne dépende pas d'un
projet particulier.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

AGENT = str(Path.home() / ".local-agent" / "bin" / "local-agent")
DEFAULT_CASES = Path(__file__).resolve().parent / "cases.json"


def fold(text: str) -> str:
    stripped = unicodedata.normalize("NFKD", text.lower())
    return "".join(char for char in stripped if not unicodedata.combining(char))


def verdict_for(answer: str, question: dict) -> tuple[str, str]:
    missing = [item for item in question["attendus"] if fold(item) not in answer]
    banned = [item for item in question.get("interdits") or [] if fold(item) in answer]

    if banned:
        return "FAUX", "interdit: " + banned[0][:16]
    if not missing:
        return "JUSTE", "-"
    if len(missing) < len(question["attendus"]):
        return "PARTIEL", ",".join(missing)[:27]
    return "FAUX", ",".join(missing)[:27]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repo",
        default=os.environ.get("LOCAL_AGENT_BENCH_REPO") or os.getcwd(),
        help="dépôt à mesurer (défaut : LOCAL_AGENT_BENCH_REPO, sinon répertoire courant)"
    )
    parser.add_argument("--cases", default=str(DEFAULT_CASES), help="fichier JSON des cas de mesure")
    parser.add_argument("--tours", type=int, default=1, help="répétitions, pour jauger la stabilité")
    options = parser.parse_args()

    repo = Path(options.repo).resolve()
    questions = json.loads(Path(options.cases).read_text())["exactitude"]

    print(f"dépôt mesuré : {repo}")
    print(f"cas          : {options.cases}\n")
    print(f"{'question':<32} {'tour':>4} {'verdict':<9} {'manquants':<28} {'car.':>6} {'durée':>7}")
    print("-" * 92)

    scores = []
    for question in questions:
        for tour in range(1, options.tours + 1):
            start = time.monotonic()
            process = subprocess.run(
                [AGENT, *question["commande"]],
                cwd=str(repo),
                capture_output=True,
                text=True,
                errors="replace",
                timeout=600,
                check=False,
                stdin=subprocess.DEVNULL
            )
            seconds = time.monotonic() - start
            verdict, detail = verdict_for(fold(process.stdout), question)
            scores.append(verdict)
            print(
                f"{question['nom']:<32} {tour:>4} {verdict:<9} {detail:<28} "
                f"{len(process.stdout.strip()):>6} {seconds:>6.1f}s"
            )

    print("-" * 92)
    total = len(scores)
    for verdict in ("JUSTE", "PARTIEL", "FAUX"):
        count = scores.count(verdict)
        print(f"{verdict:<8} {count}/{total}  ({round(100 * count / total) if total else 0} %)")
    print("\nVérités terrain :")
    for question in questions:
        print(f"  - {question['nom']} : {question['verite']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
