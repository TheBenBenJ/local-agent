"""Tâches déléguées au modèle local : recherche, analyse, logs, contrôles."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

from . import budget, ocr, prompts, shell
from .config import Config
from .files import (
    DECLARATION,
    balanced_sample,
    build_chunks,
    count_matches,
    discover_files,
    ensure_usable_root,
    grep,
    read_text,
    resolve_path
)
from .mlx import MlxClient, MlxError
from .report import Report

ANALYSIS_PRESETS: dict[str, str] = {
    "review": (
        "First-pass code review: likely bugs, inconsistencies, duplication, convention drift, "
        "obvious debt. Ignore cosmetic style preferences."
    ),
    "inspect": "Inspect the code and answer the task precisely, citing files and line numbers.",
    "summarize": (
        "Summarize each file's role in one line maximum, then the overall structure."
    ),
    "duplicates": (
        "Find duplicated or near-identical implementations (repeated logic, redundant helpers) "
        "and list files and lines for each duplicate."
    ),
}

LOG_DEFAULT_PATTERNS = [
    r"\b(ERROR|CRITICAL|EMERGENCY|ALERT|FATAL)\b",
    r"\bException\b",
    r"\bWarning\b",
    r"Stack trace",
    r"\bDeprecated\b",
]

_NOISE = [
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?"), "<date>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b0x[0-9a-f]+\b", re.I), "<hex>"),
    (re.compile(r"\b\d{3,}\b"), "<n>"),
    (re.compile(r'"[^"]{40,}"'), '"<long>"'),
]

_PHPUNIT_PASS = re.compile(r"^\s*[✔✓]\s")
_PHPUNIT_PROGRESS = re.compile(r"^[.SIER\s]{10,}\d*\s*/?\s*\d*\s*\(?\s*\d*%?\)?$")
_NOISE_LINES = (
    "Note: Using configuration file",
    "PHPUnit ",
    "Runtime:",
    "Configuration:",
    "Random Seed",
    "yarn run v",
    "$ eslint",
    "Done in ",
)


def _payload_to_report(title: str, payload: dict, *, stats: dict | None = None) -> Report:
    return Report(
        title=title,
        summary=str(payload.get("summary") or ""),
        findings=list(payload.get("findings") or []),
        files=list(payload.get("files") or []),
        locations=list(payload.get("locations") or []),
        risks=list(payload.get("risks") or []),
        next_actions=list(payload.get("next_actions") or []),
        stats=stats or {},
    )


def _looks_like_raw_json(summary: str) -> bool:
    stripped = summary.lstrip()
    return stripped.startswith("{") or '"summary"' in stripped


def _ask(
    client: MlxClient,
    system: str,
    prompt: str,
    *,
    max_tokens: int | None = None,
    temperature: float | None = None
) -> dict:
    completion = client.complete(prompt, system, max_tokens=max_tokens, temperature=temperature)
    payload = prompts.extract_json(completion.text)
    # Un résumé qui ressemble à du JSON signale une extraction échouée malgré les réparations : une
    # relance coûte quelques secondes, un rapport illisible coûte l'appel entier.
    if _looks_like_raw_json(str(payload.get("summary") or "")):
        retried = client.complete(
            prompt + "\n\nStrict reminder: a single valid, closed JSON object, with no surrounding text.",
            system,
            max_tokens=max_tokens,
            temperature=temperature
        )
        candidate = prompts.extract_json(retried.text)
        if not _looks_like_raw_json(str(candidate.get("summary") or "")):
            return candidate
    return payload


_STOP_WORDS = {
    "dans", "pour", "avec", "sans", "quel", "quelle", "quels", "quelles", "comment", "cette", "sont",
    "code", "fichier", "fichiers", "implemente", "implementee", "trouve", "cherche", "existe", "utilise",
    "metier", "generees", "generee", "genere", "where", "what", "which", "does", "this", "that", "have",
    "faut", "doit", "doivent", "peut", "peuvent", "permet", "permettent", "sert", "servent", "fait",
    "faire", "faut-il", "leur", "leurs", "elle", "elles", "ils", "ces", "chaque", "tout", "tous",
    "toute", "toutes", "plus", "moins", "aussi", "donc", "alors", "entre", "sous", "puis", "meme",
    "etre", "avoir", "quand", "pourquoi", "projet", "depot", "faut", "stockent", "fournit", "portent",
    "definit", "empeche", "appelle", "utilisent", "renvoie", "retourne",
}


_LOCATION_SHAPE = re.compile(r"^([\w./-]+)(.*)$")


def _verify_paths(report: Report, known: set[str], config: Config) -> Report:
    """Corrige ou signale les chemins recopiés par le modèle, qui se corrompent parfois en génération.

    Observé : `Supervion` pour `Supervision` dans un chemin par ailleurs juste. Le nom de fichier survit
    mieux que le répertoire, donc un basename retrouvé dans les fichiers réellement lus fait foi.
    """
    by_basename: dict[str, list[str]] = defaultdict(list)
    for relative in known:
        by_basename[Path(relative).name].append(relative)

    def repair(candidate: str) -> tuple[str, bool]:
        cleaned = candidate.strip().lstrip("/")
        if cleaned in known or (config.repo_root / cleaned).exists():
            return cleaned, True
        replacements = by_basename.get(Path(cleaned).name, [])
        if len(replacements) == 1:
            return replacements[0], True
        return cleaned, False

    def repair_list(items: list[str]) -> list[str]:
        result = []
        for item in items:
            shaped = _LOCATION_SHAPE.match(item.strip())
            if not shaped:
                result.append(item)
                continue
            path, trusted = repair(shaped.group(1))
            result.append(path + shaped.group(2) if trusted else f"{item} [chemin non vérifié]")
        return result

    report.files = repair_list(report.files)
    report.locations = repair_list(report.locations)
    return report


# Conclure à l'absence alors que des emplacements sont listés : observé sur « existe-t-il un
# mécanisme de forçage ? », le résumé niait la saisie manuelle puis listait les NumberType posés.
_ABSENCE = re.compile(
    r"(?i)("
    r"\baucun(?:e|es)?\b"
    r"|\bn['’](?:existe|existent|y a)\s+pas\b"
    r"|\bn['’]existe\s+en dehors\b"
    r"|\bne\s+(?:propose|proposent|contient|contiennent|permet|permettent|"
    r"gère|gèrent|offre|offrent)\s+pas\b"
    r"|\bpas de saisie\b"
    r"|\bno (?:mechanism|manual|such)\b"
    r")"
)


ABSENCE_PREFIX = "Do not conclude absence / Ne pas conclure à l'absence"


def _claims_absence(text: str) -> bool:
    return bool(text and _ABSENCE.search(text))


def _reconcile_absence(report: Report) -> Report:
    """Locations win: an absence claim is only valid when that section is empty."""
    if not report.locations:
        return report
    accused = [report.summary, *report.findings]
    if not any(_claims_absence(text) for text in accused):
        return report
    notice = (
        f"{ABSENCE_PREFIX}: {len(report.locations)} location(s) listed. "
        "The Locations section takes precedence over the summary."
    )
    if not report.summary.startswith("Do not conclude absence"):
        report.summary = f"{notice} {report.summary}".strip()
    if notice not in report.risks:
        report.risks.insert(0, notice)
    return report


def _flag_partial_sample(report: Report, omissions: list[str]) -> Report:
    """Dit en clair que la réponse porte sur un échantillon.

    Le rapport chiffrait déjà l'écart dans ses statistiques, mais un compteur en fin de rapport se perd
    au premier résumé : sur une question d'énumération, l'orchestrateur conclut alors à l'exhaustivité.
    """
    retenues = [item for item in omissions if item]
    if not retenues:
        return report
    report.summary = (
        f"Sample-based answer / Réponse établie sur un échantillon ({' ; '.join(retenues)}) : "
        f"an enumeration from this report may be incomplete.\n\n{report.summary}"
    )
    return report


def _deaccent(word: str) -> str:
    return "".join(
        char for char in unicodedata.normalize("NFD", word) if unicodedata.category(char) != "Mn"
    )


def _fallback_patterns(query: str) -> list[str]:
    """Repli purement lexical quand le modèle ne fournit pas de motifs exploitables.

    Les mots sont ancrés sur les limites de mot : sans cela, `faut` ramène chaque `défaut` du dépôt.
    """
    words = re.findall(r"[A-Za-zÀ-ÿ_][A-Za-zÀ-ÿ0-9_]{3,}", query)
    kept: list[str] = []
    for word in words:
        plain = _deaccent(word).lower()
        if plain in _STOP_WORDS or plain in kept:
            continue
        kept.append(plain)
    return [rf"\b{word}\b" for word in kept[:6]] or [re.escape(query[:40])]


_FLAVOR_BY_EXTENSION = {
    "php": "a PHP/Symfony repository (camelCase identifiers, PascalCase classes, YAML config keys)",
    "py": "a Python repository (snake_case functions and variables, UPPER_SNAKE constants)",
    "ts": "a TypeScript repository (camelCase identifiers, PascalCase types)",
    "js": "a JavaScript repository (camelCase identifiers)",
}

_flavor_cache: dict[str, str] = {}


def _repo_flavor(config: Config) -> str:
    """Dominant language is read from tracked files; marker files are often missing."""
    key = str(config.repo_root)
    if key not in _flavor_cache:
        listing = shell.git(config, ["ls-files"], timeout=30)
        tally = Counter(
            suffix for line in listing.stdout.splitlines()
            if (suffix := Path(line).suffix.lstrip(".").lower()) in _FLAVOR_BY_EXTENSION
        )
        dominant = tally.most_common(1)
        _flavor_cache[key] = (
            _FLAVOR_BY_EXTENSION[dominant[0][0]] if dominant
            else "a code repository (English identifiers, dominant-language conventions)"
        )
    return _flavor_cache[key]


def _analyst(config: Config) -> str:
    return prompts.analyst_system(_repo_flavor(config))


def _derive_patterns(client: MlxClient, query: str, flavor: str, avoid: list[str] | None = None) -> list[str]:
    avoid_hint = (
        f"Patterns already tried unsuccessfully, propose others: {', '.join(avoid)}.\n" if avoid else ""
    )
    prompt = (
        "Transforme cette question en 3 à 5 expressions régulières ripgrep permettant de localiser le code "
        f"concerné dans {flavor}. Extraire noms de classes, de méthodes, de constantes, de clés de "
        "configuration, termes métier.\n"
        "Les identifiants du code sont en anglais : traduis les notions non anglaises de la question en "
        "identifiants anglais probables (« propriété » donne property, « connexion » donne connection, "
        "« garde-fous » donne DENIED ou guardrail).\n"
        "Si la question est déjà en anglais, utilise ces identifiants et des radicaux courts, pas des noms inventés.\n"
        "Des radicaux courts et sûrs valent mieux que des identifiants complets inventés : delegat plutôt "
        "que shouldDelegateToModel.\n"
        "Contraintes : exactement 3 à 5 motifs, chacun tenant en un mot ou une expression courte, un seul "
        "concept par motif, jamais deux notions collées dans le même motif, sans variantes spéculatives ni "
        "énumération de synonymes.\n"
        "Si la question nomme déjà un type et demande ce qu'il fait, cherche les usages et le vocabulaire "
        "de l'action. N'émets jamais de motif de déclaration (`class Nom`, `trait Nom`, `function Nom`) : "
        "ça ne sert que si la question demande où le symbole est défini.\n"
        + avoid_hint
        + f"Question : {query}\n\n"
        'Réponds uniquement par {"patterns": ["...", "..."]}'
    )
    # Température nulle : un motif qui varie d'un appel à l'autre fait varier la réponse entière.
    try:
        completion = client.complete(prompt, prompts.SYSTEM_DERIVE, max_tokens=300, temperature=0.0)
    except MlxError:
        return _fallback_patterns(query)

    patterns: list[str] = []
    for item in prompts.extract_list(completion.text, "patterns"):
        candidate = str(item).strip()
        if not candidate or len(candidate) > 120:
            continue
        try:
            re.compile(candidate)
        except re.error:
            continue
        patterns.append(candidate)

    return patterns[:5] or _fallback_patterns(query)


_SUFFIXES = ("ations", "ation", "ings", "ing", "ions", "ion", "ers", "er", "ies", "es", "ed", "e", "s")
_GENERIC_PARTS = {"should", "would", "could", "check", "get", "set", "has", "is", "to", "for", "with", "from"}


def _stem(word: str) -> str:
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: len(word) - len(suffix)]
    return word


def _radicals(patterns: list[str]) -> list[str]:
    """Radicaux des identifiants proposés par le modèle, qui invente des noms complets là où le code
    conjugue autrement : `shouldDelegate` rate `is_worth_delegating`, son radical `delegat` le trouve."""
    found: list[str] = []
    for pattern in patterns:
        # Déplie les classes à deux casses avant découpe : sans cela `[Tt]imestamp` perd sa première lettre.
        pattern = re.sub(r"\[([A-Za-z])[A-Za-z]?\]", r"\1", pattern)
        for part in re.findall(r"[A-Za-z]{4,}", pattern):
            for piece in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", part):
                stem = _stem(piece.lower())
                if len(stem) >= 4 and stem not in _GENERIC_PARTS and stem not in found:
                    found.append(stem)
    return found


def _salient_terms(query: str) -> list[str]:
    """Termes du domaine dans la question : une majuscule ou une capitalisation les signale presque toujours."""
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", query)
    return [word for word in words if word[0].isupper() or word.isupper()]


# « comment » et « pourquoi » demandent une explication, que des lignes brutes ne donnent pas.
_ENUMERATION = re.compile(r"\b(quels?|quelles?|o[uù]|listez?|combien|which|where|list)\b", re.IGNORECASE)
_EXPLANATION = re.compile(r"\b(comment|pourquoi|how|why)\b", re.IGNORECASE)
# Verbes d'action : la question porte sur ce qu'un type FAIT, pas sur sa déclaration.
_USAGE_INTENT = re.compile(
    r"\b("
    r"écrit|ecrit|assigne|calcule|appelle|utilise|modifie|remplit|force|"
    r"envoie|pose|fixe|renseigne|hydrate|alimente|persiste|"
    r"write|assign|call|fill|update|persist|save|send|"
    r"champs?|fields?"
    r")",
    re.IGNORECASE,
)
_NAMED_TYPE_DECL = re.compile(
    r"(?:class|trait|interface|enum)\s+[A-Z_]\w*",
    re.IGNORECASE,
)


def _is_enumeration(query: str) -> bool:
    return bool(_ENUMERATION.search(query)) and not _EXPLANATION.search(query)


def _is_usage_query(query: str) -> bool:
    """« Où PaieService écrit les champs H+ » n'est pas une énumération de symboles."""
    return bool(_USAGE_INTENT.search(query))


def _anchored_variants(term: str) -> list[str]:
    """Motifs qui désignent l'usage ou la déclaration d'un symbole, pas ses simples mentions.

    Sur « quelles classes portent l'attribut Referencable », le terme nu ramène 168 lignes dont les
    imports, quand `#\\[Referencable` ramène exactement les classes qui le portent.
    """
    escaped = re.escape(term)
    return [
        rf"#\[{escaped}\b",
        rf"^\s*(?:final\s+|abstract\s+|readonly\s+)*(?:class|trait|interface|enum)\s+{escaped}\b",
        rf"function\s+{escaped}\s*\("
    ]


# Couvre les deux langages du poste : `def nom` en Python, `function nom` en PHP avec ses modificateurs.
FUNCTION_DECLARATION = r"^\s*(?:def|(?:public\s+|private\s+|protected\s+|static\s+|final\s+|abstract\s+)*function)\s+\w+"

STRUCTURAL_KEYWORDS = {
    "trait": r"^\s*trait\s+\w+",
    "traits": r"^\s*trait\s+\w+",
    "classe": r"^\s*(?:final\s+|abstract\s+|readonly\s+)*class\s+\w+",
    "classes": r"^\s*(?:final\s+|abstract\s+|readonly\s+)*class\s+\w+",
    "class": r"^\s*(?:final\s+|abstract\s+|readonly\s+)*class\s+\w+",
    "interface": r"^\s*interface\s+\w+",
    "interfaces": r"^\s*interface\s+\w+",
    "enum": r"^\s*enum\s+\w+",
    "enums": r"^\s*enum\s+\w+",
    "fonction": FUNCTION_DECLARATION,
    "fonctions": FUNCTION_DECLARATION,
    "function": FUNCTION_DECLARATION,
    "methode": FUNCTION_DECLARATION,
    "methodes": FUNCTION_DECLARATION,
    "method": FUNCTION_DECLARATION
}


def _structural_patterns(query: str) -> list[str]:
    """Une question nommant une construction PHP demande une déclaration, pas des mentions du mot.

    Le mot nu est trop courant pour servir de motif : `trait` matche chaque `use MonTrait;` du dépôt.
    """
    found = []
    for word in re.findall(r"[A-Za-zÀ-ÿ]+", query.lower()):
        pattern = STRUCTURAL_KEYWORDS.get(_deaccent(word))
        if pattern and pattern not in found:
            found.append(pattern)
    return found


def _trim_to_capacity(counted: list[tuple[str, int]], capacity: int) -> list[str]:
    """Écarte les motifs les plus larges quand ils noieraient les plus précis sous le plafond de grep.

    Un motif structurel (toute déclaration de classe) peut ramener des milliers de lignes : additionné à
    un motif précis, il consomme le plafond et dilue le signal que le motif précis portait.
    """
    kept: list[str] = []
    cumulative = 0
    for pattern, count in sorted(counted, key=lambda item: item[1]):
        if kept and cumulative + count > capacity:
            break
        kept.append(pattern)
        cumulative += count
    return kept


def _select_patterns(
    config: Config,
    client: MlxClient,
    query: str,
    target: Path,
    globs: list[str] | None = None
) -> tuple[list[str], bool]:
    """Retient les motifs du domaine, et ne descend aux mots de la question qu'à défaut.

    Mélanger les deux noie le signal : sur « quelles classes portent l'attribut Referencable », le motif
    précis donne 8 correspondances dans 4 fichiers, que le mot « attribut » à lui seul dilue dans 140.

    Renvoie aussi la valeur sémantique des motifs : ceux du repli lexical ne traduisent pas la question,
    ils en recopient les mots, et leurs correspondances ne doivent jamais être rendues comme une réponse.
    """
    salient = _salient_terms(query)
    usage = _is_usage_query(query)

    # Énumération sur un symbole nommé : les lignes ancrées SONT la réponse, exhaustive et sans modèle.
    # Mesuré : 9,6x moins cher qu'une synthèse pour la même réponse, en une seconde au lieu de neuf.
    # Sauf question d'usage : « où PaieService écrit les champs » ancré sur `class PaieService` rate
    # toutes les assignations. Le raccourci ne vaut que pour lister ou localiser le symbole lui-même.
    if salient and _is_enumeration(query) and not usage:
        anchored = [
            (pattern, count)
            for term in salient
            for pattern in _anchored_variants(term)
            if (count := count_matches(config, target, pattern, globs=globs))
        ]
        if anchored and sum(count for _, count in anchored) <= budget.PASSTHROUGH_MATCHES:
            return [pattern for pattern, _ in anchored], True

    # Hors raccourci, la dérivation reste systématique : un terme salient productif ne suffit pas
    # toujours, le pivot pouvant être un nom commun que seul le modèle traduit en identifiant anglais
    # (« connexions » vers connections). Mesuré : sauter la dérivation a coûté une réponse fausse sur dix.
    structural = [] if usage else _structural_patterns(query)
    cheap = list(dict.fromkeys(salient + structural))
    counted_salient = [
        (pattern, count)
        for pattern in salient
        if (count := count_matches(config, target, pattern, globs=globs))
    ]
    flavor = _repo_flavor(config)
    derived = [pattern for pattern in _derive_patterns(client, query, flavor) if pattern not in cheap]
    if usage:
        derived = [pattern for pattern in derived if not _NAMED_TYPE_DECL.search(pattern)]
    counted_derived = [
        (pattern, count)
        for pattern in derived
        if (count := count_matches(config, target, pattern, globs=globs))
    ]

    # Moisson maigre : la dérivation varie d'un appel à l'autre et un tirage faible produit une réponse
    # faible. Les radicaux des identifiants proposés, puis une seconde passe écartant les motifs
    # stériles, récupèrent souvent le motif manquant.
    if len(counted_derived) + len(counted_salient) < 2 or sum(c for _, c in counted_derived) < 10:
        seen = {pattern for pattern, _ in counted_derived}
        # Un radical trop productif ne secourt rien : il noierait l'échantillon qu'il devait renforcer.
        counted_derived += [
            (stem, count)
            for stem in _radicals(derived)
            if stem not in seen
            and 0 < (count := count_matches(config, target, stem, globs=globs)) <= budget.PASSTHROUGH_MATCHES
        ]
    if len(counted_derived) + len(counted_salient) < 2 or sum(c for _, c in counted_derived) < 10:
        retry = [
            pattern for pattern in _derive_patterns(client, query, flavor, avoid=derived)
            if pattern not in cheap and pattern not in derived
            and not (usage and _NAMED_TYPE_DECL.search(pattern))
        ]
        counted_derived += [
            (pattern, count)
            for pattern in retry
            if (count := count_matches(config, target, pattern, globs=globs))
        ]

    if counted_derived or counted_salient:
        # Un motif structurel ne vient qu'en appoint de motifs porteurs de sens déjà productifs : seul ou
        # adossé à des motifs quasi stériles, il ramène toutes les déclarations du dépôt et noie la question.
        counted_structural = (
            [
                (pattern, count)
                for pattern in structural
                if (count := count_matches(config, target, pattern, globs=globs))
            ]
            if sum(c for _, c in counted_derived + counted_salient) >= 10 else []
        )
        combined = counted_derived + counted_salient + counted_structural
        return _trim_to_capacity(combined, config.max_matches), True

    weak = [term for term in _fallback_patterns(query) if term.lower() not in {item.lower() for item in cheap}]
    productive_weak = [pattern for pattern in weak if count_matches(config, target, pattern, globs=globs)]
    return (productive_weak, False) if productive_weak else (derived + cheap + weak, False)


def _snippet_context(config: Config, matches: list[dict], budget: int) -> str:
    """Extrait de courtes fenêtres autour des correspondances les plus denses, sans lire tout le fichier."""
    by_file: dict[str, list[int]] = defaultdict(list)
    weight: dict[str, int] = {}
    for match in matches:
        by_file[match["file"]].append(match["line"])
        weight[match["file"]] = match.get("file_score", len(by_file[match["file"]]))
    ranked = sorted(by_file.items(), key=lambda item: weight[item[0]], reverse=True)[:6]

    pieces: list[str] = []
    used = 0
    for relative, lines in ranked:
        path = config.repo_root / relative
        if not path.is_file():
            continue
        try:
            text, _ = read_text(path, config.max_file_size)
        except Exception:
            continue
        content = text.splitlines()
        windows: list[tuple[int, int]] = []
        # Les lignes arrivent déjà réparties sur le fichier : les re-trier avant de tronquer ramènerait à l'en-tête.
        for line in sorted(dict.fromkeys(lines[:5])):
            # Une déclaration mérite son corps : sans lui, le modèle ne peut pas dire ce que la
            # classe ou le trait fournit, seulement qu'il existe.
            is_declaration = line <= len(content) and DECLARATION.search(content[line - 1])
            start = max(1, line - 12)
            end = min(len(content), line + (48 if is_declaration else 18))
            if windows and start <= windows[-1][1] + 5:
                windows[-1] = (windows[-1][0], end)
            else:
                windows.append((start, end))
        for start, end in windows:
            block = "\n".join(f"{index}| {content[index - 1]}" for index in range(start, end + 1))
            rendered = f"===== {relative}:{start}-{end} =====\n{block}\n"
            if used + len(rendered) > budget:
                return "".join(pieces)
            pieces.append(rendered)
            used += len(rendered)
    return "".join(pieces)


def search(config: Config, client: MlxClient, query: str, path: str | None = None, globs: list[str] | None = None) -> Report:
    target = resolve_path(config, path)
    patterns, semantic = _select_patterns(config, client, query, target, globs)
    matches, total = grep(config, target, patterns, globs=globs, balance_by_file=True)

    if not matches:
        return Report(
            title="Local search",
            summary=f"No matches for the question under {path or '.'}.",
            findings=[f"Patterns tried: {', '.join(patterns)}"],
            next_actions=["Reword the question or widen the search path"],
            stats={"patterns": len(patterns), "matches": 0},
        )

    counts = Counter({match["file"]: match["file_hits"] for match in matches})
    shown = matches if len(matches) <= 120 else balanced_sample(matches, 120)
    match_lines = "\n".join(f"{m['file']}:{m['line']}: {m['text']}" for m in shown)

    # Peu de correspondances : les lignes brutes répondent déjà, exhaustivement et sans risque d'omission.
    # Réservé aux motifs sémantiques : celles du repli lexical recopient les mots de la question et
    # produiraient un brut hors sujet rendu avec l'assurance d'une réponse.
    if semantic and total <= budget.PASSTHROUGH_MATCHES and not budget.is_worth_delegating(match_lines):
        return budget.passthrough(
            "Local search",
            match_lines,
            reason=f"{total} match(es) only, synthesis would cost more than the lines",
            stats={"patterns": len(patterns), "matches_total": total, "files": len(counts)},
            details=f"Patterns: {', '.join(patterns)}"
        )
    snippets = _snippet_context(config, matches, budget=config.chunk_chars)

    # Exhaustiveness hint for enumerations: the model sometimes stops at the first item even
    # when the excerpt window contained the others.
    enumeration_hint = (
        "This is an enumeration question: list every distinct item visible in the excerpts "
        "before concluding, omit none.\n"
        if _is_enumeration(query) else ""
    )
    prompt = (
        f"Question: {query}\n\n"
        f"ripgrep patterns used: {', '.join(patterns)}\n"
        f"Hottest files: {', '.join(f'{f} ({c})' for f, c in counts.most_common(10))}\n\n"
        f"Raw matches:\n{match_lines}\n\n"
        f"Code excerpts around the matches:\n{snippets}\n\n"
        "Answer using only these elements. Distinguish the main entry point from secondary "
        "occurrences. Locations first; no absence claim if they are not empty. "
        f"Only allowed count: {total} match(es) in {len(counts)} file(s), otherwise 'at least N in the sample'.\n"
        + enumeration_hint + "\n" + prompts.JSON_CONTRACT
    )
    payload = _ask(client, _analyst(config), prompt, temperature=0.0)
    report = _payload_to_report(
        "Local search",
        payload,
        stats={
            "patterns": len(patterns),
            "matches_kept": len(matches),
            "matches_total": total,
            "files": len(counts),
            "source_caracteres": len(match_lines) + len(snippets),
        },
    )
    report = _reconcile_absence(_verify_paths(report, set(counts), config))
    return _flag_partial_sample(
        report,
        [f"{len(matches)} matches examined of {total}" if len(matches) < total else ""]
    )


def analyze(
    config: Config,
    client: MlxClient,
    path: str | None,
    task: str | None = None,
    *,
    mode: str = "inspect",
    globs: list[str] | None = None,
    max_files: int | None = None,
) -> Report:
    target = resolve_path(config, path)
    preset = ANALYSIS_PRESETS.get(mode, ANALYSIS_PRESETS["inspect"])
    instruction = task or preset
    files, total = discover_files(config, target, globs=globs, max_files=max_files)

    if not files:
        images = ocr.list_image_files(config, target, globs=globs, max_files=max_files)
        if images:
            extra = [str(item) for item in images[1:]]
            return ocr.read_images(config, str(images[0]), extra or None, task)
        return Report(
            title=f"Local analysis ({mode})",
            summary=f"No analysable file under {path or '.'} after applying guardrails.",
            next_actions=[
                "If this is a screenshot, call local_image with the file path (png/jpg are not code).",
                "Check the path or relax --glob filters",
            ],
        )

    chunk_set = build_chunks(config, files)
    chunks = chunk_set.parts

    # Contenu plus court qu'une synthèse : le rendre tel quel et laisser l'orchestrateur juger.
    joined = "\n".join(chunks)
    if not budget.is_worth_delegating(joined, budget.PASSTHROUGH_CONTENT_CHARS):
        return budget.passthrough(
            f"Local analysis ({mode})",
            joined,
            reason=f"{len(files)} file(s) for {len(joined.strip())} characters, shorter than a synthesis",
            stats={"files_examined": len(files), "files_found": total},
            details=f"Task not delegated: {instruction}"
        )

    payloads: list[dict] = []
    errors: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        prompt = (
            f"Task: {instruction}\n"
            f"Frame: {preset}\n"
            f"Chunk {index}/{len(chunks)} of repository files (numbered lines).\n\n"
            f"{chunk}\n\n" + prompts.JSON_CONTRACT
        )
        try:
            payloads.append(_ask(client, _analyst(config), prompt))
        except MlxError as error:
            errors.append(f"chunk {index}: {error}")

    if not payloads:
        return Report(
            title=f"Local analysis ({mode})",
            summary="The local model produced no usable analysis.",
            errors=errors,
        )

    merged = prompts.merge_payloads(payloads)
    if len(payloads) > 1:
        digest = "\n".join(
            f"- chunk {index}: {p.get('summary')} | " + " ; ".join(list(p.get("findings") or [])[:5])
            for index, p in enumerate(payloads, start=1)
        )
        prompt = (
            f"Original task: {instruction}\n\n"
            f"Per-chunk partial analyses:\n{digest}\n\n"
            f"Files spotted: {', '.join(list(merged.get('files') or [])[:30])}\n\n"
            "Produce a single synthesis, deduplicated and ranked by importance. Be brief, "
            "the orchestrator has little context left.\n\n" + prompts.JSON_CONTRACT
        )
        try:
            merged = prompts.merge_payloads(
                [_ask(client, _analyst(config), prompt, max_tokens=max(config.max_completion_tokens, 2600))]
            )
        except MlxError as error:
            errors.append(f"synthesis: {error}")

    report = _payload_to_report(
        f"Local analysis ({mode})",
        merged,
        stats={
            "files_analyzed": len(files),
            "files_found": total,
            "chunks": len(chunks),
            "source_caracteres": sum(item.size for item in files),
        },
    )
    report.errors = errors
    if total > len(files):
        report.next_actions.append(
            f"{total - len(files)} files not analysed (limit of {max_files or config.max_files} files per call)"
        )
    report = _reconcile_absence(_verify_paths(report, {item.relative for item in files}, config))
    return _flag_partial_sample(
        report,
        [
            f"{chunk_set.files_included} files read of {total} found"
            if total > chunk_set.files_included else "",
            f"{chunk_set.files_truncated} files truncated for lack of space"
            if chunk_set.files_truncated else ""
        ]
    )


def _signature(line: str) -> str:
    normalized = line
    for pattern, replacement in _NOISE:
        normalized = pattern.sub(replacement, normalized)
    return normalized.strip()[:180]


def analyze_logs(
    config: Config,
    client: MlxClient,
    path: str,
    task: str | None = None,
    patterns: list[str] | None = None,
) -> Report:
    target = resolve_path(config, path)
    used_patterns = patterns or LOG_DEFAULT_PATTERNS
    matches, total = grep(
        config,
        target,
        used_patterns,
        max_matches=5000,
        ignore_case=False,
        max_count_per_file=2000,
    )

    if not matches:
        size = target.stat().st_size if target.is_file() else 0
        return Report(
            title="Log analysis",
            summary=f"No error line found in {path} ({size} bytes scanned).",
            stats={"matches": 0},
            next_actions=["Pass explicit patterns if the log format is unusual"],
        )

    clusters: dict[str, dict] = {}
    for match in matches:
        key = _signature(match["text"])
        entry = clusters.setdefault(key, {"count": 0, "sample": match, "files": set()})
        entry["count"] += 1
        entry["files"].add(match["file"])

    ranked = sorted(clusters.items(), key=lambda item: item[1]["count"], reverse=True)[:20]
    digest = "\n".join(
        f"[{entry['count']}x] {entry['sample']['file']}:{entry['sample']['line']} :: {entry['sample']['text'][:180]}"
        for _, entry in ranked
    )

    prompt = (
        f"Task: {task or 'Identify the dominant errors, their likely causes, and what is worth investigating.'}\n"
        f"Source: {path}\n"
        f"{len(matches)} lines kept of {total} matches, grouped into {len(clusters)} signatures.\n\n"
        f"Most frequent signatures:\n{digest}\n\n"
        "Group by likely root cause. Distinguish recurring noise from real anomalies.\n\n"
        + prompts.JSON_CONTRACT
    )
    payload = _ask(client, _analyst(config), prompt)
    report = _payload_to_report(
        "Log analysis",
        payload,
        stats={
            "lines_matched": len(matches),
            "signatures": len(clusters),
            "patterns": len(used_patterns),
            "source_caracteres": sum(len(match["text"]) for match in matches),
        },
    )
    report = _reconcile_absence(_verify_paths(report, {match["file"] for match in matches}, config))
    return _flag_partial_sample(
        report,
        [
            f"{len(matches)} lines kept of {total}" if len(matches) < total else "",
            f"{len(ranked)} signatures examined of {len(clusters)}" if len(ranked) < len(clusters) else ""
        ]
    )


def _filter_command_output(kind: str, output: str, limit: int = 12_000) -> tuple[str, dict]:
    lines = output.splitlines()
    kept: list[str] = []
    dropped = 0
    for line in lines:
        if _PHPUNIT_PASS.match(line) or _PHPUNIT_PROGRESS.match(line):
            dropped += 1
            continue
        if kind == "cs-fixer" and (line.startswith("+") or line.startswith("-")) and not line.startswith("---"):
            dropped += 1
            continue
        if any(line.startswith(noise) for noise in _NOISE_LINES):
            dropped += 1
            continue
        if not line.strip():
            continue
        kept.append(line)

    text = "\n".join(kept)
    if len(text) > limit:
        head = text[: limit // 2]
        tail = text[-limit // 2 :]
        text = f"{head}\n[... {len(text) - limit} caractères retirés ...]\n{tail}"
    return text, {"lines_total": len(lines), "lines_kept": len(kept), "lines_dropped": dropped}


def check(
    config: Config,
    client: MlxClient,
    kind: str | None = None,
    target: str | None = None,
    filter_expression: str | None = None,
) -> Report:
    ensure_usable_root(config)
    if target:
        resolve_path(config, target)
    checks = shell.load_checks(config)
    if not checks:
        raise ValueError(
            f"no checks defined for this repository: declare checks in {shell.CHECKS_FILE} at the root"
        )
    kind = kind or next(iter(checks))
    argv, spec = shell.build_check_command(checks, kind, target, filter_expression)
    result = shell.run(argv, config.repo_root, config.command_timeout)
    filtered, stats = _filter_command_output(kind, result.output)

    stats.update({
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "source_caracteres": len(result.output),
    })
    base = Report(
        title=f"Local check: {spec['label']}",
        stats=stats,
        details=f"Command: `{result.command}`",
    )

    if result.exit_code == 0 and not filtered.strip():
        base.summary = "Check passed."
        base.stats = {"exit_code": 0}
        base.details = ""
        return base
    if result.timed_out:
        base.summary = f"Command interrupted by timeout after {config.command_timeout}s."
        base.risks = ["Partial result, raise LOCAL_AGENT_COMMAND_TIMEOUT if needed"]
        return base
    if not filtered.strip():
        base.summary = f"Command exited with code {result.exit_code} and no usable output."
        return base

    # Sortie déjà courte : la synthèse pèserait plus lourd que la sortie qu'elle résume.
    if not budget.is_worth_delegating(filtered):
        return budget.passthrough(
            base.title,
            filtered,
            reason=f"output of {len(filtered.strip())} characters, already shorter than a synthesis",
            stats=stats,
            details=base.details
        )

    prompt = (
        f"Filtered output of: {result.command}\n"
        f"Exit code: {result.exit_code}\n"
        f"Lines kept: {stats['lines_kept']} of {stats['lines_total']}\n\n"
        f"{filtered}\n\n"
        "Classify issues by kind and file, give likely causes and what is blocking. "
        "Do not copy stack traces.\n\n" + prompts.JSON_CONTRACT
    )
    try:
        payload = _ask(client, _analyst(config), prompt)
    except MlxError as error:
        base.summary = f"Command ran (code {result.exit_code}) but local synthesis is unavailable."
        base.errors = [str(error)]
        return base

    report = _payload_to_report(base.title, payload, stats=stats)
    report.details = base.details
    return report


DIFF_SCOPES: dict[str, list[str]] = {
    "worktree": ["diff", "HEAD"],
    "staged": ["diff", "--staged"],
    "branch": ["diff"],
}


def _resolve_diff_args(config: Config, scope: str, base: str | None) -> list[str]:
    if scope not in DIFF_SCOPES:
        raise ValueError(f"unknown scope: {scope}. Available: {', '.join(sorted(DIFF_SCOPES))}")
    args = list(DIFF_SCOPES[scope])
    if scope == "branch":
        reference = base or next(
            (name for name in ("main", "master", "develop")
             if shell.git(config, ["rev-parse", "--verify", "--quiet", name]).exit_code == 0),
            None
        )
        if not reference:
            raise ValueError("no base branch found (main, master, develop): pass base")
        args.append(f"{reference}...HEAD")
    return args


def _split_diff_by_file(diff: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    for block in re.split(r"(?m)^(?=diff --git )", diff):
        if not block.strip():
            continue
        header = block.splitlines()[0]
        found = re.search(r" b/(.+)$", header)
        sections.append((found.group(1) if found else header, block))
    return sections


_ADDED_CALL = re.compile(r"^\+[^+].*?(?:->|::|\.)(\w{4,})\s*\(", re.MULTILINE)
_DIFF_DEFINED = re.compile(r"(?:function|def|fn)\s+(\w{4,})\s*\(")
_GENERIC_CALLS = {
    "add", "get", "set", "has", "is", "count", "push", "pop", "map", "filter", "reduce",
    "find", "append", "extend", "update", "write", "read", "load", "save", "render",
    "create", "delete", "remove", "clear", "reset", "flush", "persist", "findAll",
    "findBy", "findOneBy", "toArray", "toString", "format", "replace", "split",
}
_MISSING_CLAIM = re.compile(
    r"(?i)("
    r"n['’]existe pas|introuvable|undefined|does not exist|n['’]est pas défini"
    r"|manquant[e]?|missing|inconnu[e]?|pas définie?"
    r")"
)


def _calls_in_added_lines(diff: str) -> list[str]:
    """Appels ajoutés dans le diff, hors méthodes trop génériques et hors définitions du même patch."""
    defined = set(_DIFF_DEFINED.findall(diff))
    found: list[str] = []
    for name in _ADDED_CALL.findall(diff):
        if name in defined or name in _GENERIC_CALLS or name.startswith("__") or name in found:
            continue
        found.append(name)
        if len(found) >= 12:
            break
    return found


def _resolve_diff_symbols(config: Config, names: list[str]) -> dict[str, str]:
    """Un appel dans le diff n'est pas une méthode nouvelle : on vérifie sa définition dans le dépôt."""
    resolved: dict[str, str] = {}
    for name in names:
        pattern = rf"(?:function|def|fn)\s+{re.escape(name)}\s*\("
        matches, _ = grep(
            config,
            config.repo_root,
            [pattern],
            ignore_case=False,
            max_matches=3,
            max_count_per_file=1,
        )
        if matches:
            hit = matches[0]
            resolved[name] = f"{hit['file']}:{hit['line']}"
    return resolved


def _downgrade_known_symbols(report: Report, resolved: dict[str, str]) -> Report:
    """Observé : la revue signalait getTotalHeuresSup comme manquant, alors qu'il était à FichePaie.php:787."""
    if not resolved:
        return report
    kept: list[str] = []
    for risk in report.risks:
        hit = next((name for name in resolved if name in risk), None)
        if hit and _MISSING_CLAIM.search(risk):
            note = f"Verified: {hit} is defined ({resolved[hit]}), not a new method."
            if note not in report.findings:
                report.findings.append(note)
            continue
        kept.append(risk)
    report.risks = kept
    return report


def diff_review(
    config: Config,
    client: MlxClient,
    scope: str = "worktree",
    base: str | None = None,
    task: str | None = None,
) -> Report:
    ensure_usable_root(config)
    args = _resolve_diff_args(config, scope, base)
    result = shell.git(config, [*args, "--no-color"], timeout=config.command_timeout)
    if result.exit_code != 0:
        raise ValueError(f"git {' '.join(args)} failed: {result.stderr.strip()[:300]}")
    diff = result.stdout
    title = f"Diff review ({scope})"

    if not diff.strip():
        return Report(
            title=title,
            summary=f"No changes in scope {scope}.",
            stats={"files": 0},
            next_actions=["Check the scope: worktree, staged, or branch with base"],
        )

    sections = _split_diff_by_file(diff)

    # Un diff court se lit tel quel : la revue par le modèle ne vaut que sur du volume.
    if not budget.is_worth_delegating(diff, budget.PASSTHROUGH_CONTENT_CHARS):
        return budget.passthrough(
            title,
            diff.strip(),
            reason=f"diff de {len(diff.strip())} caractères, plus court qu'une revue",
            stats={"files": len(sections)},
        )

    # Les plus gros changements d'abord : si le budget de contexte force des omissions, elles
    # portent sur les fichiers les moins modifiés.
    sections.sort(key=lambda item: len(item[1]), reverse=True)
    packed: list[str] = []
    used = 0
    omitted: list[str] = []
    for name, block in sections:
        if used + len(block) > config.chunk_chars * 2:
            omitted.append(name)
            continue
        if len(block) > config.chunk_chars:
            block = block[: config.chunk_chars].rsplit("\n", 1)[0] + "\n[... file truncated ...]\n"
        packed.append(block)
        used += len(block)

    instruction = (
        task or (
            "Review these changes: likely bugs, missed edge cases, leftover debug, "
            "inconsistencies with surrounding code visible in the diff, regression risks. "
            "Ignore cosmetic style."
        )
    ) + (
        " Add as the last next_actions item a commit-message proposal prefixed with 'Commit: '."
        " A second loop is not double-processing if a guard in the diff already excludes seen items."
    )
    resolved = _resolve_diff_symbols(config, _calls_in_added_lines(diff))
    known = ""
    if resolved:
        listing = "\n".join(f"- {name}: {place}" for name, place in resolved.items())
        known = (
            "\n\nSymbols called in the diff that already exist elsewhere in the repo "
            "(do not flag them as missing):\n" + listing + "\n"
        )
    prompt = (
        f"Task: {instruction}\n\n"
        f"Git diff ({scope}, {len(sections)} files):\n\n"
        + "".join(packed)
        + known
        + "\n" + prompts.JSON_CONTRACT
    )
    payload = _ask(client, _analyst(config), prompt)
    report = _payload_to_report(
        title,
        payload,
        stats={"files": len(sections), "files_reviewed": len(packed), "source_caracteres": len(diff)},
    )
    report = _downgrade_known_symbols(report, resolved)
    report = _reconcile_absence(_verify_paths(report, {name for name, _ in sections}, config))
    return _flag_partial_sample(
        report,
        [f"{len(omitted)} files not reviewed for lack of space: {', '.join(omitted[:5])}" if omitted else ""]
    )
