"""Embedding client adapters."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from memory_plane.ports.embeddings import EmbeddingClient


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

    @classmethod
    def from_env(cls) -> EmbeddingProviderConfig:
        """Build provider config from Docker-friendly environment variables."""
        provider = os.getenv("UAM_EMBEDDING_PROVIDER", "fake").strip().lower()
        model = os.getenv("UAM_EMBEDDING_MODEL") or _default_model(provider)
        dimension = int(os.getenv("UAM_EMBEDDING_DIM", "1536"))
        timeout = float(os.getenv("UAM_EMBEDDING_TIMEOUT_SECONDS", "30"))
        return cls(
            provider=provider,
            model_name=model,
            dimension=dimension,
            base_url=os.getenv("UAM_EMBEDDING_BASE_URL") or _default_base_url(provider),
            api_key=os.getenv("UAM_EMBEDDING_API_KEY") or os.getenv("OPENAI_API_KEY"),
            timeout_seconds=timeout,
        )


class OpenAIEmbeddingClient(EmbeddingClient):
    """OpenAI `/v1/embeddings` client using only the Python stdlib."""

    def __init__(
        self,
        *,
        model_name: str,
        dimension: int,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: float = 30.0,
    ) -> None:
        """Initialize an OpenAI-compatible embedding client."""
        if not api_key:
            raise ValueError("OpenAI embedding provider requires UAM_EMBEDDING_API_KEY")
        self._model_name = model_name
        self._dimension = dimension
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds

    @property
    def model_name(self) -> str:
        """Return the configured embedding model."""
        return self._model_name

    @property
    def dimension(self) -> int:
        """Return the expected vector dimension."""
        return self._dimension

    def embed(self, text: str) -> list[float]:
        """Call OpenAI embeddings and return one dense vector."""
        payload: dict[str, Any] = {
            "model": self._model_name,
            "input": text,
            "dimensions": self._dimension,
        }
        data = _post_json(
            f"{self._base_url}/embeddings",
            payload,
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout_seconds=self._timeout_seconds,
        )
        try:
            return _coerce_vector(data["data"][0]["embedding"])
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("invalid OpenAI embeddings response") from exc


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
        self._base_url = base_url.rstrip("/")
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
            f"{self._base_url}/api/embeddings",
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
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
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
        headers = (
            {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        )
        data = _post_json(
            f"{self._base_url}/v1/embeddings",
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
    if cfg.dimension <= 0:
        raise ValueError("embedding dimension must be positive")
    if cfg.provider == "fake":
        return FakeEmbeddingClient(model_name=cfg.model_name, dimension=cfg.dimension)
    if cfg.provider == "openai":
        return OpenAIEmbeddingClient(
            model_name=cfg.model_name,
            dimension=cfg.dimension,
            api_key=cfg.api_key or "",
            base_url=cfg.base_url or "https://api.openai.com/v1",
            timeout_seconds=cfg.timeout_seconds,
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
        raise RuntimeError(f"embedding provider HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"embedding provider request failed: {exc.reason}") from exc
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError("embedding provider returned non-object JSON")
    return parsed


def _coerce_vector(value: Any) -> list[float]:
    """Validate and coerce JSON embedding arrays into floats."""
    if not isinstance(value, list):
        raise RuntimeError("embedding value is not a list")
    try:
        return [float(item) for item in value]
    except (TypeError, ValueError) as exc:
        raise RuntimeError("embedding vector contains non-numeric values") from exc


def _default_model(provider: str) -> str:
    if provider == "openai":
        return "text-embedding-3-small"
    if provider == "ollama":
        return "nomic-embed-text"
    if provider == "tei":
        return "tei-default"
    return "fake-embed-v1"


def _default_base_url(provider: str) -> str | None:
    if provider == "openai":
        return "https://api.openai.com/v1"
    if provider == "ollama":
        return "http://localhost:11434"
    return None
