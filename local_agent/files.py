"""Découverte de fichiers, garde-fous et découpage en lots pour le modèle local."""

from __future__ import annotations

import fnmatch
import re
import subprocess
from collections import Counter, deque
from dataclasses import dataclass
from pathlib import Path

from .config import Config

DENIED_DIRECTORIES = {
    ".git",
    ".idea",
    ".yarn",
    "node_modules",
    "vendor",
    "var",
    "temp",
    "_db",
    "coverage",
    "build",
    "dist",
    "__pycache__",
    ".pytest_cache",
    ".playwright-mcp",
}

DENIED_PATTERNS = (
    ".env",
    ".env.*",
    "*.env",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.crt",
    "*.jks",
    "id_rsa*",
    "id_ed25519*",
    "*.backup",
    "*.dump",
    "*.sql.gz",
    "*.lock",
    "*credential*",
    "*secret*",
    "*.min.js",
    "*.min.css",
    "*.map",
)

BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svgz", ".pdf", ".zip", ".gz", ".tar",
    ".bz2", ".xz", ".7z", ".woff", ".woff2", ".ttf", ".eot", ".otf", ".mp3", ".mp4", ".mov",
    ".so", ".dylib", ".dll", ".exe", ".bin", ".class", ".jar", ".pyc", ".db", ".sqlite",
    ".xlsx", ".xls", ".docx", ".doc", ".pptx",
}


class GuardrailError(RuntimeError):
    pass


@dataclass
class SelectedFile:
    path: Path
    relative: str
    size: int


def ensure_usable_root(config: Config) -> None:
    """Refuse de travailler hors d'un dépôt git, pour ne jamais parcourir un répertoire personnel entier."""
    root = config.repo_root
    if root == Path.home():
        raise GuardrailError(
            "racine de travail égale au répertoire personnel : le client n'a pas été lancé depuis un "
            "dépôt. Passer l'argument repo, ou relancer le client depuis le dépôt visé."
        )
    if not (root / ".git").exists():
        raise GuardrailError(
            f"{root} n'est pas un dépôt git. Passer l'argument repo, ou relancer depuis un dépôt."
        )


def resolve_path(config: Config, raw: str | None) -> Path:
    ensure_usable_root(config)
    root = config.repo_root
    candidate = Path(raw or ".")
    resolved = (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise GuardrailError(f"chemin hors du dépôt refusé : {resolved}") from error
    if not resolved.exists():
        raise GuardrailError(f"chemin inexistant : {resolved}")
    # Un fichier sensible désigné explicitement doit être refusé, pas silencieusement ignoré.
    if resolved.is_file() and any(fnmatch.fnmatch(resolved.name, pattern) for pattern in DENIED_PATTERNS):
        raise GuardrailError(f"fichier sensible ou non pertinent refusé : {relative_to_root(config, resolved)}")
    return resolved


def relative_to_root(config: Config, path: Path) -> str:
    try:
        return str(path.relative_to(config.repo_root))
    except ValueError:
        return str(path)


def unlocked_directories(config: Config, target: Path) -> frozenset[str]:
    """Un répertoire normalement exclu redevient lisible quand la cible le désigne explicitement."""
    relative = relative_to_root(config, target)
    return frozenset(part for part in Path(relative).parts if part in DENIED_DIRECTORIES)


def is_git_ignored(config: Config, target: Path) -> bool:
    process = subprocess.run(
        ["git", "check-ignore", "-q", str(target)],
        cwd=str(config.repo_root),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    return process.returncode == 0


def is_denied(
    relative: str,
    *,
    allow_sensitive: bool = False,
    unlocked: frozenset[str] = frozenset(),
) -> bool:
    parts = Path(relative).parts
    if any(part in DENIED_DIRECTORIES and part not in unlocked for part in parts):
        return True
    name = Path(relative).name
    if Path(name).suffix.lower() in BINARY_EXTENSIONS:
        return True
    if allow_sensitive:
        return False
    return any(fnmatch.fnmatch(name, pattern) for pattern in DENIED_PATTERNS)


def _run_ripgrep(args: list[str], cwd: Path, timeout: int = 60, max_bytes: int = 8_000_000) -> list[str]:
    """Exécute ripgrep en bornant la sortie lue, pour ne jamais charger un log entier en mémoire.

    stdin est neutralisé : hérité, ripgrep le prendrait pour sa source à fouiller et consommerait
    le flux JSON-RPC du serveur MCP.
    """
    process = subprocess.Popen(
        ["rg", *args],
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        raise GuardrailError(f"ripgrep interrompu après {timeout}s") from None
    if process.returncode not in (0, 1):
        raise GuardrailError(f"ripgrep a échoué : {stderr.strip()[:300]}")
    return [line for line in stdout[:max_bytes].splitlines() if line]


def discover_files(
    config: Config,
    target: Path,
    *,
    globs: list[str] | None = None,
    max_files: int | None = None,
    allow_sensitive: bool = False,
) -> tuple[list[SelectedFile], int]:
    """Liste les fichiers candidats via ripgrep, qui respecte déjà .gitignore."""
    limit = max_files or config.max_files
    unlocked = unlocked_directories(config, target)
    if target.is_file():
        candidates = [target]
    else:
        args = ["--files", "--no-messages"]
        if is_git_ignored(config, target):
            args.append("--no-ignore-vcs")
        for pattern in globs or []:
            args += ["--glob", pattern]
        for directory in sorted(DENIED_DIRECTORIES - unlocked):
            args += ["--glob", f"!{directory}/**"]
        args.append(".")
        names = _run_ripgrep(args, target)
        candidates = [target / name for name in names]

    selected: list[SelectedFile] = []
    for path in candidates:
        relative = relative_to_root(config, path)
        if is_denied(relative, allow_sensitive=allow_sensitive, unlocked=unlocked):
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size == 0:
            continue
        selected.append(SelectedFile(path=path, relative=relative, size=size))

    total = len(selected)
    selected.sort(key=lambda item: item.relative)
    return selected[:limit], total


def grep(
    config: Config,
    target: Path,
    patterns: list[str],
    *,
    globs: list[str] | None = None,
    max_matches: int | None = None,
    ignore_case: bool = True,
    context: int = 0,
    max_count_per_file: int | None = None,
    balance_by_file: bool = False,
) -> tuple[list[dict], int]:
    """Recherche textuelle native, retournée sous forme de correspondances structurées."""
    limit = max_matches or config.max_matches
    unlocked = unlocked_directories(config, target)
    args = [
        "--line-number",
        "--no-heading",
        "--with-filename",
        "--no-messages",
        "--max-columns",
        "220",
        "--max-columns-preview",
    ]
    if ignore_case:
        args.append("--ignore-case")
    if context:
        args += ["--context", str(context)]
    if max_count_per_file:
        args += ["--max-count", str(max_count_per_file)]
    if is_git_ignored(config, target):
        args.append("--no-ignore-vcs")
    for pattern in patterns:
        args += ["--regexp", pattern]
    for pattern in globs or []:
        args += ["--glob", pattern]
    for directory in sorted(DENIED_DIRECTORIES - unlocked):
        args += ["--glob", f"!{directory}/**"]

    search_root = target if target.is_dir() else target.parent
    args.append(target.name if target.is_file() else ".")
    lines = _run_ripgrep(args, search_root, timeout=120)

    matches: list[dict] = []
    for line in lines:
        pieces = line.split(":", 2)
        if len(pieces) < 3:
            continue
        file_part, line_no, text = pieces
        path = (search_root / file_part).resolve()
        relative = relative_to_root(config, path)
        if is_denied(relative, unlocked=unlocked):
            continue
        if not line_no.isdigit():
            continue
        matches.append({"file": relative, "line": int(line_no), "text": text.strip()[:200]})

    total = len(matches)
    # Comptage avant troncature : c'est le volume réel par fichier qui hiérarchise la pertinence.
    per_file = Counter(match["file"] for match in matches)
    declaring = {match["file"] for match in matches if DECLARATION.match(match["text"])}
    for match in matches:
        name = match["file"]
        match["file_hits"] = per_file[name]
        match["file_score"] = per_file[name] + (DECLARATION_BONUS if name in declaring else 0)

    if total <= limit:
        return matches, total
    return (balanced_sample(matches, limit) if balance_by_file else matches[:limit]), total


BOILERPLATE_PREFIXES = ("use ", "namespace ", "//", "/*", "*", "import ", "require ")

DECLARATION = re.compile(
    r"^\s*(?:final\s+|abstract\s+|readonly\s+)*(?:trait|class|interface|enum|function|const)\s",
)
# Une définition vaut plus que la densité : un fichier bavard écraserait sinon la ligne décisive.
DECLARATION_BONUS = 500


def _drop_boilerplate(entries: list[dict]) -> list[dict]:
    """Écarte imports et commentaires, qui saturent le début des fichiers sans jamais porter la réponse."""
    useful = [entry for entry in entries if not entry["text"].lstrip().startswith(BOILERPLATE_PREFIXES)]
    return useful or entries


def _spread(entries: list[dict]) -> list[dict]:
    """Ordonne par dichotomie, pour qu'un préfixe de la liste couvre déjà tout le fichier.

    Pris dans l'ordre des lignes, un échantillon partiel ne montre que l'en-tête du fichier.
    """
    order: list[dict] = []
    queue: deque[tuple[int, int]] = deque([(0, len(entries) - 1)])
    while queue:
        low, high = queue.popleft()
        if low > high:
            continue
        middle = (low + high) // 2
        order.append(entries[middle])
        queue.append((low, middle - 1))
        queue.append((middle + 1, high))
    return order


def count_matches(config: Config, target: Path, pattern: str, *, globs: list[str] | None = None) -> int:
    """Compte les correspondances d'un motif seul, pour jauger sa sélectivité avant de l'employer."""
    unlocked = unlocked_directories(config, target)
    args = ["--count-matches", "--no-filename", "--no-messages", "--ignore-case"]
    if is_git_ignored(config, target):
        args.append("--no-ignore")
    for pattern_glob in globs or []:
        args += ["--glob", pattern_glob]
    for denied in DENIED_DIRECTORIES:
        if denied not in unlocked:
            args += ["--glob", f"!**/{denied}/**"]
    args += ["-e", pattern]

    if target.is_file():
        search_root, path_argument = target.parent, target.name
    else:
        search_root, path_argument = target, "."
    args.append(path_argument)

    try:
        lines = _run_ripgrep(args, search_root, timeout=60)
    except GuardrailError:
        return 0
    return sum(int(line) for line in lines if line.strip().isdigit())


def balanced_sample(matches: list[dict], limit: int, max_files: int | None = None) -> list[dict]:
    """Répartit l'échantillon sur les fichiers les plus denses.

    Deux écueils à éviter ensemble : une troncature dans l'ordre de parcours du disque masque des
    répertoires entiers, mais une répartition sur tous les fichiers touchés ne laisse qu'une ligne
    par fichier et prive le modèle du contexte qui porte la réponse.
    """
    grouped: dict[str, list[dict]] = {}
    for match in matches:
        grouped.setdefault(match["file"], []).append(match)

    breadth = max_files or max(12, limit // 8)
    ranked = sorted(
        grouped.items(),
        key=lambda item: (item[1][0].get("file_score", len(item[1])), len(item[1])),
        reverse=True
    )[:breadth]
    by_file = {name: _spread(_drop_boilerplate(entries)) for name, entries in ranked}

    selected: list[dict] = []
    round_index = 0
    while len(selected) < limit:
        added = False
        for entries in by_file.values():
            if round_index >= len(entries):
                continue
            selected.append(entries[round_index])
            added = True
            if len(selected) >= limit:
                break
        if not added:
            break
        round_index += 1
    return sorted(selected, key=lambda item: (item["file"], item["line"]))


def read_text(path: Path, max_size: int) -> tuple[str, bool]:
    raw = path.read_bytes()[: max_size + 1]
    truncated = len(raw) > max_size
    text = raw[:max_size].decode("utf-8", errors="replace")
    if "\x00" in text[:4096]:
        raise GuardrailError(f"fichier binaire ignoré : {path.name}")
    return text, truncated


def render_file(config: Config, item: SelectedFile, *, numbered: bool = True) -> str:
    try:
        text, truncated = read_text(item.path, config.max_file_size)
    except GuardrailError:
        return ""
    if numbered:
        body = "\n".join(f"{index}| {line}" for index, line in enumerate(text.splitlines(), start=1))
    else:
        body = text
    suffix = "\n[... fichier tronqué ...]" if truncated else ""
    return f"===== FICHIER {item.relative} =====\n{body}{suffix}\n"


def build_chunks(config: Config, files: list[SelectedFile], *, numbered: bool = True) -> list[str]:
    """Assemble les fichiers en lots bornés, sans jamais tout envoyer d'un coup."""
    chunks: list[str] = []
    current: list[str] = []
    current_size = 0
    for item in files:
        rendered = render_file(config, item, numbered=numbered)
        if not rendered:
            continue
        if current and current_size + len(rendered) > config.chunk_chars:
            chunks.append("".join(current))
            current, current_size = [], 0
        if len(rendered) > config.chunk_chars:
            rendered = rendered[: config.chunk_chars] + "\n[... lot tronqué ...]\n"
        current.append(rendered)
        current_size += len(rendered)
        if len(chunks) >= config.max_chunks:
            break
    if current and len(chunks) < config.max_chunks:
        chunks.append("".join(current))
    return chunks[: config.max_chunks]
