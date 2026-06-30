"""Deterministic text ingestion before model-assisted extraction."""

from __future__ import annotations

from hashlib import sha256

from memory_plane.contracts.dto import IngestDocumentCommand, IngestResult, RetainCommand
from memory_plane.domain.models import MemoryLayer, MemoryScope, Provenance
from memory_plane.services.retention import RetentionService


class TextChunker:
    """Dependency-free paragraph-aware chunker with stable offsets."""

    def split(self, text: str, *, size: int, overlap: int) -> tuple[tuple[int, int, str], ...]:
        """Split normalized text into overlapping chunks with character offsets."""
        normalized = "\n".join(line.rstrip() for line in text.strip().splitlines())
        chunks: list[tuple[int, int, str]] = []
        start = 0
        while start < len(normalized):
            hard_end = min(len(normalized), start + size)
            end = hard_end
            if hard_end < len(normalized):
                paragraph = normalized.rfind("\n\n", start + size // 2, hard_end)
                sentence = normalized.rfind(". ", start + size // 2, hard_end)
                boundary = max(paragraph + 2 if paragraph >= 0 else -1, sentence + 2)
                if boundary > start:
                    end = boundary
            chunk = normalized[start:end].strip()
            if chunk:
                chunks.append((start, end, chunk))
            if end >= len(normalized):
                break
            start = max(start + 1, end - overlap)
        return tuple(chunks)


class IngestionService:
    """Convert source text into provenance-linked episodic memory chunks."""

    def __init__(self, retention: RetentionService, chunker: TextChunker | None = None) -> None:
        """Bind ingestion to the canonical retain path."""
        self._retention = retention
        self._chunker = chunker or TextChunker()

    def ingest_text(self, command: IngestDocumentCommand) -> IngestResult:
        """Checksum, chunk and retain a text document idempotently."""
        digest = command.document_checksum or sha256(command.text.encode("utf-8")).hexdigest()
        origin_digest = sha256(command.origin_uri.encode("utf-8")).hexdigest()[:16]
        chunks = self._chunker.split(
            command.text,
            size=command.chunk_size_chars,
            overlap=command.chunk_overlap_chars,
        )
        ids = []
        created = 0
        for index, (start, end, text) in enumerate(chunks):
            result = self._retention.retain(
                RetainCommand(
                    tenant_id=command.tenant_id,
                    workspace_id=command.workspace_id,
                    agent_id=command.agent_id,
                    thread_id=command.thread_id,
                    layer=MemoryLayer.EPISODIC,
                    scope=(
                        MemoryScope.THREAD
                        if command.thread_id is not None
                        else MemoryScope.WORKSPACE
                    ),
                    kind="document_chunk",
                    text=text,
                    labels=command.labels,
                    provenance=Provenance(
                        source_kind=command.source_kind,
                        origin_uri=command.origin_uri,
                        checksum_sha256=digest,
                        quote=text,
                        extraction_version=command.extraction_version,
                    ),
                    idempotency_key=(
                        f"doc:{digest}:origin:{origin_digest}:chunk:{index}:{start}:{end}"
                    ),
                )
            )
            ids.append(result.item.id)
            created += int(result.created)
        return IngestResult(
            document_checksum=digest,
            memory_ids=tuple(ids),
            created_count=created,
        )
