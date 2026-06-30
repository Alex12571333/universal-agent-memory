"""Embedding client adapters."""

from __future__ import annotations

import hashlib

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
