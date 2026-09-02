#!/usr/bin/env python3
"""Vision locale : chemins fichier, pas de base64, OCR reste la source de vérité."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_agent.mlx import Completion, image_part, user_message  # noqa: E402
from local_agent.report import Report  # noqa: E402
from local_agent.vision import (  # noqa: E402
    page_needs_vision,
    select_pages,
    enrich,
)


def check(name: str, condition: bool) -> None:
    status = "OK" if condition else "KO"
    print(f"  {status}  {name}")
    if not condition:
        raise SystemExit(1)


class FakeVision:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def supports_vision(self) -> bool:
        return True

    def complete(self, prompt, system, **kwargs):
        self.calls.append({"prompt": prompt, "system": system, "images": kwargs.get("images")})
        return Completion(
            text='{"notes":["en-tête fusionné Animateur / salaire"],"ui":["filtre chantier"],'
            '"header_split":["Chantier","Animateur","salaire"]}'
        )


class Boom:
    def complete(self, *args, **kwargs):
        raise AssertionError("le modèle ne doit pas être appelé")


def main() -> None:
    check("chemin fichier, pas data-uri", "data:" not in image_part(Path("/tmp/a.png"))["image_url"]["url"])
    check("url est un chemin absolu", image_part(Path("/tmp/a.png"))["image_url"]["url"].startswith("/"))
    text_only = user_message("hello")
    check("sans image le content reste une chaîne", text_only["content"] == "hello")
    with_image = user_message("hello", [Path("/tmp/a.png")])
    check("avec image le content est une liste", isinstance(with_image["content"], list))
    check("premier bloc texte", with_image["content"][0]["type"] == "text")
    check("second bloc image_url", with_image["content"][1]["type"] == "image_url")

    check("table propre : pas de vision", not page_needs_vision([["A", "B"], ["1", "2"]], None))
    check("en-tête vide : vision", page_needs_vision([["A", ""], ["1", "2"]], None))
    check("pas de table : vision", page_needs_vision(None, None))
    check("tâche filtre : vision", page_needs_vision([["A", "B"]], "le filtre chantier"))
    check("tâche erreur : pas un hint layout", not page_needs_vision([["A", "B"]], "erreur"))

    pages = [
        {"path": Path("/tmp/a.png"), "table": [["A", "B"], ["1", "2"]], "label": "a"},
        {"path": Path("/tmp/b.png"), "table": [["A", ""], ["1", "2"]], "label": "b"},
    ]
    chosen = select_pages(pages, None)
    check("ne prend que la page à en-tête vide", [p["label"] for p in chosen] == ["b"])

    from local_agent.config import Config

    report = Report(title="t", summary="OCR inventory.")
    skipped = enrich(Config(vision=True), Boom(), report, pages, None)
    check("sans supports_vision : pas d'appel", skipped.stats.get("vision") == "unavailable")

    from dataclasses import replace

    disabled = enrich(replace(Config(), vision=False), FakeVision(), Report(title="t"), pages, None)
    check("LOCAL_AGENT_VISION=0 désactive", disabled.stats.get("vision") == "disabled")

    fake = FakeVision()
    applied = enrich(
        Config(vision=True),
        fake,
        Report(title="t", summary="OCR inventory.", findings=["row"]),
        [
            {
                "path": Path("/etc/hosts"),
                "table": [["A", ""], ["1", "2"]],
                "label": "shot",
                "excerpt": "| A |  |",
                "image_id": "",
            }
        ],
        "filtre chantier",
    )
    check("applique la vision si le modèle la déclare", applied.stats.get("vision") == "applied")
    check("un seul appel", len(fake.calls) == 1)
    check("passe le chemin, pas du base64", fake.calls[0]["images"][0] == Path("/etc/hosts"))
    check("OCR findings conservés", "row" in applied.findings)
    check("notes vision en tête", applied.findings[0].startswith("Local vision:"))
    check("n'écrase pas le résumé OCR", "OCR inventory" in applied.summary)

    print("tous les contrôles vision passent")


if __name__ == "__main__":
    main()
