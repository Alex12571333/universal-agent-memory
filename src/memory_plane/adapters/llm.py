"""Memory LLM runtime configuration.

This is intentionally separate from embedding configuration. Embeddings power
search; the memory LLM powers future Navigator/Curator reasoning.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from memory_plane.config.secrets import read_secret_env


@dataclass(frozen=True, slots=True)
class MemoryLLMConfig:
    """Docker-friendly OpenAI-compatible chat model settings for memory workers."""

    provider: str = "openai-compatible"
    model_name: str = "gpt-5.6-terra"
    base_url: str = "https://api.openai.com/v1"
    api_key: str | None = None
    timeout_seconds: float = 60.0
    temperature: float = 0.1
    context_window_tokens: int = 131072
    max_tokens: int = 1600
    enable_thinking: bool = False

    @classmethod
    def from_env(cls) -> MemoryLLMConfig:
        """Build memory LLM config from `UAM_MEMORY_LLM_*` env vars."""
        return cls(
            provider=os.getenv("UAM_MEMORY_LLM_PROVIDER", "openai-compatible").strip().lower(),
            model_name=os.getenv("UAM_MEMORY_LLM_MODEL", "gpt-5.6-terra").strip(),
            base_url=os.getenv(
                "UAM_MEMORY_LLM_BASE_URL",
                "https://api.openai.com/v1",
            ).rstrip("/"),
            api_key=read_secret_env(
                "UAM_MEMORY_LLM_API_KEY",
                "OPENAI_API_KEY",
                "SPARK_API_KEY",
            ),
            timeout_seconds=float(os.getenv("UAM_MEMORY_LLM_TIMEOUT_SECONDS", "60")),
            temperature=float(os.getenv("UAM_MEMORY_LLM_TEMPERATURE", "0.1")),
            context_window_tokens=int(
                os.getenv("UAM_MEMORY_LLM_CONTEXT_TOKENS", "131072")
            ),
            max_tokens=int(os.getenv("UAM_MEMORY_LLM_MAX_TOKENS", "1600")),
            enable_thinking=_env_bool("UAM_MEMORY_LLM_ENABLE_THINKING", default=False),
        )

    def public_dict(self) -> dict[str, object]:
        """Return non-secret config values for status/docs endpoints."""
        return {
            "provider": self.provider,
            "model_name": self.model_name,
            "base_url": self.base_url,
            "timeout_seconds": self.timeout_seconds,
            "temperature": self.temperature,
            "context_window_tokens": self.context_window_tokens,
            "max_tokens": self.max_tokens,
            "enable_thinking": self.enable_thinking,
            "api_key_configured": bool(self.api_key),
        }


class MemoryLLMError(RuntimeError):
    """Raised when the memory LLM endpoint fails or returns invalid data."""


class MemoryLLMClient:
    """Small OpenAI-compatible chat client for memory reasoning workers.

    The client is intentionally dependency-light: Docker deployments can point it
    at llama.cpp, vLLM, LiteLLM, Spark, or any other server exposing
    `/chat/completions`.
    """

    def __init__(self, config: MemoryLLMConfig | None = None) -> None:
        self.config = config or MemoryLLMConfig.from_env()

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict[str, str] | None = None,
    ) -> str:
        """Return assistant text for OpenAI-compatible chat messages."""
        payload: dict[str, Any] = {
            "model": self.config.model_name,
            "messages": messages,
            "temperature": (
                self.config.temperature if temperature is None else temperature
            ),
            "max_tokens": self.config.max_tokens if max_tokens is None else max_tokens,
        }
        if response_format is not None:
            payload["response_format"] = response_format
        if self.config.provider in {"spark", "vllm", "qwen"}:
            payload["chat_template_kwargs"] = {
                "enable_thinking": self.config.enable_thinking
            }

        body = self._post_json("/chat/completions", payload)
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise MemoryLLMError("Memory LLM response does not contain choices")

        first = choices[0]
        if not isinstance(first, dict):
            raise MemoryLLMError("Memory LLM choice is not an object")
        message = first.get("message")
        if not isinstance(message, dict):
            raise MemoryLLMError("Memory LLM choice does not contain message")
        content = message.get("content")
        if not isinstance(content, str):
            raise MemoryLLMError(
                "Memory LLM message content is not text; "
                "the model may have exhausted max_tokens while still reasoning"
            )
        return content.strip()

    def chat_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = 0.0,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Return a JSON object produced by the memory LLM."""
        content = self.chat(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
        try:
            parsed = json.loads(_strip_json_fence(content))
        except json.JSONDecodeError as exc:
            raise MemoryLLMError("Memory LLM returned invalid JSON") from exc
        if not isinstance(parsed, dict):
            raise MemoryLLMError("Memory LLM JSON response is not an object")
        return parsed

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.config.base_url}{path}"
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        request = Request(url, data=data, headers=headers, method="POST")

        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise MemoryLLMError(
                f"Memory LLM HTTP {exc.code}: {details[:500]}"
            ) from exc
        except URLError as exc:
            raise MemoryLLMError(f"Memory LLM is unreachable: {exc.reason}") from exc

        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MemoryLLMError("Memory LLM returned invalid response JSON") from exc
        if not isinstance(decoded, dict):
            raise MemoryLLMError("Memory LLM response is not an object")
        return decoded


def build_memory_llm_client(
    config: MemoryLLMConfig | None = None,
) -> MemoryLLMClient:
    """Build the default memory LLM client from runtime config."""
    return MemoryLLMClient(config=config)


def _strip_json_fence(content: str) -> str:
    text = content.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _env_bool(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
