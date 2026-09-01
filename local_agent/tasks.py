"""Tâches déléguées au modèle local : recherche, analyse, logs, contrôles."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

from . import budget, prompts, shell
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
        "Fais une première passe de revue de code : bugs probables, incohérences, duplications, "
        "écarts aux conventions Symfony, dette évidente. Ignore les préférences de style cosmétique."
    ),
    "inspect": "Inspecte le code et réponds précisément à la consigne, en citant fichiers et lignes.",
    "summarize": (
        "Résume le rôle de chaque fichier et l'organisation d'ensemble, en 1 ligne par fichier maximum, "
        "puis dégage la structure générale."
    ),
    "duplicates": (
        "Repère les implémentations dupliquées ou quasi identiques (même logique répétée, helpers redondants) "
        "et indique pour chaque doublon les fichiers et lignes concernés."
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
            prompt + "\n\nRappel strict : un unique objet JSON valide et refermé, sans aucun texte autour.",
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


def _flag_partial_sample(report: Report, omissions: list[str]) -> Report:
    """Dit en clair que la réponse porte sur un échantillon.

    Le rapport chiffrait déjà l'écart dans ses statistiques, mais un compteur en fin de rapport se perd
    au premier résumé : sur une question d'énumération, l'orchestrateur conclut alors à l'exhaustivité.
    """
    retenues = [item for item in omissions if item]
    if not retenues:
        return report
    report.summary = (
        f"Réponse établie sur un échantillon ({' ; '.join(retenues)}) : "
        f"une énumération tirée de ce rapport peut être incomplète.\n\n{report.summary}"
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
    "php": "un dépôt Symfony/PHP (identifiants camelCase, classes en PascalCase, clés de configuration YAML)",
    "py": "un dépôt Python (fonctions et variables snake_case, constantes en MAJUSCULES_SOULIGNÉES)",
    "ts": "un dépôt TypeScript (identifiants camelCase, types en PascalCase)",
    "js": "un dépôt JavaScript (identifiants camelCase)",
}

_flavor_cache: dict[str, str] = {}


def _repo_flavor(config: Config) -> str:
    """Le langage dominant se lit dans les fichiers suivis, les fichiers marqueurs étant souvent absents."""
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
            else "un dépôt de code (identifiants anglais, conventions du langage dominant)"
        )
    return _flavor_cache[key]


def _derive_patterns(client: MlxClient, query: str, flavor: str, avoid: list[str] | None = None) -> list[str]:
    avoid_hint = (
        f"Motifs déjà essayés sans succès, proposes-en d'autres : {', '.join(avoid)}.\n" if avoid else ""
    )
    prompt = (
        "Transforme cette question en 3 à 5 expressions régulières ripgrep permettant de localiser le code "
        f"concerné dans {flavor}, dont le vocabulaire métier est français : noms de classes, de méthodes, "
        "de constantes, de clés de configuration, termes métier.\n"
        "Les identifiants du code sont en anglais : traduis les notions françaises de la question en "
        "identifiants anglais probables (« propriété » donne property, « connexion » donne connection).\n"
        "Des radicaux courts et sûrs valent mieux que des identifiants complets inventés : delegat plutôt "
        "que shouldDelegateToModel.\n"
        "Contraintes : exactement 3 à 5 motifs, chacun tenant en un mot ou une expression courte, un seul "
        "concept par motif, jamais deux notions collées dans le même motif, sans variantes spéculatives ni "
        "énumération de synonymes.\n"
        + avoid_hint
        + f"Question : {query}\n\n"
        'Réponds uniquement par {"patterns": ["...", "..."]}'
    )
    # Température nulle : un motif qui varie d'un appel à l'autre fait varier la réponse entière.
    try:
        completion = client.complete(prompt, prompts.SYSTEM_ANALYST, max_tokens=300, temperature=0.0)
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


def _is_enumeration(query: str) -> bool:
    return bool(_ENUMERATION.search(query)) and not _EXPLANATION.search(query)


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

    # Énumération sur un symbole nommé : les lignes ancrées SONT la réponse, exhaustive et sans modèle.
    # Mesuré : 9,6x moins cher qu'une synthèse pour la même réponse, en une seconde au lieu de neuf.
    if salient and _is_enumeration(query):
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
    structural = _structural_patterns(query)
    cheap = list(dict.fromkeys(salient + structural))
    counted_salient = [
        (pattern, count)
        for pattern in salient
        if (count := count_matches(config, target, pattern, globs=globs))
    ]
    flavor = _repo_flavor(config)
    derived = [pattern for pattern in _derive_patterns(client, query, flavor) if pattern not in cheap]
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
            title="Recherche locale",
            summary=f"Aucune correspondance pour la question posée sous {path or '.'}.",
            findings=[f"Motifs essayés : {', '.join(patterns)}"],
            next_actions=["Reformuler la question ou élargir le chemin de recherche"],
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
            "Recherche locale",
            match_lines,
            reason=f"{total} correspondance(s) seulement, la synthèse coûterait plus que les lignes",
            stats={"patterns": len(patterns), "matches_total": total, "files": len(counts)},
            details=f"Motifs : {', '.join(patterns)}"
        )
    snippets = _snippet_context(config, matches, budget=config.chunk_chars)

    # Consigne d'exhaustivité réservée aux énumérations : le modèle conclut parfois sur le premier
    # élément vu, alors même que la fenêtre d'extraits contenait les autres.
    enumeration_hint = (
        "La question demande une énumération : recense chaque élément distinct visible dans les extraits "
        "avant de conclure, sans en omettre.\n"
        if _is_enumeration(query) else ""
    )
    prompt = (
        f"Question : {query}\n\n"
        f"Motifs ripgrep utilisés : {', '.join(patterns)}\n"
        f"Fichiers les plus touchés : {', '.join(f'{f} ({c})' for f, c in counts.most_common(10))}\n\n"
        f"Correspondances brutes :\n{match_lines}\n\n"
        f"Extraits de code autour des correspondances :\n{snippets}\n\n"
        "Réponds à la question en t'appuyant uniquement sur ces éléments. Distingue le point d'entrée principal "
        "des occurrences secondaires.\n" + enumeration_hint + "\n" + prompts.JSON_CONTRACT
    )
    payload = _ask(client, prompts.SYSTEM_ANALYST, prompt, temperature=0.0)
    report = _payload_to_report(
        "Recherche locale",
        payload,
        stats={
            "patterns": len(patterns),
            "matches_kept": len(matches),
            "matches_total": total,
            "files": len(counts),
            "source_caracteres": len(match_lines) + len(snippets),
        },
    )
    report = _verify_paths(report, set(counts), config)
    return _flag_partial_sample(
        report,
        [f"{len(matches)} correspondances examinées sur {total}" if len(matches) < total else ""]
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
        return Report(
            title=f"Analyse locale ({mode})",
            summary=f"Aucun fichier analysable sous {path or '.'} après application des garde-fous.",
            next_actions=["Vérifier le chemin ou assouplir les filtres --glob"],
        )

    chunk_set = build_chunks(config, files)
    chunks = chunk_set.parts

    # Contenu plus court qu'une synthèse : le rendre tel quel et laisser l'orchestrateur juger.
    joined = "\n".join(chunks)
    if not budget.is_worth_delegating(joined, budget.PASSTHROUGH_CONTENT_CHARS):
        return budget.passthrough(
            f"Analyse locale ({mode})",
            joined,
            reason=f"{len(files)} fichier(s) pour {len(joined.strip())} caractères, plus court qu'une synthèse",
            stats={"files_examined": len(files), "files_found": total},
            details=f"Consigne non déléguée : {instruction}"
        )

    payloads: list[dict] = []
    errors: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        prompt = (
            f"Consigne : {instruction}\n"
            f"Cadre : {preset}\n"
            f"Lot {index}/{len(chunks)} de fichiers du dépôt (lignes numérotées).\n\n"
            f"{chunk}\n\n" + prompts.JSON_CONTRACT
        )
        try:
            payloads.append(_ask(client, prompts.SYSTEM_ANALYST, prompt))
        except MlxError as error:
            errors.append(f"lot {index} : {error}")

    if not payloads:
        return Report(
            title=f"Analyse locale ({mode})",
            summary="Le modèle local n'a produit aucune analyse exploitable.",
            errors=errors,
        )

    merged = prompts.merge_payloads(payloads)
    if len(payloads) > 1:
        digest = "\n".join(
            f"- lot {index}: {p.get('summary')} | " + " ; ".join(list(p.get("findings") or [])[:5])
            for index, p in enumerate(payloads, start=1)
        )
        prompt = (
            f"Consigne initiale : {instruction}\n\n"
            f"Analyses partielles par lot :\n{digest}\n\n"
            f"Fichiers concernés repérés : {', '.join(list(merged.get('files') or [])[:30])}\n\n"
            "Produis une synthèse unique, dédoublonnée et hiérarchisée par importance. Sois bref, "
            "l'orchestrateur n'a que peu de contexte disponible.\n\n" + prompts.JSON_CONTRACT
        )
        try:
            merged = prompts.merge_payloads(
                [_ask(client, prompts.SYSTEM_ANALYST, prompt, max_tokens=max(config.max_completion_tokens, 2600))]
            )
        except MlxError as error:
            errors.append(f"synthèse : {error}")

    report = _payload_to_report(
        f"Analyse locale ({mode})",
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
            f"{total - len(files)} fichiers non analysés (limite de {max_files or config.max_files} fichiers par appel)"
        )
    report = _verify_paths(report, {item.relative for item in files}, config)
    return _flag_partial_sample(
        report,
        [
            f"{chunk_set.files_included} fichiers lus sur {total} trouvés"
            if total > chunk_set.files_included else "",
            f"{chunk_set.files_truncated} fichiers coupés faute de place"
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
            title="Analyse de logs",
            summary=f"Aucune ligne d'erreur détectée dans {path} ({size} octets scannés).",
            stats={"matches": 0},
            next_actions=["Fournir des motifs explicites via patterns si le format de log est atypique"],
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
        f"Consigne : {task or 'Identifie les erreurs dominantes, leurs causes probables et ce qui mérite investigation.'}\n"
        f"Source : {path}\n"
        f"{len(matches)} lignes retenues sur {total} correspondances, regroupées en {len(clusters)} signatures.\n\n"
        f"Signatures les plus fréquentes :\n{digest}\n\n"
        "Regroupe par cause racine probable, distingue le bruit récurrent des anomalies réelles.\n\n"
        + prompts.JSON_CONTRACT
    )
    payload = _ask(client, prompts.SYSTEM_ANALYST, prompt)
    report = _payload_to_report(
        "Analyse de logs",
        payload,
        stats={
            "lines_matched": len(matches),
            "signatures": len(clusters),
            "patterns": len(used_patterns),
            "source_caracteres": sum(len(match["text"]) for match in matches),
        },
    )
    report = _verify_paths(report, {match["file"] for match in matches}, config)
    return _flag_partial_sample(
        report,
        [
            f"{len(matches)} lignes retenues sur {total}" if len(matches) < total else "",
            f"{len(ranked)} signatures examinées sur {len(clusters)}" if len(ranked) < len(clusters) else ""
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
            f"aucun contrôle défini pour ce dépôt : déclarer des checks dans {shell.CHECKS_FILE} à la racine"
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
        title=f"Contrôle local : {spec['label']}",
        stats=stats,
        details=f"Commande : `{result.command}`",
    )

    if result.exit_code == 0 and not filtered.strip():
        base.summary = "Contrôle passé."
        base.stats = {"exit_code": 0}
        base.details = ""
        return base
    if result.timed_out:
        base.summary = f"Commande interrompue par timeout après {config.command_timeout}s."
        base.risks = ["Résultat partiel, augmenter LOCAL_AGENT_COMMAND_TIMEOUT si nécessaire"]
        return base
    if not filtered.strip():
        base.summary = f"Commande sortie en code {result.exit_code} sans sortie exploitable."
        return base

    # Sortie déjà courte : la synthèse pèserait plus lourd que la sortie qu'elle résume.
    if not budget.is_worth_delegating(filtered):
        return budget.passthrough(
            base.title,
            filtered,
            reason=f"sortie de {len(filtered.strip())} caractères, déjà plus courte qu'une synthèse",
            stats=stats,
            details=base.details
        )

    prompt = (
        f"Sortie filtrée de : {result.command}\n"
        f"Code de sortie : {result.exit_code}\n"
        f"Lignes conservées : {stats['lines_kept']} sur {stats['lines_total']}\n\n"
        f"{filtered}\n\n"
        "Classe les problèmes par nature et par fichier, indique les causes probables et ce qui bloque. "
        "Ne recopie pas les stack traces.\n\n" + prompts.JSON_CONTRACT
    )
    try:
        payload = _ask(client, prompts.SYSTEM_ANALYST, prompt)
    except MlxError as error:
        base.summary = f"Commande exécutée (code {result.exit_code}) mais synthèse locale indisponible."
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
        raise ValueError(f"périmètre inconnu : {scope}. Disponibles : {', '.join(sorted(DIFF_SCOPES))}")
    args = list(DIFF_SCOPES[scope])
    if scope == "branch":
        reference = base or next(
            (name for name in ("main", "master", "develop")
             if shell.git(config, ["rev-parse", "--verify", "--quiet", name]).exit_code == 0),
            None
        )
        if not reference:
            raise ValueError("aucune branche de base trouvée (main, master, develop) : préciser base")
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
        raise ValueError(f"git {' '.join(args)} a échoué : {result.stderr.strip()[:300]}")
    diff = result.stdout
    title = f"Revue de diff ({scope})"

    if not diff.strip():
        return Report(
            title=title,
            summary=f"Aucun changement dans le périmètre {scope}.",
            stats={"files": 0},
            next_actions=["Vérifier le périmètre : worktree, staged, ou branch avec base"],
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
            block = block[: config.chunk_chars].rsplit("\n", 1)[0] + "\n[... fichier tronqué ...]\n"
        packed.append(block)
        used += len(block)

    instruction = (
        task or (
            "Fais une revue de ces changements : bugs probables, cas limites oubliés, restes de débogage, "
            "incohérences avec le code environnant visible dans le diff, risques de régression. "
            "Ignore le style cosmétique."
        )
    ) + " Ajoute en dernière ligne de next_actions une proposition de message de commit préfixée « Commit : »."
    prompt = (
        f"Consigne : {instruction}\n\n"
        f"Diff git ({scope}, {len(sections)} fichiers) :\n\n"
        + "".join(packed)
        + "\n\n" + prompts.JSON_CONTRACT
    )
    payload = _ask(client, prompts.SYSTEM_ANALYST, prompt)
    report = _payload_to_report(
        title,
        payload,
        stats={"files": len(sections), "files_reviewed": len(packed), "source_caracteres": len(diff)},
    )
    report = _verify_paths(report, {name for name, _ in sections}, config)
    return _flag_partial_sample(
        report,
        [f"{len(omitted)} fichiers non examinés faute de place : {', '.join(omitted[:5])}" if omitted else ""]
    )
