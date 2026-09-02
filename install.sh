#!/usr/bin/env bash
# Enregistre le serveur MCP local-agent pour Claude Code et Cursor, sans écraser l'existant.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for tool in python3 rg git; do
    if ! command -v "$tool" > /dev/null 2>&1; then
        echo "prérequis manquant : $tool" >&2
        exit 1
    fi
done

python3 - "$ROOT" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
server = {"command": str(root / "bin" / "local-agent-mcp")}


def register(path: Path, label: str) -> None:
    payload = {}
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"{label} : {path} illisible, non modifié. L'entrée à ajouter est :")
            print(json.dumps({"mcpServers": {"local-agent": server}}, indent=2))
            return
    servers = payload.setdefault("mcpServers", {})
    if "local-agent" in servers:
        print(f"{label} : déjà enregistré, configuration existante préservée.")
        return
    servers["local-agent"] = server
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{label} : enregistré dans {path}.")


register(Path.home() / ".claude.json", "Claude Code")
register(Path.home() / ".cursor" / "mcp.json", "Cursor")
PY

echo
python3 "$ROOT/bin/local-agent" doctor || true
echo
echo "Terminé. Redémarrer Claude Code et Cursor, puis vérifier avec l'outil local_ping."
echo "Serveur local attendu sur http://127.0.0.1:11234/v1 (surcharger via LOCAL_LLM_BASE_URL)."
echo "Outil principal : local_task (mission + sources). Les outils fins restent disponibles."
