"""Interface protocol for embedding models."""

from __future__ import annotations

from typing import Protocol


class EmbeddingClient(Protocol):
    """Protocol defining the stable API for generating document/query embeddings."""

    @property
    def model_name(self) -> str:
        """Stable identifier of the model including its version (e.g. 'fake-embed-v1')."""
        ...

    @property
    def dimension(self) -> int:
        """Fixed length of dense vectors produced by this model."""
        ...

    def embed(self, text: str) -> list[float]:
        """Produce a dense representation of the raw string."""
        ...
