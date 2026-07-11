"""Memory LLM runtime configuration.

This is intentionally separate from embedding configuration. Embeddings power
search; the memory LLM powers future Navigator/Curator reasoning.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from memory_plane.config.secrets import read_secret_env

_SUPPORTED_PROVIDERS = frozenset({"openai-compatible", "openai"})
_OPENAI_API_BASE_URL = "https://api.openai.com/v1"
_COMPATIBLE_API_BASE_URL = "http://localhost:8000/v1"
_RESERVED_EXTRA_BODY_FIELDS = frozenset(
    {
        "api_key",
        "authorization",
        "base_url",
        "endpoint",
        "function_call",
        "functions",
        "headers",
        "max_completion_tokens",
        "max_tokens",
        "messages",
        "model",
        "parallel_tool_calls",
        "response_format",
        "stream",
        "stream_options",
        "temperature",
        "tool_choice",
        "tools",
        "url",
        "user",
    }
)


class _RejectRedirects(HTTPRedirectHandler):
    """Prevent a trusted model gateway from redirecting to another origin."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_NO_REDIRECT_OPENER = build_opener(_RejectRedirects())


def urlopen(request: Request, timeout: float) -> Any:
    """Open one model request without following HTTP redirects."""
    return _NO_REDIRECT_OPENER.open(request, timeout=timeout)


@dataclass(frozen=True, slots=True)
class MemoryLLMConfig:
    """Docker-friendly OpenAI-compatible chat model settings for memory workers."""

    provider: str = "openai-compatible"
    model_name: str = "memory-model"
    base_url: str = _COMPATIBLE_API_BASE_URL
    api_key: str | None = None
    timeout_seconds: float = 60.0
    temperature: float = 0.1
    context_window_tokens: int = 8192
    max_tokens: int = 1200
    extra_body: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        """Normalize and validate configuration before any request can be sent."""
        if not isinstance(self.provider, str):
            raise ValueError("memory LLM provider must be a string")
        if not isinstance(self.model_name, str):
            raise ValueError("memory LLM model name must be a string")
        provider = self.provider.strip().lower()
        model_name = self.model_name.strip()
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "model_name", model_name)
        object.__setattr__(self, "base_url", _validate_openai_base_url(self.base_url))

        if provider not in _SUPPORTED_PROVIDERS:
            supported = ", ".join(sorted(_SUPPORTED_PROVIDERS))
            raise ValueError(
                f"unsupported memory LLM provider {provider!r}; expected one of: {supported}"
            )
        if not model_name:
            raise ValueError("memory LLM model name must not be empty")
        if (
            not isinstance(self.timeout_seconds, int | float)
            or isinstance(self.timeout_seconds, bool)
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("memory LLM timeout must be a positive finite number")
        if (
            not isinstance(self.temperature, int | float)
            or isinstance(self.temperature, bool)
            or not math.isfinite(self.temperature)
            or not 0 <= self.temperature <= 2
        ):
            raise ValueError("memory LLM temperature must be between 0 and 2")
        if (
            not isinstance(self.context_window_tokens, int)
            or isinstance(self.context_window_tokens, bool)
            or self.context_window_tokens <= 0
        ):
            raise ValueError("memory LLM context window must be a positive integer")
        if (
            not isinstance(self.max_tokens, int)
            or isinstance(self.max_tokens, bool)
            or self.max_tokens <= 0
        ):
            raise ValueError("memory LLM max tokens must be a positive integer")
        if self.extra_body is not None:
            object.__setattr__(self, "extra_body", _validate_extra_body(self.extra_body))

    @classmethod
    def from_env(cls) -> MemoryLLMConfig:
        """Build memory LLM config from `UAM_MEMORY_LLM_*` env vars."""
        provider = os.getenv("UAM_MEMORY_LLM_PROVIDER", "openai-compatible").strip().lower()
        api_key = read_secret_env("UAM_MEMORY_LLM_API_KEY")
        if not api_key and provider == "openai":
            api_key = read_secret_env("OPENAI_API_KEY")
        return cls(
            provider=provider,
            model_name=os.getenv(
                "UAM_MEMORY_LLM_MODEL",
                _default_model(provider),
            ).strip(),
            base_url=os.getenv(
                "UAM_MEMORY_LLM_BASE_URL",
                _default_base_url(provider),
            ),
            api_key=api_key,
            timeout_seconds=float(os.getenv("UAM_MEMORY_LLM_TIMEOUT_SECONDS", "60")),
            temperature=float(os.getenv("UAM_MEMORY_LLM_TEMPERATURE", "0.1")),
            context_window_tokens=int(os.getenv("UAM_MEMORY_LLM_CONTEXT_TOKENS", "8192")),
            max_tokens=int(os.getenv("UAM_MEMORY_LLM_MAX_TOKENS", "1200")),
            extra_body=_parse_extra_body(os.getenv("UAM_MEMORY_LLM_EXTRA_BODY_JSON", "")),
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
            "extra_body_configured": bool(self.extra_body),
            "api_key_configured": bool(self.api_key),
        }


class MemoryLLMError(RuntimeError):
    """Raised when the memory LLM endpoint fails or returns invalid data."""


class MemoryLLMClient:
    """Small OpenAI-compatible chat client for memory reasoning workers.

    The client is intentionally dependency-light: Docker deployments can point it
    at llama.cpp, vLLM, LiteLLM, or any other server exposing the
    OpenAI-compatible `/v1/chat/completions` contract.
    """

    def __init__(self, config: MemoryLLMConfig | None = None) -> None:
        self.config = config or MemoryLLMConfig.from_env()
        if self.config.provider == "openai" and not (self.config.api_key or "").strip():
            raise ValueError(
                "OpenAI memory LLM provider requires UAM_MEMORY_LLM_API_KEY or OPENAI_API_KEY"
            )

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict[str, str] | None = None,
    ) -> str:
        """Return assistant text for OpenAI-compatible chat messages."""
        _validate_messages(messages)
        effective_temperature = self.config.temperature if temperature is None else temperature
        effective_max_tokens = self.config.max_tokens if max_tokens is None else max_tokens
        if (
            not isinstance(effective_temperature, int | float)
            or isinstance(effective_temperature, bool)
            or not math.isfinite(effective_temperature)
            or not 0 <= effective_temperature <= 2
        ):
            raise ValueError("memory LLM request temperature must be between 0 and 2")
        if (
            not isinstance(effective_max_tokens, int)
            or isinstance(effective_max_tokens, bool)
            or effective_max_tokens <= 0
        ):
            raise ValueError("memory LLM request max tokens must be a positive integer")
        payload: dict[str, Any] = {
            "model": self.config.model_name,
            "messages": messages,
            "temperature": effective_temperature,
            "max_tokens": effective_max_tokens,
        }
        if response_format is not None:
            payload["response_format"] = response_format
        if self.config.extra_body:
            payload.update(_validate_extra_body(self.config.extra_body) or {})

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
        url = _openai_v1_endpoint(self.config.base_url, path)
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
            raise MemoryLLMError(f"Memory LLM HTTP {exc.code}: {details[:500]}") from exc
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


def _parse_extra_body(raw: str) -> dict[str, Any] | None:
    """Parse optional provider-specific OpenAI-compatible request fields."""
    if not raw.strip():
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("UAM_MEMORY_LLM_EXTRA_BODY_JSON must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("UAM_MEMORY_LLM_EXTRA_BODY_JSON must be a JSON object")
    return _validate_extra_body(payload)


def _validate_extra_body(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Reject routing, authentication, and client-owned request fields."""
    if not all(isinstance(key, str) for key in payload):
        raise ValueError("memory LLM extra body keys must be strings")
    reserved = sorted(key for key in payload if key.casefold() in _RESERVED_EXTRA_BODY_FIELDS)
    if reserved:
        raise ValueError(
            "memory LLM extra body cannot override standard or security-sensitive "
            "fields: " + ", ".join(reserved)
        )
    try:
        json.dumps(payload)
    except (TypeError, ValueError) as exc:
        raise ValueError("memory LLM extra body must contain JSON values") from exc
    return dict(payload) or None


def _validate_messages(messages: list[dict[str, str]]) -> None:
    """Validate the text-only chat contract used by memory workers."""
    if not messages:
        raise ValueError("memory LLM messages must not be empty")
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise ValueError(f"memory LLM message {index} must be an object")
        role = message.get("role")
        content = message.get("content")
        if not isinstance(role, str) or not role.strip():
            raise ValueError(f"memory LLM message {index} has an invalid role")
        if not isinstance(content, str):
            raise ValueError(f"memory LLM message {index} content must be text")


def _default_model(provider: str) -> str:
    return "gpt-4.1-mini" if provider == "openai" else "memory-model"


def _default_base_url(provider: str) -> str:
    return _OPENAI_API_BASE_URL if provider == "openai" else _COMPATIBLE_API_BASE_URL


def _validate_openai_base_url(base_url: str) -> str:
    """Validate a gateway root or OpenAI-style `/v1` base URL."""
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("memory LLM base URL must not be empty")
    value = base_url.strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
        raise ValueError("memory LLM base URL must be an absolute HTTP(S) URL")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("memory LLM base URL contains an invalid port") from exc
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("memory LLM base URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("memory LLM base URL must not contain a query or fragment")
    if parsed.path.rstrip("/").endswith("/chat/completions"):
        raise ValueError(
            "memory LLM base URL must be a gateway root or end in /v1, not /chat/completions"
        )
    return value


def _openai_v1_endpoint(base_url: str, endpoint: str) -> str:
    """Join an OpenAI-compatible base URL without duplicating `/v1`."""
    parsed = urlsplit(_validate_openai_base_url(base_url))
    base_path = parsed.path.rstrip("/")
    if not base_path.endswith("/v1"):
        base_path = f"{base_path}/v1"
    endpoint_path = endpoint.strip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, f"{base_path}/{endpoint_path}", "", ""))
