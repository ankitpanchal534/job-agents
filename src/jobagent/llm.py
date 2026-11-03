from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Mapping, Optional

from jobagent.config import Settings, resolve_task


USER_AGENT = "jobagent/0.1"


class LLMError(Exception):
    pass


class ChatClient:
    def __init__(self, settings: Settings, env: Optional[Mapping[str, str]] = None, timeout: float = 120):
        self._settings = settings
        self._env = os.environ if env is None else env
        self._timeout = timeout

    def complete(self, task: str, system: str, user: str) -> str:
        resolved = resolve_task(self._settings, task)
        body = {
            "model": resolved.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": resolved.temperature,
            "max_tokens": resolved.max_tokens,
        }
        request = urllib.request.Request(
            resolved.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers=self._headers(resolved.provider, resolved.api_key_env),
            method="POST",
        )
        data = self._send(request, resolved.provider, resolved.base_url)
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise LLMError(f"unexpected response from {resolved.provider}: {str(data)[:200]}") from error

    def list_models(self, provider: Optional[str] = None) -> list[str]:
        name = provider or self._settings.llm.provider
        config = self._settings.llm.providers[name]
        request = urllib.request.Request(
            config.base_url.rstrip("/") + "/models",
            headers=self._headers(name, config.api_key_env),
            method="GET",
        )
        data = self._send(request, name, config.base_url)
        try:
            return sorted(item["id"] for item in data["data"])
        except (KeyError, TypeError) as error:
            raise LLMError(f"unexpected response from {name}: {str(data)[:200]}") from error

    def _headers(self, provider: str, api_key_env: Optional[str]) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        if api_key_env:
            key = self._env.get(api_key_env)
            if not key:
                raise LLMError(f"{api_key_env} is not set (needed by the {provider} provider)")
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _send(self, request: urllib.request.Request, provider: str, base_url: str) -> dict:
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise LLMError(f"{provider} returned HTTP {error.code}: {error.read()[:200]!r}") from error
        except urllib.error.URLError as error:
            raise LLMError(f"cannot reach {base_url}: {error.reason}") from error
