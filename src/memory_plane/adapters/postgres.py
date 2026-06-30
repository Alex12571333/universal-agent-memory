"""PostgreSQL system-of-record adapter with transactional outbox semantics."""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

from memory_plane.contracts.dto import Candidate, RecallQuery
from memory_plane.contracts.events import IntegrationEvent
from memory_plane.domain.models import (
    MemoryItem,
    MemoryLayer,
    MemoryScope,
    Observation,
    Provenance,
)

_ITEM_COLUMNS = """
    m.id, m.tenant_id, m.workspace_id, m.agent_id, m.thread_id,
    m.layer, m.scope, m.kind, m.text, m.labels, m.metadata,
    m.importance, m.salience, m.confidence, m.observed_at,
    m.valid_from, m.valid_to, m.created_at, m.revision, m.supersedes_id,
    p.source_kind, p.origin_uri, p.object_key, p.checksum_sha256,
    p.quote_text, p.extraction_version
"""
_WORD = re.compile(r"\w+", re.UNICODE)


class PostgresMemoryLedger:
    """Psycopg implementation of the retention store and canonical ledger."""

    def __init__(self, dsn: str) -> None:
        """Capture configuration without opening a connection at import time."""
        if not dsn.strip():
            raise ValueError("PostgreSQL DSN must not be empty")
        self.dsn = dsn

    def connect(self) -> None:
        """Check that PostgreSQL is reachable and the schema is installed."""
        with self._connection() as connection:
            row = connection.execute("select to_regclass('memory_items') as table_name").fetchone()
            if row is None or row["table_name"] is None:
                raise RuntimeError("memory schema is not installed")

    @property
    def name(self) -> str:
        """Return the stable retrieval diagnostic name."""
        return "postgres_lexical"

    def ensure_standalone_scope(
        self,
        server_id: UUID,
        project_id: UUID,
        *,
        server_name: str = "standalone",
        project_name: str = "default",
    ) -> None:
        """Create the fixed standalone server/project namespace idempotently."""
        with self._connection() as connection:
            connection.execute(
                """
                insert into tenants (id, slug) values (%s, %s)
                on conflict (id) do nothing
                """,
                (server_id, server_name),
            )
            self._set_tenant(connection, server_id)
            connection.execute(
                """
                insert into workspaces (id, tenant_id, name) values (%s, %s, %s)
                on conflict (id) do nothing
                """,
                (project_id, server_id, project_name),
            )

    def retain(
        self,
        item: MemoryItem,
        event: IntegrationEvent,
        idempotency_key: str | None = None,
    ) -> tuple[MemoryItem, bool]:
        """Atomically append memory, provenance, idempotency key and outbox event."""
        self._validate_event(item, event)
        with self._connection() as connection:
            self._set_tenant(connection, item.tenant_id)
            if idempotency_key:
                self._lock_idempotency_key(connection, item.tenant_id, idempotency_key)
                existing = self._get_by_idempotency_key(
                    connection, item.tenant_id, idempotency_key
                )
                if existing is not None:
                    return existing, False

            self._insert_item(connection, item)
            if idempotency_key:
                connection.execute(
                    """
                    insert into idempotency_keys (tenant_id, key, memory_item_id)
                    values (%s, %s, %s)
                    """,
                    (item.tenant_id, idempotency_key, item.id),
                )
            self._insert_event(connection, event)
            return item, True

    def append(
        self, item: MemoryItem, idempotency_key: str | None = None
    ) -> tuple[MemoryItem, bool]:
        """Append without an event for maintenance and import workflows."""
        with self._connection() as connection:
            self._set_tenant(connection, item.tenant_id)
            if idempotency_key:
                self._lock_idempotency_key(connection, item.tenant_id, idempotency_key)
                existing = self._get_by_idempotency_key(
                    connection, item.tenant_id, idempotency_key
                )
                if existing is not None:
                    return existing, False
            self._insert_item(connection, item)
            if idempotency_key:
                connection.execute(
                    """
                    insert into idempotency_keys (tenant_id, key, memory_item_id)
                    values (%s, %s, %s)
                    """,
                    (item.tenant_id, idempotency_key, item.id),
                )
            return item, True

    def get(self, tenant_id: UUID, item_id: UUID) -> MemoryItem | None:
        """Load one item under an explicit PostgreSQL RLS tenant context."""
        with self._connection() as connection:
            self._set_tenant(connection, tenant_id)
            row = connection.execute(
                f"""
                select {_ITEM_COLUMNS}
                from memory_items m
                join memory_provenance p on p.memory_item_id = m.id
                where m.id = %s and m.deleted_at is null
                """,
                (item_id,),
            ).fetchone()
            return None if row is None else self._to_item(row)

    def list_for_workspace(
        self,
        tenant_id: UUID,
        workspace_id: UUID,
        *,
        layers: tuple[MemoryLayer, ...] = (),
    ) -> tuple[MemoryItem, ...]:
        """List canonical workspace memory in deterministic creation order."""
        with self._connection() as connection:
            self._set_tenant(connection, tenant_id)
            params: list[Any] = [workspace_id]
            layer_filter = ""
            if layers:
                layer_filter = "and m.layer = any(%s)"
                params.append([layer.value for layer in layers])
            rows = connection.execute(
                f"""
                select {_ITEM_COLUMNS}
                from memory_items m
                join memory_provenance p on p.memory_item_id = m.id
                where m.workspace_id = %s
                  and m.deleted_at is null
                  {layer_filter}
                order by m.created_at, m.id
                """,
                params,
            ).fetchall()
            return tuple(self._to_item(row) for row in rows)

    def search(self, query: RecallQuery) -> tuple[Candidate, ...]:
        """Provide a durable lexical fallback until the optional vector index is enabled."""
        query_terms = self._terms(query.text)
        candidates: list[Candidate] = []
        for item in self.list_for_workspace(
            query.tenant_id, query.workspace_id, layers=query.layers
        ):
            if item.scope == MemoryScope.THREAD and item.thread_id != query.thread_id:
                continue
            if query.labels and not set(query.labels).issubset(item.labels):
                continue
            if query.valid_at and not item.is_valid_at(query.valid_at):
                continue
            overlap = len(query_terms & self._terms(item.text))
            lexical = overlap / max(1, len(query_terms))
            if lexical > 0 or item.layer in (MemoryLayer.CORE, MemoryLayer.WORKING):
                candidates.append(
                    Candidate(
                        item=item,
                        source=self.name,
                        lexical=lexical,
                        entity=lexical,
                        trust=item.confidence,
                    )
                )
        candidates.sort(key=lambda row: (row.lexical, row.item.created_at), reverse=True)
        return tuple(candidates[: query.top_k * 3])

    def save(self, observation: Observation) -> Observation:
        """Store an evidence-grounded observation and its immutable links."""
        with self._connection() as connection:
            self._set_tenant(connection, observation.tenant_id)
            connection.execute(
                """
                insert into observations (
                  id, tenant_id, workspace_id, summary, confidence, stale, created_at
                ) values (%s, %s, %s, %s, %s, %s, %s)
                on conflict (id) do nothing
                """,
                (
                    observation.id,
                    observation.tenant_id,
                    observation.workspace_id,
                    observation.summary,
                    observation.confidence,
                    observation.stale,
                    observation.created_at,
                ),
            )
            for evidence_id in observation.evidence_ids:
                connection.execute(
                    """
                    insert into observation_evidence (
                      tenant_id, observation_id, memory_item_id
                    ) values (%s, %s, %s)
                    on conflict do nothing
                    """,
                    (observation.tenant_id, observation.id, evidence_id),
                )
        return observation

    def list_observations(
        self, tenant_id: UUID, workspace_id: UUID
    ) -> tuple[Observation, ...]:
        """List derived observations with their evidence under RLS."""
        with self._connection() as connection:
            self._set_tenant(connection, tenant_id)
            rows = connection.execute(
                """
                select
                  o.id, o.tenant_id, o.workspace_id, o.summary, o.confidence,
                  o.stale, o.created_at,
                  array_agg(e.memory_item_id order by e.memory_item_id) as evidence_ids
                from observations o
                join observation_evidence e on e.observation_id = o.id
                where o.workspace_id = %s
                group by o.id
                order by o.created_at, o.id
                """,
                (workspace_id,),
            ).fetchall()
        return tuple(
            Observation(
                id=row["id"],
                tenant_id=row["tenant_id"],
                workspace_id=row["workspace_id"],
                summary=row["summary"],
                evidence_ids=tuple(row["evidence_ids"]),
                confidence=row["confidence"],
                stale=row["stale"],
                created_at=row["created_at"],
            )
            for row in rows
        )

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        """Open a short-lived transaction with dictionary-shaped rows."""
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as error:
            raise RuntimeError(
                'PostgreSQL support is not installed; run pip install -e ".[postgres]"'
            ) from error

        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            yield connection

    @staticmethod
    def _set_tenant(connection: Any, tenant_id: UUID) -> None:
        """Bind RLS policies to this transaction without string interpolation."""
        connection.execute(
            "select set_config('app.tenant_id', %s, true)",
            (str(tenant_id),),
        )

    @staticmethod
    def _lock_idempotency_key(connection: Any, tenant_id: UUID, key: str) -> None:
        """Serialize concurrent retries for one tenant/key pair."""
        connection.execute(
            "select pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"{tenant_id}:{key}",),
        )

    def _get_by_idempotency_key(
        self, connection: Any, tenant_id: UUID, key: str
    ) -> MemoryItem | None:
        row = connection.execute(
            f"""
            select {_ITEM_COLUMNS}
            from idempotency_keys i
            join memory_items m on m.id = i.memory_item_id
            join memory_provenance p on p.memory_item_id = m.id
            where i.tenant_id = %s and i.key = %s
            """,
            (tenant_id, key),
        ).fetchone()
        return None if row is None else self._to_item(row)

    @staticmethod
    def _insert_item(connection: Any, item: MemoryItem) -> None:
        from psycopg.types.json import Jsonb

        connection.execute(
            """
            insert into memory_items (
              id, tenant_id, workspace_id, agent_id, thread_id, layer, scope,
              kind, text, labels, metadata, importance, salience, confidence,
              observed_at, valid_from, valid_to, revision, supersedes_id, created_at
            ) values (
              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
              %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                item.id,
                item.tenant_id,
                item.workspace_id,
                item.agent_id,
                item.thread_id,
                item.layer.value,
                item.scope.value,
                item.kind,
                item.text,
                list(item.labels),
                Jsonb(item.metadata),
                item.importance,
                item.salience,
                item.confidence,
                item.observed_at,
                item.valid_from,
                item.valid_to,
                item.revision,
                item.supersedes_id,
                item.created_at,
            ),
        )
        provenance = item.provenance
        connection.execute(
            """
            insert into memory_provenance (
              tenant_id, workspace_id, memory_item_id, source_kind, origin_uri,
              object_key, checksum_sha256, quote_text, extraction_version
            ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                item.tenant_id,
                item.workspace_id,
                item.id,
                provenance.source_kind,
                provenance.origin_uri,
                provenance.object_key,
                provenance.checksum_sha256,
                provenance.quote,
                provenance.extraction_version,
            ),
        )

    @staticmethod
    def _insert_event(connection: Any, event: IntegrationEvent) -> None:
        from psycopg.types.json import Jsonb

        connection.execute(
            """
            insert into outbox_events (
              id, tenant_id, workspace_id, name, payload,
              correlation_id, occurred_at
            ) values (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                event.id,
                event.tenant_id,
                event.workspace_id,
                event.name,
                Jsonb(event.payload),
                event.correlation_id,
                event.occurred_at,
            ),
        )

    @staticmethod
    def _validate_event(item: MemoryItem, event: IntegrationEvent) -> None:
        if (item.tenant_id, item.workspace_id) != (
            event.tenant_id,
            event.workspace_id,
        ):
            raise ValueError("memory item and event must share tenant/workspace")
        if event.correlation_id != item.id:
            raise ValueError("retention event must correlate to the memory item")

    @staticmethod
    def _to_item(row: dict[str, Any]) -> MemoryItem:
        return MemoryItem(
            id=row["id"],
            tenant_id=row["tenant_id"],
            workspace_id=row["workspace_id"],
            agent_id=row["agent_id"],
            thread_id=row["thread_id"],
            layer=MemoryLayer(row["layer"]),
            scope=MemoryScope(row["scope"]),
            kind=row["kind"],
            text=row["text"],
            labels=tuple(row["labels"]),
            metadata=row["metadata"],
            importance=row["importance"],
            salience=row["salience"],
            confidence=row["confidence"],
            observed_at=row["observed_at"],
            valid_from=row["valid_from"],
            valid_to=row["valid_to"],
            created_at=row["created_at"],
            revision=row["revision"],
            supersedes_id=row["supersedes_id"],
            provenance=Provenance(
                source_kind=row["source_kind"],
                origin_uri=row["origin_uri"],
                object_key=row["object_key"],
                checksum_sha256=row["checksum_sha256"],
                quote=row["quote_text"],
                extraction_version=row["extraction_version"],
            ),
        )

    @staticmethod
    def _terms(text: str) -> set[str]:
        return {match.group(0).casefold() for match in _WORD.finditer(text)}


class PostgresObservationRepository:
    """Observation-port view over the shared PostgreSQL store."""

    def __init__(self, store: PostgresMemoryLedger) -> None:
        self._store = store

    def save(self, observation: Observation) -> Observation:
        return self._store.save(observation)

    def list_for_workspace(
        self, tenant_id: UUID, workspace_id: UUID
    ) -> tuple[Observation, ...]:
        return self._store.list_observations(tenant_id, workspace_id)
