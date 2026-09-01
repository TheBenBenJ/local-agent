"""Arbitrage de frugalité : ne déléguer que ce que la délégation allège réellement.

Une synthèse produite par le modèle pèse en pratique entre 1 500 et 2 600 caractères. En dessous de ce
seuil, la preuve brute est à la fois moins coûteuse pour l'orchestrateur et exacte, là où la synthèse
introduit un risque d'omission. Mesures à l'appui dans `tests/bench.py` et `tests/bench_exactitude.py`.
"""

from __future__ import annotations

from .report import Report

# Coût plancher d'une synthèse : en deçà, l'appel au modèle est une dépense sans contrepartie.
PASSTHROUGH_CHARS = 2000

# Une énumération courte doit rester exhaustive : le modèle résume là où il faudrait lister.
PASSTHROUGH_MATCHES = 60

# Pour une revue ou une synthèse, le livrable est un commentaire et non le contenu : rendre le brut
# reporte le travail sur l'orchestrateur, ce qui ne vaut que si le contenu est plus court qu'une synthèse.
PASSTHROUGH_CONTENT_CHARS = 1200


def is_worth_delegating(raw: str, threshold: int = PASSTHROUGH_CHARS) -> bool:
    return len(raw.strip()) > threshold


def passthrough(
    title: str,
    raw: str,
    *,
    reason: str,
    stats: dict | None = None,
    details: str | None = None
) -> Report:
    """Rend la preuve brute sans appeler le modèle, en disant pourquoi."""
    report = Report(
        title=title,
        summary=f"Brut renvoyé sans synthèse : {reason}.",
        stats=dict(stats or {}, delegue=False, brut_caracteres=len(raw.strip()))
    )
    report.details = f"{details}\n\n{raw.strip()}" if details else raw.strip()
    return report
