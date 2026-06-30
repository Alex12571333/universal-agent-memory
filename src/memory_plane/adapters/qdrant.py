"""Qdrant dense+sparse CandidateSource implementation boundary."""

from __future__ import annotations


class QdrantCandidateSource:
    """Production retrieval adapter placeholder owned by Track G."""

    def __init__(self, url: str, collection: str = "memory_items") -> None:
        """Capture endpoint and collection; delay client creation until startup."""
        self.url = url
        self.collection = collection

    @property
    def name(self) -> str:
        """Return the source identifier expected in recall diagnostics."""
        return "qdrant_hybrid"

    def connect(self) -> None:
        """Fail clearly until Track G supplies the qdrant-client implementation."""
        raise NotImplementedError("implement against CandidateSource")
