"""Client HTTP pour un serveur mlx-serve exposant une API compatible OpenAI."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

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


def _strip_reasoning(text: str) -> str:
    cleaned = THINK_BLOCK.sub("", text)
    if "</think>" in cleaned.lower():
        cleaned = re.split(r"</think>", cleaned, flags=re.IGNORECASE)[-1]
    elif DANGLING_THINK.match(cleaned):
        cleaned = ""
    return cleaned.strip()


class MlxClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._resolved_model: str | None = None

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
            raise MlxError(f"HTTP {error.code} sur {path} : {detail}") from error
        except urllib.error.URLError as error:
            raise MlxError(
                f"serveur MLX injoignable sur {self.config.base_url} ({error.reason}). "
                "Vérifier que le serveur local tourne et que LOCAL_LLM_BASE_URL est correct."
            ) from error
        except TimeoutError as error:
            raise MlxError(f"timeout après {timeout or self.config.timeout}s sur {path}") from error
        except json.JSONDecodeError as error:
            raise MlxError(f"réponse non JSON sur {path}") from error

    def models(self) -> list[dict]:
        payload = self._request("/models", timeout=15)
        return payload.get("data", []) or []

    def resolve_model(self) -> str:
        if self._resolved_model:
            return self._resolved_model
        configured = self.config.model
        if configured and configured != "auto":
            self._resolved_model = configured
            return configured
        try:
            for model in self.models():
                if model.get("loaded") or model.get("state") == "ready":
                    self._resolved_model = str(model.get("id"))
                    return self._resolved_model
        except MlxError:
            pass
        self._resolved_model = "local"
        return self._resolved_model

    def ping(self) -> dict:
        models = self.models()
        loaded = [m for m in models if m.get("loaded") or m.get("state") == "ready"]
        completion = self.complete("Réponds exactement: PONG", "Tu es un service de test.", max_tokens=8, timeout=60)
        return {
            "base_url": self.config.base_url,
            "model": self.resolve_model(),
            "loaded_models": [m.get("id") for m in loaded],
            "available_models": len(models),
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
    ) -> Completion:
        payload = {
            "model": self.resolve_model(),
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": self.config.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.config.max_completion_tokens,
            "stream": False,
        }
        data = self._request("/chat/completions", payload, timeout=timeout)
        choices = data.get("choices") or []
        if not choices:
            raise MlxError("réponse MLX sans choix exploitable")
        message = choices[0].get("message") or {}
        text = message.get("content") or ""
        if not text and message.get("reasoning_content"):
            text = str(message["reasoning_content"])
        usage = data.get("usage") or {}
        return Completion(
            text=_strip_reasoning(str(text)),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )
