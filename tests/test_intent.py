#!/usr/bin/env python3
"""Contrôles sans modèle : le raccourci d'énumération ne doit pas viser une déclaration d'usage."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_agent.tasks import (  # noqa: E402
    _is_enumeration,
    _is_usage_query,
    _salient_terms,
    _NAMED_TYPE_DECL,
)


def check(name: str, condition: bool) -> None:
    status = "OK" if condition else "KO"
    print(f"  {status}  {name}")
    if not condition:
        raise SystemExit(1)


def main() -> None:
    usage = "où PaieService écrit les champs H+"
    listing = "quelles classes portent l'attribut Referencable"
    definition = "où est définie la classe Server"

    check("usage : énumération (où + nom de classe)", _is_enumeration(usage))
    check("usage : intent d'écriture détecté", _is_usage_query(usage))
    check("usage : PaieService est salient", _salient_terms(usage) == ["PaieService"])
    check("listing attribut : pas un usage", not _is_usage_query(listing))
    check("listing attribut : énumération", _is_enumeration(listing))
    check("définition de classe : pas un usage", not _is_usage_query(definition))
    check("filtre déclaration class Foo", bool(_NAMED_TYPE_DECL.search(r"class PaieService")))
    check("filtre ignore un identifiant nu", not _NAMED_TYPE_DECL.search("PaieService"))
    print("tous les contrôles d'intent passent")


if __name__ == "__main__":
    main()
