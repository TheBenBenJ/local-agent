#!/usr/bin/env python3
"""Sonde de bout en bout du serveur MCP local-agent.

Lance le serveur en stdio, joue une poignée d'appels et vérifie que chaque réponse reste compacte.

Les appels vivent dans `cases.json`, sous la clé `sonde`, et visent ce dépôt même par défaut. Pour
sonder un dépôt de travail avec ses propres chemins, copier ce fichier en `cases.local.json` (ignoré
par git) et passer `--cases`.

    python3 ~/.local-agent/tests/mcp_probe.py
    python3 ~/.local-agent/tests/mcp_probe.py --quick
    python3 ~/.local-agent/tests/mcp_probe.py --repo /chemin/vers/le/depot
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parent.parent
SERVER = TOOL_ROOT / "bin" / "local-agent-mcp"
COMPACT_LIMIT = 4000

DEFAULT_CASES = TOOL_ROOT / "tests" / "cases.json"


def load_calls(cases_file: Path, quick: bool) -> list[tuple[str, dict]]:
    payload = json.loads(cases_file.read_text()).get("sonde") or {}
    entries = payload.get("quick" if quick else "full") or []
    return [(entry["outil"], entry.get("arguments") or {}) for entry in entries]


def resolve_repo(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    process = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    if process.returncode == 0 and process.stdout.strip():
        return Path(process.stdout.strip()).resolve()
    return Path.cwd().resolve()


def probe(calls: list[tuple[str, dict]], repo_root: Path) -> int:
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    ]
    for index, (name, arguments) in enumerate(calls, start=2):
        requests.append(
            {"jsonrpc": "2.0", "id": index, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        )

    payload = "".join(json.dumps(request) + "\n" for request in requests)
    started = time.time()
    process = subprocess.run(
        [sys.executable, str(SERVER)],
        cwd=str(repo_root),
        input=payload,
        capture_output=True,
        text=True,
        timeout=3600,
    )
    elapsed = time.time() - started

    responses = {}
    for line in process.stdout.splitlines():
        if not line.strip():
            continue
        message = json.loads(line)
        responses[message.get("id")] = message

    failures: list[str] = []
    tools = responses.get(1, {}).get("result", {}).get("tools", [])
    print(f"outils exposés : {len(tools)} -> {', '.join(tool['name'] for tool in tools)}")
    if not tools:
        failures.append("tools/list vide")
    names = {tool.get("name") for tool in tools}
    required = {"local_task", "local_expand", "local_metrics", "local_image_compare"}
    missing = sorted(required - names)
    if missing:
        failures.append("tools/list manque : " + ", ".join(missing))

    for index, (name, arguments) in enumerate(calls, start=2):
        message = responses.get(index)
        if message is None:
            failures.append(f"{name} : aucune réponse")
            continue
        result = message.get("result", {})
        text = result.get("content", [{}])[0].get("text", "")
        size = len(text)
        flag = "erreur" if result.get("isError") else "ok"
        expected_error = arguments.get("path") == "../../etc"
        status = "OK"
        if size > COMPACT_LIMIT:
            status = "TROP VERBEUX"
            failures.append(f"{name} : {size} caractères")
        if result.get("isError") and not expected_error:
            status = "ECHEC"
            failures.append(f"{name} : {text.splitlines()[0][:120]}")
        if expected_error and not result.get("isError"):
            status = "CONFINEMENT NON APPLIQUE"
            failures.append(f"{name} : chemin hors dépôt accepté")
        print(f"  {status:<26} {name:<20} {flag:<7} {size:>5} caractères  {json.dumps(arguments, ensure_ascii=False)[:70]}")

    print(f"\ndurée totale : {elapsed:.1f}s")
    if process.stderr.strip():
        print(f"stderr du serveur :\n{process.stderr[:1500]}")
    if failures:
        print("\nÉCHECS :")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nToutes les réponses sont compactes et sans erreur inattendue.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true", help="ne joue que le ping et une recherche")
    parser.add_argument("--repo", default=None, help="racine du dépôt à sonder (défaut : dépôt courant)")
    parser.add_argument("--cases", default=str(DEFAULT_CASES), help="fichier JSON des appels de sonde")
    options = parser.parse_args()
    repo = resolve_repo(options.repo)
    calls = load_calls(Path(options.cases), options.quick)
    print(f"dépôt sondé : {repo}")
    print(f"cas         : {options.cases}")
    print(f"serveur     : {SERVER}\n")
    sys.exit(probe(calls, repo))
