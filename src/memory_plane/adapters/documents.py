"""Deterministic Markdown and PDF extraction adapters."""

from __future__ import annotations

import io
import re
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from typing import Any
from uuid import UUID

from memory_plane.contracts.dto import IngestDocumentCommand, IngestResult
from memory_plane.services.ingestion import IngestionService

_FRONT_MATTER = re.compile(r"\A---\s*\n.*?\n---\s*(?:\n|$)", re.DOTALL)
_COMMENTS = re.compile(r"<!--.*?-->", re.DOTALL)
_LINK = re.compile(r"!\[([^\]]*)\]\([^)]*\)|\[([^\]]+)\]\([^)]*\)")
_MARKERS = re.compile(r"(?m)^\s{0,3}(?:#{1,6}\s+|>\s*|[-*+]\s+|\d+[.)]\s+)")


@dataclass(frozen=True, slots=True)
class BinaryDocumentCommand:
    """Binary source plus standalone ingestion scope."""

    tenant_id: UUID
    workspace_id: UUID
    data: bytes
    origin_uri: str
    agent_id: UUID | None = None
    thread_id: UUID | None = None
    labels: tuple[str, ...] = ()
    chunk_size_chars: int = 2400
    chunk_overlap_chars: int = 240

    def __post_init__(self) -> None:
        if not self.data:
            raise ValueError("document data must not be empty")
        if not self.origin_uri.strip():
            raise ValueError("origin_uri must not be empty")


class MarkdownParser:
    """Convert Markdown to stable readable text without executing content."""

    version = "markdown-parser-v1"

    def parse(self, data: bytes) -> str:
        text = data.decode("utf-8-sig")
        text = _FRONT_MATTER.sub("", text)
        text = _COMMENTS.sub("", text)
        text = _LINK.sub(lambda match: match.group(1) or match.group(2) or "", text)
        text = _MARKERS.sub("", text)
        text = text.replace("```", "")
        text = re.sub(r"[*_~]{1,3}", "", text)
        return "\n".join(line.rstrip() for line in text.strip().splitlines())


class PdfParser:
    """Extract normalized page text through optional pypdf."""

    version = "pypdf-parser-v1"

    def __init__(self, reader_factory: Callable[[io.BytesIO], Any] | None = None) -> None:
        self._reader_factory = reader_factory

    def parse_pages(self, data: bytes) -> tuple[str, ...]:
        factory = self._reader_factory
        if factory is None:
            try:
                from pypdf import PdfReader
            except ImportError as error:
                raise RuntimeError(
                    'PDF support is not installed; use ".[documents]"'
                ) from error
            factory = PdfReader
        try:
            reader = factory(io.BytesIO(data))
            pages = tuple(self._normalize(page.extract_text() or "") for page in reader.pages)
        except Exception as error:
            raise ValueError("invalid or unreadable PDF") from error
        if not any(pages):
            raise ValueError("PDF contains no extractable text")
        return pages

    @staticmethod
    def _normalize(text: str) -> str:
        return "\n".join(line.rstrip() for line in text.strip().splitlines())


class DocumentIngestor:
    """Parse binary documents and delegate stable chunks to canonical ingestion."""

    def __init__(
        self,
        ingestion: IngestionService,
        *,
        markdown: MarkdownParser | None = None,
        pdf: PdfParser | None = None,
    ) -> None:
        self._ingestion = ingestion
        self._markdown = markdown or MarkdownParser()
        self._pdf = pdf or PdfParser()

    def ingest_markdown(self, command: BinaryDocumentCommand) -> IngestResult:
        digest = sha256(command.data).hexdigest()
        text = self._markdown.parse(command.data)
        return self._ingest_text(
            command,
            text=text,
            origin_uri=command.origin_uri,
            checksum=digest,
            source_kind="markdown",
            extraction_version=self._markdown.version,
        )

    def ingest_pdf(self, command: BinaryDocumentCommand) -> IngestResult:
        digest = sha256(command.data).hexdigest()
        ids: list[UUID] = []
        created = 0
        for page_number, text in enumerate(self._pdf.parse_pages(command.data), start=1):
            if not text:
                continue
            result = self._ingest_text(
                command,
                text=text,
                origin_uri=f"{command.origin_uri}#page={page_number}",
                checksum=digest,
                source_kind="pdf",
                extraction_version=self._pdf.version,
            )
            ids.extend(result.memory_ids)
            created += result.created_count
        return IngestResult(digest, tuple(ids), created)

    def _ingest_text(
        self,
        command: BinaryDocumentCommand,
        *,
        text: str,
        origin_uri: str,
        checksum: str,
        source_kind: str,
        extraction_version: str,
    ) -> IngestResult:
        return self._ingestion.ingest_text(
            IngestDocumentCommand(
                tenant_id=command.tenant_id,
                workspace_id=command.workspace_id,
                text=text,
                origin_uri=origin_uri,
                agent_id=command.agent_id,
                thread_id=command.thread_id,
                labels=command.labels,
                chunk_size_chars=command.chunk_size_chars,
                chunk_overlap_chars=command.chunk_overlap_chars,
                document_checksum=checksum,
                source_kind=source_kind,
                extraction_version=extraction_version,
            )
        )
