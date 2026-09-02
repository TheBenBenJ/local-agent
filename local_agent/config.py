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


def _bool_env(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}


def _first_bool(*names: str, default: bool) -> bool:
    for name in names:
        raw = (os.environ.get(name) or "").strip().lower()
        if raw:
            return raw not in {"0", "false", "no", "off"}
    return default


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
    vision: bool = field(
        default_factory=lambda: _first_bool("LOCAL_AGENT_ENABLE_VISION", "LOCAL_AGENT_VISION", default=True)
    )
    provider: str = field(default_factory=lambda: _str_env("LOCAL_LLM_PROVIDER", "mlx"))
    max_context: int = field(default_factory=lambda: _int_env("LOCAL_LLM_MAX_CONTEXT", 0))
    max_steps: int = field(default_factory=lambda: _int_env("LOCAL_AGENT_MAX_STEPS", 12))
    max_tool_calls: int = field(default_factory=lambda: _int_env("LOCAL_AGENT_MAX_TOOL_CALLS", 24))
    max_runtime: int = field(default_factory=lambda: _int_env("LOCAL_AGENT_MAX_RUNTIME", 180))
    output_budget: int = field(default_factory=lambda: _int_env("LOCAL_AGENT_OUTPUT_BUDGET", 1500))
    local_context_budget: int = field(
        default_factory=lambda: _int_env("LOCAL_AGENT_LOCAL_CONTEXT_BUDGET", 48_000)
    )
    autonomy: str = field(default_factory=lambda: _str_env("LOCAL_AGENT_AUTONOMY", "read_only"))
    confidence_threshold: float = field(
        default_factory=lambda: _float_env("LOCAL_AGENT_CONFIDENCE_THRESHOLD", 0.7)
    )
    enable_cache: bool = field(default_factory=lambda: _bool_env("LOCAL_AGENT_ENABLE_CACHE", True))
    max_retries: int = field(default_factory=lambda: _int_env("LOCAL_AGENT_MAX_RETRIES", 2))
    # Tokens of raw source below which a local LLM must not run. 2000 ≈ 8 kB.
    direct_context_threshold: int = field(
        default_factory=lambda: _int_env("LOCAL_AGENT_DIRECT_CONTEXT_THRESHOLD", 2000)
    )
    force_tier: str = field(default_factory=lambda: _str_env("LOCAL_AGENT_FORCE_TIER", "").lower())

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
            "vision": self.vision,
            "provider": self.provider,
            "max_steps": self.max_steps,
            "max_tool_calls": self.max_tool_calls,
            "max_runtime": self.max_runtime,
            "output_budget": self.output_budget,
            "local_context_budget": self.local_context_budget,
            "autonomy": self.autonomy,
            "confidence_threshold": self.confidence_threshold,
            "max_retries": self.max_retries,
            "enable_cache": self.enable_cache,
            "direct_context_threshold": self.direct_context_threshold,
            "force_tier": self.force_tier,
            "api_key_set": bool(self.api_key),
        }


def get_config() -> Config:
    load_env_file()
    return Config()
