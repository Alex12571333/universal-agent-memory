from __future__ import annotations

from hashlib import sha256
from uuid import uuid4

from memory_plane.adapters.documents import (
    BinaryDocumentCommand,
    DocumentIngestor,
    MarkdownParser,
    PdfParser,
)
from memory_plane.adapters.in_memory import InMemoryMemoryStore
from memory_plane.bootstrap import build_in_memory_container


class FakePage:
    def __init__(self, text: str) -> None:
        self._text = text

    def extract_text(self) -> str:
        return self._text


class FakeReader:
    def __init__(self, stream) -> None:
        assert stream.read() == b"%PDF-fixture"
        self.pages = [FakePage("Page one fact."), FakePage("Page two fact.")]


def command(data: bytes, origin_uri: str) -> BinaryDocumentCommand:
    return BinaryDocumentCommand(
        tenant_id=uuid4(),
        workspace_id=uuid4(),
        data=data,
        origin_uri=origin_uri,
        chunk_size_chars=256,
        chunk_overlap_chars=20,
    )


def test_markdown_parser_removes_markup_without_losing_content() -> None:
    parsed = MarkdownParser().parse(
        b"""---
title: Hidden metadata
---
# Project Plan

- Use **PostgreSQL**
- Read [architecture](https://example.invalid)
<!-- private comment -->
"""
    )

    assert "Hidden metadata" not in parsed
    assert "Project Plan" in parsed
    assert "Use PostgreSQL" in parsed
    assert "Read architecture" in parsed
    assert "private comment" not in parsed


def test_markdown_ingestion_preserves_binary_checksum_and_is_idempotent() -> None:
    container = build_in_memory_container()
    ingestor = DocumentIngestor(container.ingestion)
    source = b"# Durable memory\n\nRemember this decision."
    request = command(source, "file:///decision.md")

    first = ingestor.ingest_markdown(request)
    second = ingestor.ingest_markdown(request)

    assert first.document_checksum == sha256(source).hexdigest()
    assert first.created_count == 1
    assert second.created_count == 0
    assert first.memory_ids == second.memory_ids
    assert isinstance(container.store, InMemoryMemoryStore)
    item = container.store.get(request.tenant_id, first.memory_ids[0])
    assert item is not None
    assert item.provenance.source_kind == "markdown"
    assert item.provenance.origin_uri == "file:///decision.md"
    assert item.provenance.checksum_sha256 == sha256(source).hexdigest()


def test_pdf_pages_have_distinct_page_provenance_and_stable_ids() -> None:
    container = build_in_memory_container()
    ingestor = DocumentIngestor(
        container.ingestion,
        pdf=PdfParser(reader_factory=FakeReader),
    )
    request = command(b"%PDF-fixture", "file:///notes.pdf")

    first = ingestor.ingest_pdf(request)
    second = ingestor.ingest_pdf(request)

    assert first.created_count == 2
    assert second.created_count == 0
    assert len(set(first.memory_ids)) == 2
    assert isinstance(container.store, InMemoryMemoryStore)
    items = container.store.list_for_workspace(request.tenant_id, request.workspace_id)
    assert [item.provenance.origin_uri for item in items] == [
        "file:///notes.pdf#page=1",
        "file:///notes.pdf#page=2",
    ]
    assert all(item.provenance.source_kind == "pdf" for item in items)
