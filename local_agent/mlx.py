"""Client HTTP pour un serveur mlx-serve exposant une API compatible OpenAI."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .config import Config

THINK_BLOCK = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.DOTALL | re.IGNORECASE)
DANGLING_THINK = re.compile(r"^\s*<(think|thinking|reasoning)>.*", re.DOTALL | re.IGNORECASE)


class MlxError(RuntimeError):
    pass


@dataclass
class Completion:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tool_calls: list[dict] | None = None
    raw_message: dict | None = None


def _strip_reasoning(text: str) -> str:
    cleaned = THINK_BLOCK.sub("", text)
    if "</think>" in cleaned.lower():
        cleaned = re.split(r"</think>", cleaned, flags=re.IGNORECASE)[-1]
    elif DANGLING_THINK.match(cleaned):
        cleaned = ""
    return cleaned.strip()


def image_part(path: Path) -> dict:
    """Chemin fichier, jamais du base64 : Qwen3.x / mlx-vlm boucle ou ignore les data-URI."""
    return {"type": "image_url", "image_url": {"url": str(Path(path).resolve())}}


def user_message(prompt: str, images: list[Path] | None = None) -> dict:
    if not images:
        return {"role": "user", "content": prompt}
    content: list[dict] = [{"type": "text", "text": prompt}]
    content.extend(image_part(path) for path in images)
    return {"role": "user", "content": content}


class MlxClient:
    def __init__(self, config: Config) -> None:
        self.config = config

    def _request(self, path: str, payload: dict | None = None, timeout: int | None = None) -> dict:
        url = f"{self.config.base_url}{path}"
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.config.timeout) as response:
                return json.loads(response.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:400]
            raise MlxError(f"HTTP {error.code} on {path}: {detail}") from error
        except urllib.error.URLError as error:
            raise MlxError(
                f"local LLM server unreachable at {self.config.base_url} ({error.reason}). "
                "Check that the server is running and LOCAL_LLM_BASE_URL is correct."
            ) from error
        except TimeoutError as error:
            raise MlxError(f"timeout after {timeout or self.config.timeout}s on {path}") from error
        except json.JSONDecodeError as error:
            raise MlxError(f"non-JSON response on {path}") from error

    def models(self) -> list[dict]:
        payload = self._request("/models", timeout=8)
        return payload.get("data", []) or []

    def loaded_info(self) -> dict:
        try:
            for model in self.models():
                if model.get("loaded") or model.get("state") == "ready":
                    return model
        except MlxError:
            pass
        return {}

    def resolve_model(self) -> str:
        configured = self.config.model
        if configured and configured != "auto":
            return configured
        info = self.loaded_info()
        if info.get("id"):
            return str(info["id"])
        return "local"

    def supports_vision(self) -> bool:
        info = self.loaded_info()
        caps = [str(item).lower() for item in (info.get("capabilities") or [])]
        mods = [str(item).lower() for item in (info.get("input_modalities") or [])]
        return "vision" in caps or "image" in mods

    def capabilities(self) -> dict:
        """Probe the OpenAI-compatible server; MLX is the preferred backend, not a hard dependency."""
        info = self.loaded_info()
        caps = [str(item).lower() for item in (info.get("capabilities") or [])]
        meta = info.get("meta") if isinstance(info.get("meta"), dict) else {}
        context = (
            meta.get("model_max_tokens")
            or info.get("max_model_len")
            or info.get("context_length")
            or self.config.max_context
        )
        return {
            "provider": self.config.provider,
            "base_url": self.config.base_url,
            "id": info.get("id") or self.resolve_model(),
            "loaded": bool(info),
            "capabilities": caps,
            "tool_use": "tool_use" in caps or "tools" in caps,
            "vision": self.supports_vision(),
            "json_schema": "json_schema" in caps,
            "reasoning": "reasoning" in caps,
            "streaming": "streaming" in caps,
            "context_length": int(context or 0),
            "input_modalities": info.get("input_modalities") or ["text"],
        }

    def ping(self) -> dict:
        models = self.models()
        loaded = [m for m in models if m.get("loaded") or m.get("state") == "ready"]
        completion = self.complete("Reply with exactly: PONG", "You are a ping service.", max_tokens=8, timeout=60)
        return {
            "base_url": self.config.base_url,
            "model": self.resolve_model(),
            "loaded_models": [m.get("id") for m in loaded],
            "available_models": len(models),
            "vision": self.supports_vision(),
            "capabilities": self.capabilities(),
            "echo": completion.text[:40],
        }

    def complete(
        self,
        prompt: str,
        system: str,
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout: int | None = None,
        images: list[Path] | None = None,
    ) -> Completion:
        payload = {
            "model": self.resolve_model(),
            "messages": [
                {"role": "system", "content": system},
                user_message(prompt, images),
            ],
            "temperature": self.config.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.config.max_completion_tokens,
            "stream": False,
        }
        data = self._request("/chat/completions", payload, timeout=timeout)
        return self._parse_completion(data)

    def complete_chat(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout: int | None = None,
        images: list[Path] | None = None,
    ) -> Completion:
        """Multi-turn chat, optional OpenAI tool calling. Used by the local_task loop."""
        payload: dict = {
            "model": self.resolve_model(),
            "messages": messages,
            "temperature": self.config.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.config.max_completion_tokens,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        data = self._request("/chat/completions", payload, timeout=timeout)
        return self._parse_completion(data)

    def _parse_completion(self, data: dict) -> Completion:
        choices = data.get("choices") or []
        if not choices:
            raise MlxError("LLM response had no usable choice")
        message = choices[0].get("message") or {}
        text = message.get("content") or ""
        if not text and message.get("reasoning_content"):
            text = str(message["reasoning_content"])
        usage = data.get("usage") or {}
        tool_calls = message.get("tool_calls") or []
        return Completion(
            text=_strip_reasoning(str(text)),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            tool_calls=list(tool_calls) if tool_calls else None,
            raw_message=message,
        )
