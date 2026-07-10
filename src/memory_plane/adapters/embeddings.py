"""Embedding client adapters."""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from memory_plane.config.secrets import read_secret_env
from memory_plane.ports.embeddings import EmbeddingClient

_SUPPORTED_PROVIDERS = frozenset({"fake", "openai-compatible", "openai", "ollama", "tei"})
_OPENAI_API_BASE_URL = "https://api.openai.com/v1"
_COMPATIBLE_API_BASE_URL = "http://localhost:8000/v1"


class _RejectRedirects(HTTPRedirectHandler):
    """Prevent an allowlisted model endpoint from redirecting to another origin."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_NO_REDIRECT_OPENER = build_opener(_RejectRedirects())


def urlopen(request: Request, timeout: float) -> Any:
    """Open one provider request without following HTTP redirects."""
    return _NO_REDIRECT_OPENER.open(request, timeout=timeout)


class FakeEmbeddingClient(EmbeddingClient):
    """Deterministic mock embedding generator for local testing and CI."""

    def __init__(self, model_name: str = "fake-embed-v1", dimension: int = 1536) -> None:
        """Initialize with configureable model identifier and output shape."""
        self._model_name = model_name
        self._dimension = dimension

    @property
    def model_name(self) -> str:
        """Return the stable model identifier."""
        return self._model_name

    @property
    def dimension(self) -> int:
        """Return the target vector size."""
        return self._dimension

    def embed(self, text: str) -> list[float]:
        """Produce a deterministic normalized dense vector from text hashing."""
        hasher = hashlib.md5(text.encode("utf-8"))
        digest = hasher.digest()
        # Derive a predictable pattern of floats scaled to [-1.0, 1.0]
        vector: list[float] = []
        for i in range(self._dimension):
            byte_val = digest[i % len(digest)]
            # Add some positional variation so not all repeating values are identical
            val = (byte_val + i) % 256
            vector.append(float(val - 128) / 128.0)
        return vector


@dataclass(frozen=True, slots=True)
class EmbeddingProviderConfig:
    """Runtime configuration for production embedding providers."""

    provider: str = "fake"
    model_name: str = "fake-embed-v1"
    dimension: int = 1536
    base_url: str | None = None
    api_key: str | None = None
    timeout_seconds: float = 30.0
    send_dimensions: bool | None = None

    def __post_init__(self) -> None:
        """Normalize and validate provider settings before building a client."""
        if not isinstance(self.provider, str):
            raise ValueError("embedding provider must be a string")
        if not isinstance(self.model_name, str):
            raise ValueError("embedding model name must be a string")
        provider = self.provider.strip().lower()
        model_name = self.model_name.strip()
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "model_name", model_name)

        if provider not in _SUPPORTED_PROVIDERS:
            supported = ", ".join(sorted(_SUPPORTED_PROVIDERS))
            raise ValueError(
                f"unsupported embedding provider {provider!r}; expected one of: {supported}"
            )
        if not model_name:
            raise ValueError("embedding model name must not be empty")
        if (
            not isinstance(self.dimension, int)
            or isinstance(self.dimension, bool)
            or self.dimension <= 0
        ):
            raise ValueError("embedding dimension must be a positive integer")
        if (
            not isinstance(self.timeout_seconds, int | float)
            or isinstance(self.timeout_seconds, bool)
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("embedding timeout must be a positive finite number")
        if self.send_dimensions is not None and not isinstance(
            self.send_dimensions,
            bool,
        ):
            raise ValueError("embedding send_dimensions must be a boolean")
        if provider != "fake":
            if not self.base_url:
                raise ValueError(f"{provider} embedding provider requires UAM_EMBEDDING_BASE_URL")
            object.__setattr__(
                self,
                "base_url",
                _validate_http_base_url(self.base_url, provider=provider),
            )

    @classmethod
    def from_env(cls) -> EmbeddingProviderConfig:
        """Build provider config from Docker-friendly environment variables."""
        provider = os.getenv("UAM_EMBEDDING_PROVIDER", "fake").strip().lower()
        model = os.getenv("UAM_EMBEDDING_MODEL") or _default_model(provider)
        dimension = int(os.getenv("UAM_EMBEDDING_DIM", "1536"))
        timeout = float(os.getenv("UAM_EMBEDDING_TIMEOUT_SECONDS", "30"))
        send_dimensions_value = os.getenv("UAM_EMBEDDING_SEND_DIMENSIONS", "").strip()
        api_key = read_secret_env("UAM_EMBEDDING_API_KEY")
        if not api_key and provider == "openai":
            api_key = read_secret_env("OPENAI_API_KEY")
        return cls(
            provider=provider,
            model_name=model,
            dimension=dimension,
            base_url=os.getenv("UAM_EMBEDDING_BASE_URL") or _default_base_url(provider),
            api_key=api_key,
            timeout_seconds=timeout,
            send_dimensions=_parse_optional_bool(
                "UAM_EMBEDDING_SEND_DIMENSIONS",
                send_dimensions_value,
            ),
        )


class OpenAICompatibleEmbeddingClient(EmbeddingClient):
    """Generic OpenAI-compatible `/v1/embeddings` client."""

    def __init__(
        self,
        *,
        model_name: str,
        dimension: int,
        api_key: str | None = None,
        base_url: str = _COMPATIBLE_API_BASE_URL,
        timeout_seconds: float = 30.0,
        send_dimensions: bool = False,
    ) -> None:
        """Initialize a provider-neutral embeddings client."""
        self._model_name = model_name
        self._dimension = dimension
        self._base_url = _validate_http_base_url(
            base_url,
            provider="openai-compatible",
        )
        self._api_key = api_key if api_key and api_key.strip() else None
        self._timeout_seconds = timeout_seconds
        self._send_dimensions = send_dimensions

    @property
    def model_name(self) -> str:
        """Return the configured embedding model."""
        return self._model_name

    @property
    def dimension(self) -> int:
        """Return the expected vector dimension."""
        return self._dimension

    def embed(self, text: str) -> list[float]:
        """Call the compatible embeddings endpoint and return one dense vector."""
        payload: dict[str, Any] = {
            "model": self._model_name,
            "input": text,
        }
        if self._send_dimensions:
            payload["dimensions"] = self._dimension
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        data = _post_json(
            _openai_v1_endpoint(self._base_url, "embeddings"),
            payload,
            headers=headers,
            timeout_seconds=self._timeout_seconds,
        )
        try:
            return _coerce_vector(data["data"][0]["embedding"])
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("invalid OpenAI-compatible embeddings response") from exc


class OpenAIEmbeddingClient(OpenAICompatibleEmbeddingClient):
    """OpenAI `/v1/embeddings` client using only the Python stdlib."""

    def __init__(
        self,
        *,
        model_name: str,
        dimension: int,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: float = 30.0,
        send_dimensions: bool = True,
    ) -> None:
        """Initialize the OpenAI-hosted embedding profile."""
        if not (api_key or "").strip():
            raise ValueError(
                "OpenAI embedding provider requires UAM_EMBEDDING_API_KEY or OPENAI_API_KEY"
            )
        super().__init__(
            model_name=model_name,
            dimension=dimension,
            api_key=api_key,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            send_dimensions=send_dimensions,
        )


class OllamaEmbeddingClient(EmbeddingClient):
    """Ollama embedding client for local self-hosted models."""

    def __init__(
        self,
        *,
        model_name: str,
        dimension: int,
        base_url: str = "http://localhost:11434",
        timeout_seconds: float = 30.0,
    ) -> None:
        """Initialize an Ollama embedding client."""
        self._model_name = model_name
        self._dimension = dimension
        self._base_url = _validate_http_base_url(base_url, provider="ollama")
        self._timeout_seconds = timeout_seconds

    @property
    def model_name(self) -> str:
        """Return the configured Ollama model."""
        return self._model_name

    @property
    def dimension(self) -> int:
        """Return the expected vector dimension."""
        return self._dimension

    def embed(self, text: str) -> list[float]:
        """Call Ollama embeddings and return one dense vector."""
        data = _post_json(
            _join_endpoint(self._base_url, "api/embeddings"),
            {"model": self._model_name, "prompt": text},
            timeout_seconds=self._timeout_seconds,
        )
        try:
            return _coerce_vector(data["embedding"])
        except (KeyError, TypeError) as exc:
            raise RuntimeError("invalid Ollama embeddings response") from exc


class TEIEmbeddingClient(EmbeddingClient):
    """TEI/vLLM-style HTTP embedding client."""

    def __init__(
        self,
        *,
        model_name: str,
        dimension: int,
        base_url: str,
        api_key: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        """Initialize a generic OpenAI-compatible embeddings endpoint."""
        if not base_url:
            raise ValueError("TEI embedding provider requires UAM_EMBEDDING_BASE_URL")
        self._model_name = model_name
        self._dimension = dimension
        self._base_url = _validate_http_base_url(base_url, provider="tei")
        self._api_key = api_key if api_key and api_key.strip() else None
        self._timeout_seconds = timeout_seconds

    @property
    def model_name(self) -> str:
        """Return the configured endpoint model name."""
        return self._model_name

    @property
    def dimension(self) -> int:
        """Return the expected vector dimension."""
        return self._dimension

    def embed(self, text: str) -> list[float]:
        """Call an OpenAI-compatible `/v1/embeddings` endpoint."""
        return self.embed_document(text)

    def embed_query(self, text: str) -> list[float]:
        """Embed a retrieval query when the endpoint supports input typing."""
        return self._embed(text, input_type="query")

    def embed_document(self, text: str) -> list[float]:
        """Embed a stored memory/document when the endpoint supports input typing."""
        return self._embed(text, input_type="document")

    def _embed(self, text: str, *, input_type: str) -> list[float]:
        """Call an OpenAI-compatible `/v1/embeddings` endpoint."""
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        data = _post_json(
            _openai_v1_endpoint(self._base_url, "embeddings"),
            {"model": self._model_name, "input": text, "input_type": input_type},
            headers=headers,
            timeout_seconds=self._timeout_seconds,
        )
        try:
            return _coerce_vector(data["data"][0]["embedding"])
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("invalid TEI embeddings response") from exc


def build_embedding_client(config: EmbeddingProviderConfig | None = None) -> EmbeddingClient:
    """Create an embedding client selected by `UAM_EMBEDDING_PROVIDER`."""
    cfg = config or EmbeddingProviderConfig.from_env()
    if cfg.provider == "fake":
        return FakeEmbeddingClient(model_name=cfg.model_name, dimension=cfg.dimension)
    if cfg.provider == "openai":
        return OpenAIEmbeddingClient(
            model_name=cfg.model_name,
            dimension=cfg.dimension,
            api_key=cfg.api_key or "",
            base_url=cfg.base_url or "https://api.openai.com/v1",
            timeout_seconds=cfg.timeout_seconds,
            send_dimensions=True if cfg.send_dimensions is None else cfg.send_dimensions,
        )
    if cfg.provider == "openai-compatible":
        return OpenAICompatibleEmbeddingClient(
            model_name=cfg.model_name,
            dimension=cfg.dimension,
            api_key=cfg.api_key,
            base_url=cfg.base_url or _COMPATIBLE_API_BASE_URL,
            timeout_seconds=cfg.timeout_seconds,
            send_dimensions=(False if cfg.send_dimensions is None else cfg.send_dimensions),
        )
    if cfg.provider == "ollama":
        return OllamaEmbeddingClient(
            model_name=cfg.model_name,
            dimension=cfg.dimension,
            base_url=cfg.base_url or "http://localhost:11434",
            timeout_seconds=cfg.timeout_seconds,
        )
    if cfg.provider == "tei":
        return TEIEmbeddingClient(
            model_name=cfg.model_name,
            dimension=cfg.dimension,
            base_url=cfg.base_url or "",
            api_key=cfg.api_key,
            timeout_seconds=cfg.timeout_seconds,
        )
    raise ValueError(f"unsupported embedding provider: {cfg.provider}")


def _post_json(
    url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    timeout_seconds: float,
) -> dict[str, Any]:
    """POST JSON and decode one JSON object."""
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            **(headers or {}),
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"embedding provider HTTP {exc.code}: {detail[:500]}") from exc
    except URLError as exc:
        raise RuntimeError(f"embedding provider request failed: {exc.reason}") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("embedding provider returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("embedding provider returned non-object JSON")
    return parsed


def _coerce_vector(value: Any) -> list[float]:
    """Validate and coerce JSON embedding arrays into floats."""
    if not isinstance(value, list):
        raise RuntimeError("embedding value is not a list")
    try:
        vector = [float(item) for item in value]
    except (TypeError, ValueError) as exc:
        raise RuntimeError("embedding vector contains non-numeric values") from exc
    if not vector:
        raise RuntimeError("embedding vector is empty")
    if not all(math.isfinite(item) for item in vector):
        raise RuntimeError("embedding vector contains non-finite values")
    return vector


def _default_model(provider: str) -> str:
    if provider == "openai":
        return "text-embedding-3-small"
    if provider == "openai-compatible":
        return "embedding-model"
    if provider == "ollama":
        return "nomic-embed-text"
    if provider == "tei":
        return "tei-default"
    return "fake-embed-v1"


def _default_base_url(provider: str) -> str | None:
    if provider == "openai":
        return _OPENAI_API_BASE_URL
    if provider == "openai-compatible":
        return _COMPATIBLE_API_BASE_URL
    if provider == "ollama":
        return "http://localhost:11434"
    return None


def _parse_optional_bool(name: str, value: str) -> bool | None:
    """Parse an optional boolean without silently accepting typos."""
    if not value:
        return None
    normalized = value.casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be one of: true, false, 1, 0, yes, no, on, off")


def _validate_http_base_url(base_url: str, *, provider: str) -> str:
    """Validate a provider base URL without accepting embedded credentials."""
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError(f"{provider} embedding base URL must not be empty")
    value = base_url.strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
        raise ValueError(f"{provider} embedding base URL must be an absolute HTTP(S) URL")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(f"{provider} embedding base URL contains an invalid port") from exc
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{provider} embedding base URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{provider} embedding base URL must not contain a query or fragment")
    if parsed.path.rstrip("/").endswith(("/embeddings", "/api/embeddings")):
        raise ValueError(
            f"{provider} embedding base URL must be a server root or end in /v1, "
            "not an embeddings endpoint"
        )
    return value


def _openai_v1_endpoint(base_url: str, endpoint: str) -> str:
    """Join a compatible base URL with `/v1` exactly once."""
    parsed = urlsplit(base_url)
    base_path = parsed.path.rstrip("/")
    if not base_path.endswith("/v1"):
        base_path = f"{base_path}/v1"
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            f"{base_path}/{endpoint.strip('/')}",
            "",
            "",
        )
    )


def _join_endpoint(base_url: str, endpoint: str) -> str:
    """Join a validated provider base URL and a relative endpoint."""
    parsed = urlsplit(base_url)
    path = f"{parsed.path.rstrip('/')}/{endpoint.strip('/')}"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
