"""Configuration du local-agent, pilotable par variables d'environnement."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

ENV_FILE_NAME = "local-agent.env"


def _tool_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _repo_root() -> Path:
    """L'outil vit hors des dépôts : la racine se déduit du répertoire courant."""
    override = (os.environ.get("LOCAL_AGENT_REPO_ROOT") or "").strip()
    # Cursor ne substitue pas ${workspaceFolder} : un gabarit non résolu vaut absence de valeur.
    if override and "${" not in override:
        return Path(override).expanduser().resolve()
    try:
        process = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        if process.returncode == 0 and process.stdout.strip():
            return Path(process.stdout.strip()).resolve()
    except (OSError, subprocess.SubprocessError):
        pass
    return Path.cwd().resolve()


def load_env_file() -> None:
    """Charge tools/local-agent/local-agent.env sans écraser l'environnement réel."""
    path = _tool_root() / ENV_FILE_NAME
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def _raw_env(*names: str) -> str:
    """Première variable renseignée parmi les alias : LOCAL_LLM_* d'abord, MLX_* en rétrocompatibilité."""
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def _int_env(name: str, default: int) -> int:
    try:
        return int(_raw_env(name, name.replace("LOCAL_LLM_", "MLX_")) or default)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(_raw_env(name, name.replace("LOCAL_LLM_", "MLX_")) or default)
    except ValueError:
        return default


def _str_env(name: str, default: str) -> str:
    return _raw_env(name, name.replace("LOCAL_LLM_", "MLX_")) or default


@dataclass
class Config:
    repo_root: Path = field(default_factory=_repo_root)

    # Tout serveur compatible OpenAI convient : mlx-serve, Ollama, llama.cpp, LM Studio, vLLM.
    base_url: str = field(
        default_factory=lambda: _str_env("LOCAL_LLM_BASE_URL", "http://127.0.0.1:11234/v1").rstrip("/")
    )
    model: str = field(default_factory=lambda: _str_env("LOCAL_LLM_MODEL", "auto"))
    api_key: str = field(default_factory=lambda: _str_env("LOCAL_LLM_API_KEY", ""))
    # Une analyse n'a rien à gagner à varier. Sans rendre les réponses reproductibles pour autant : le
    # serveur ne suit pas le même chemin numérique selon que le prompt est traité à froid ou en cache.
    temperature: float = field(default_factory=lambda: _float_env("LOCAL_LLM_TEMPERATURE", 0.0))
    timeout: int = field(default_factory=lambda: _int_env("LOCAL_LLM_TIMEOUT", 300))
    max_completion_tokens: int = field(default_factory=lambda: _int_env("LOCAL_LLM_MAX_TOKENS", 1600))

    max_files: int = field(default_factory=lambda: _int_env("LOCAL_AGENT_MAX_FILES", 40))
    max_file_size: int = field(default_factory=lambda: _int_env("LOCAL_AGENT_MAX_FILE_SIZE", 120_000))
    max_output_tokens: int = field(default_factory=lambda: _int_env("LOCAL_AGENT_MAX_OUTPUT_TOKENS", 900))
    # Le prétraitement de l'entrée domine la latence : mesuré, 10 000 caractères coûtent 6,5 s contre
    # 3,9 s pour 4 400, sans perte de justesse sur le banc. Élargir si une réponse manque de contexte.
    chunk_chars: int = field(default_factory=lambda: _int_env("LOCAL_AGENT_CHUNK_CHARS", 12_000))
    max_chunks: int = field(default_factory=lambda: _int_env("LOCAL_AGENT_MAX_CHUNKS", 8))
    max_matches: int = field(default_factory=lambda: _int_env("LOCAL_AGENT_MAX_MATCHES", 200))
    fix_max_file_size: int = field(default_factory=lambda: _int_env("LOCAL_AGENT_FIX_MAX_FILE_SIZE", 40_000))
    command_timeout: int = field(default_factory=lambda: _int_env("LOCAL_AGENT_COMMAND_TIMEOUT", 900))
    compound_turns: int = field(
        default_factory=lambda: _int_env("LOCAL_AGENT_COMPOUND_TURNS", 25)
    )

    @property
    def max_output_chars(self) -> int:
        return max(1200, self.max_output_tokens * 4)

    def as_summary(self) -> dict[str, object]:
        return {
            "repo_root": str(self.repo_root),
            "base_url": self.base_url,
            "model": self.model,
            "temperature": self.temperature,
            "timeout": self.timeout,
            "max_files": self.max_files,
            "max_file_size": self.max_file_size,
            "max_output_tokens": self.max_output_tokens,
            "chunk_chars": self.chunk_chars,
            "compound_turns": self.compound_turns,
            "api_key_set": bool(self.api_key),
        }


def get_config() -> Config:
    load_env_file()
    return Config()
