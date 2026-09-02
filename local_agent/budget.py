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

# A token never loaded is saved on every later prompt, not once. Measured on a long Claude recette
# session: 14,339 one-shot tokens → ~390,000 billed effect (~27×). 25 is that order of magnitude.


def is_worth_delegating(raw: str, threshold: int = PASSTHROUGH_CHARS) -> bool:
    return len(raw.strip()) > threshold


def billed_chars(saved_chars: int, remaining_turns: int) -> int:
    """One-shot avoided characters scaled by how many later prompts they would have stayed in."""
    turns = int(remaining_turns)
    if turns <= 0:
        return 0
    return max(0, int(saved_chars)) * turns


def tokens_from_chars(chars: int) -> int:
    return max(0, int(chars) // 4)


def savings_footer(
    source_chars: int,
    returned_chars: int,
    remaining_turns: int,
    *,
    session_saved: int,
    session_compounded: int,
    lifetime_saved: int,
    lifetime_compounded: int,
) -> str:
    """Four counters: raw, visible, avoided, exposure. Exposure is not billed usage."""
    raw = tokens_from_chars(source_chars)
    visible = tokens_from_chars(returned_chars)
    avoided = max(0, raw - visible)
    lines = [
        f"\n\nRaw context processed locally: ~{raw} tokens",
        f"Claude-visible context returned: ~{visible} tokens",
        (
            f"Direct context avoided: ~{avoided} this call · "
            f"~{tokens_from_chars(session_saved)} this session · "
            f"~{tokens_from_chars(lifetime_saved)} lifetime"
        ),
    ]
    if remaining_turns <= 0:
        lines.append("Optional exposure estimate: disabled (LOCAL_AGENT_COMPOUND_TURNS=0).")
        return "\n".join(lines)
    exposure = tokens_from_chars(billed_chars(avoided * 4, remaining_turns))
    lines.append(
        f"Optional exposure estimate: ~{exposure} token-turns "
        f"(assumption: {remaining_turns} future turns). "
        "NOT equivalent to billed Claude tokens. cache and compaction apply."
    )
    return "\n".join(lines)


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
        summary=f"Raw output, no synthesis: {reason}.",
        stats=dict(stats or {}, delegue=False, brut_caracteres=len(raw.strip()))
    )
    report.details = f"{details}\n\n{raw.strip()}" if details else raw.strip()
    return report
