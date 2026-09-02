"""Parse source URIs so Claude sends a task + pointers, never the raw document."""

from __future__ import annotations

from dataclasses import dataclass

SCHEMES = ("repo", "image", "file", "log", "jira", "confluence", "docs", "data")
IMAGE_SUFFIX = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".heic", ".tif", ".tiff", ".bmp")


@dataclass
class Source:
    scheme: str
    reference: str
    raw: str


def parse_source(raw: str) -> Source:
    text = str(raw or "").strip()
    if not text:
        raise ValueError("empty source")
    if "://" not in text:
        lowered = text.lower()
        if lowered.endswith(IMAGE_SUFFIX):
            return Source("image", text, text)
        return Source("repo", text, text)
    scheme, rest = text.split("://", 1)
    scheme = scheme.lower()
    if scheme not in SCHEMES:
        raise ValueError(f"unsupported source scheme {scheme!r}. Use {', '.join(SCHEMES)}")
    reference = rest.strip() or "."
    if scheme in {"jira", "confluence"}:
        reference = reference.strip("/")
    return Source(scheme, reference, text)


def parse_sources(raw: list | None) -> list[Source]:
    items = [str(item) for item in (raw or []) if str(item).strip()]
    if not items:
        return [Source("repo", ".", "repo://.")]
    return [parse_source(item) for item in items]
