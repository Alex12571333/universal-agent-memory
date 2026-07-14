"""PostgreSQL system-of-record adapter with transactional outbox semantics."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from memory_plane.config.secrets import read_secret_env
from memory_plane.contracts.dto import Candidate, RecallQuery
from memory_plane.contracts.events import ClaimedEvent, ConsumerClaim, IntegrationEvent
from memory_plane.domain.api_key import ApiKeyRecord
from memory_plane.domain.audit import AuditEvent
from memory_plane.domain.checkpoint import Checkpoint, StaleRevisionError
from memory_plane.domain.conflict import ConflictReviewDecision, ConflictReviewStatus
from memory_plane.domain.conversation import (
    PURGED_CONVERSATION_CONTENT,
    ConversationMessage,
    ConversationRetentionPolicy,
    ConversationTurn,
)
from memory_plane.domain.graph import MemoryEdge, MemoryEdgeType
from memory_plane.domain.identity import AgentIdentity, ThreadIdentity, WorkspaceIdentity
from memory_plane.domain.models import (
    MemoryItem,
    MemoryLayer,
    MemoryRevisionConflictError,
    MemoryScope,
    MemoryStatus,
    Observation,
    Provenance,
)
from memory_plane.domain.proposal import (
    MemoryProposal,
    MemoryProposalStatus,
    MemoryProposalTarget,
)
from memory_plane.services.protected_search import (
    protected_document_marker,
    protected_index_digests,
    protected_tokens,
)


def _scope_idempotency_key(workspace_id: UUID, key: str | None) -> str | None:
    """Namespace retry keys so independent workspaces cannot collide."""
    return None if key is None else f"{workspace_id}:{key}"

_PGCRYPTO_TEXT_PREFIX = "enc:pgcrypto:v1:"
_PGCRYPTO_JSON_KEY = "_uam_encrypted_json_v1"
_RUNTIME_ACL_REQUIRED = {
    "outbox_events_update": ("outbox_events", "update"),
    "checkpoints_delete": ("checkpoints", "delete"),
}
_RUNTIME_ACL_FORBIDDEN = {
    "memory_items_update": ("memory_items", "update"),
    "memory_items_delete": ("memory_items", "delete"),
    "audit_events_update": ("audit_events", "update"),
    "audit_events_delete": ("audit_events", "delete"),
}

_WORD = re.compile(r"\w+", re.UNICODE)
_CONVERSATION_CONTENT_SQL = f"""
case
  when left(m.content, {len(_PGCRYPTO_TEXT_PREFIX)}) = '{_PGCRYPTO_TEXT_PREFIX}'
  then pgp_sym_decrypt(
    decode(substr(m.content, {len(_PGCRYPTO_TEXT_PREFIX) + 1}), 'base64'),
    nullif(current_setting('app.memory_text_encryption_key', true), '')
  )
  else m.content
end
"""
_PROPOSAL_TEXT_SQL = f"""
case when left(p.proposal, {len(_PGCRYPTO_TEXT_PREFIX)}) = '{_PGCRYPTO_TEXT_PREFIX}'
then pgp_sym_decrypt(decode(substr(p.proposal, {len(_PGCRYPTO_TEXT_PREFIX) + 1}), 'base64'),
  nullif(current_setting('app.memory_text_encryption_key', true), ''))
else p.proposal end
"""
_PROPOSAL_EVIDENCE_SQL = f"""
case when left(p.evidence, {len(_PGCRYPTO_TEXT_PREFIX)}) = '{_PGCRYPTO_TEXT_PREFIX}'
then pgp_sym_decrypt(decode(substr(p.evidence, {len(_PGCRYPTO_TEXT_PREFIX) + 1}), 'base64'),
  nullif(current_setting('app.memory_text_encryption_key', true), ''))
else p.evidence end
"""
_PROVENANCE_QUOTE_SQL = f"""
case
  when left(p.quote_text, {len(_PGCRYPTO_TEXT_PREFIX)}) = '{_PGCRYPTO_TEXT_PREFIX}'
  then pgp_sym_decrypt(
    decode(substr(p.quote_text, {len(_PGCRYPTO_TEXT_PREFIX) + 1}), 'base64'),
    nullif(current_setting('app.memory_text_encryption_key', true), '')
  )
  else p.quote_text
end
"""
_OBSERVATION_SUMMARY_SQL = f"""
case
  when left(o.summary, {len(_PGCRYPTO_TEXT_PREFIX)}) = '{_PGCRYPTO_TEXT_PREFIX}'
  then pgp_sym_decrypt(
    decode(substr(o.summary, {len(_PGCRYPTO_TEXT_PREFIX) + 1}), 'base64'),
    nullif(current_setting('app.memory_text_encryption_key', true), '')
  )
  else o.summary
end
"""


def _encrypted_json_sql(column: str) -> str:
    """Read a backward-compatible pgcrypto JSON wrapper from *column*."""
    return f"""
    case
      when jsonb_typeof({column}) = 'object'
        and {column} ? '{_PGCRYPTO_JSON_KEY}'
        and ({column} - '{_PGCRYPTO_JSON_KEY}') = '{{}}'::jsonb
        and left(
          {column} ->> '{_PGCRYPTO_JSON_KEY}',
          {len(_PGCRYPTO_TEXT_PREFIX)}
        ) = '{_PGCRYPTO_TEXT_PREFIX}'
      then pgp_sym_decrypt(
        decode(
          substr({column} ->> '{_PGCRYPTO_JSON_KEY}', {len(_PGCRYPTO_TEXT_PREFIX) + 1}),
          'base64'
        ),
        nullif(current_setting('app.memory_text_encryption_key', true), '')
      )::jsonb
      else {column}
    end
    """


_AUDIT_METADATA_SQL = _encrypted_json_sql("a.metadata")
_CHECKPOINT_STATE_SQL = _encrypted_json_sql("state")
_ITEM_METADATA_SQL = _encrypted_json_sql("m.metadata")
_TURN_METADATA_SQL = _encrypted_json_sql("t.metadata")
_MESSAGE_METADATA_SQL = _encrypted_json_sql("m.metadata")
_PROPOSAL_METADATA_SQL = _encrypted_json_sql("p.metadata")
_OUTBOX_PAYLOAD_SQL = _encrypted_json_sql("o.payload")
_AGENT_CONFIG_SQL = _encrypted_json_sql("config")
_ITEM_COLUMNS = f"""
    m.id, m.tenant_id, m.workspace_id, m.agent_id, m.thread_id,
    m.layer, m.scope, m.kind,
    case
      when left(m.text, {len(_PGCRYPTO_TEXT_PREFIX)}) = '{_PGCRYPTO_TEXT_PREFIX}'
      then pgp_sym_decrypt(
        decode(substr(m.text, {len(_PGCRYPTO_TEXT_PREFIX) + 1}), 'base64'),
        nullif(current_setting('app.memory_text_encryption_key', true), '')
      )
      else m.text
    end as text,
    m.labels, {_ITEM_METADATA_SQL} as metadata, m.status,
    m.importance, m.salience, m.confidence, m.observed_at,
    m.valid_from, m.valid_to, m.created_at, m.revision, m.supersedes_id,
    p.source_kind, p.origin_uri, p.object_key, p.checksum_sha256,
    {_PROVENANCE_QUOTE_SQL} as quote_text, p.extraction_version
"""


def _is_foreign_key_violation(exc: Exception) -> bool:
    """Return whether a psycopg exception represents a missing FK target."""
    try:
        from psycopg.errors import ForeignKeyViolation
    except ImportError:
        return False
    return isinstance(exc, ForeignKeyViolation)


def _is_unique_violation(exc: Exception) -> bool:
    """Return whether a psycopg exception represents a global ID collision."""
    try:
        from psycopg.errors import UniqueViolation
    except ImportError:
        return False
    return isinstance(exc, UniqueViolation)


def _parse_text_encryption_scopes(raw: str) -> frozenset[MemoryScope] | None:
    """Parse UAM_MEMORY_TEXT_ENCRYPTION_SCOPES.

    `all` means every canonical memory row is encrypted. Otherwise the value is a
    comma-separated list of MemoryScope values such as `private,thread`.
    """
    normalized = raw.strip().lower()
    if not normalized or normalized == "all":
        return None
    scopes: set[MemoryScope] = set()
    valid = {scope.value for scope in MemoryScope}
    for value in (part.strip().lower() for part in normalized.split(",")):
        if not value:
            continue
        if value not in valid:
            raise ValueError(
                "UAM_MEMORY_TEXT_ENCRYPTION_SCOPES must be all or a comma-separated "
                f"list of memory scopes: {', '.join(sorted(valid))}"
            )
        scopes.add(MemoryScope(value))
    if not scopes:
        raise ValueError("UAM_MEMORY_TEXT_ENCRYPTION_SCOPES must not be empty")
    return frozenset(scopes)


class PostgresMemoryLedger:
    """Psycopg implementation of the retention store and canonical ledger."""

    def __init__(self, dsn: str) -> None:
        """Capture configuration without opening a connection at import time."""
        if not dsn.strip():
            raise ValueError("PostgreSQL DSN must not be empty")
        self.dsn = dsn
        self._pool: Any | None = None
        self._text_encryption_mode = os.getenv("UAM_MEMORY_TEXT_ENCRYPTION", "off").lower()
        self._text_encryption_key = read_secret_env("UAM_MEMORY_TEXT_ENCRYPTION_KEY") or ""
        self._protected_search_index_mode = os.getenv(
            "UAM_PROTECTED_SEARCH_INDEX", "off"
        ).strip().lower()
        self._protected_search_index_key = (
            read_secret_env("UAM_PROTECTED_SEARCH_INDEX_KEY") or ""
        )
        protected_search_key_version_raw = os.getenv(
            "UAM_PROTECTED_SEARCH_INDEX_KEY_VERSION", "1"
        )
        self._text_encryption_scopes = _parse_text_encryption_scopes(
            os.getenv("UAM_MEMORY_TEXT_ENCRYPTION_SCOPES", "all")
        )
        self._enforce_runtime_acl = (
            os.getenv("UAM_ENFORCE_RUNTIME_DB_ACL", "false").strip().lower() == "true"
        )
        if self._text_encryption_mode not in {"off", "pgcrypto"}:
            raise ValueError("UAM_MEMORY_TEXT_ENCRYPTION must be off or pgcrypto")
        if self._text_encryption_enabled and not self._text_encryption_key:
            raise ValueError(
                "UAM_MEMORY_TEXT_ENCRYPTION_KEY is required when "
                "UAM_MEMORY_TEXT_ENCRYPTION=pgcrypto"
            )
        if self._protected_search_index_mode not in {"off", "hmac-v1"}:
            raise ValueError("UAM_PROTECTED_SEARCH_INDEX must be off or hmac-v1")
        if self._protected_search_index_mode == "hmac-v1":
            if not self._protected_search_index_key:
                raise ValueError(
                    "UAM_PROTECTED_SEARCH_INDEX_KEY is required when "
                    "UAM_PROTECTED_SEARCH_INDEX=hmac-v1"
                )
            if self._protected_search_index_key == self._text_encryption_key:
                raise ValueError(
                    "UAM_PROTECTED_SEARCH_INDEX_KEY must differ from "
                    "UAM_MEMORY_TEXT_ENCRYPTION_KEY"
                )
            try:
                self._protected_search_index_key_version = int(
                    protected_search_key_version_raw
                )
            except ValueError as error:
                raise ValueError(
                    "UAM_PROTECTED_SEARCH_INDEX_KEY_VERSION must be a positive integer"
                ) from error
            if not 0 < self._protected_search_index_key_version <= 32767:
                raise ValueError(
                    "UAM_PROTECTED_SEARCH_INDEX_KEY_VERSION must be between 1 and 32767"
                )
        else:
            self._protected_search_index_key_version = 1

    def connect(self) -> None:
        """Check that PostgreSQL is reachable and the schema is installed."""
        with self._connection() as connection:
            row = connection.execute("select to_regclass('memory_items') as table_name").fetchone()
            if row is None or row["table_name"] is None:
                raise RuntimeError("memory schema is not installed")
            if self._enforce_runtime_acl:
                self._verify_runtime_acl(connection)

    @staticmethod
    def _verify_runtime_acl(connection: Any) -> None:
        """Fail closed if the runtime login can mutate canonical/audit history."""
        checks = {**_RUNTIME_ACL_REQUIRED, **_RUNTIME_ACL_FORBIDDEN}
        expressions = ", ".join(
            "has_table_privilege(current_user, %s, %s) as " + name
            for name in checks
        )
        params = tuple(
            value for table, privilege in checks.values() for value in (table, privilege)
        )
        row = connection.execute(f"select {expressions}", params).fetchone()
        if row is None:
            raise RuntimeError("runtime database ACL verification returned no result")
        missing = [name for name in _RUNTIME_ACL_REQUIRED if not row[name]]
        forbidden = [name for name in _RUNTIME_ACL_FORBIDDEN if row[name]]
        if missing or forbidden:
            details = ", ".join([
                *(f"missing:{name}" for name in missing),
                *(f"forbidden:{name}" for name in forbidden),
            ])
            raise RuntimeError(
                "runtime database ACL is unsafe; rerun the migration job before serving: "
                + details
            )

    def ping(self) -> bool:
        """Actively verify canonical PostgreSQL readiness."""
        with self._connection() as connection:
            row = connection.execute("select 1 as ready").fetchone()
        return bool(row and row["ready"] == 1)

    @contextmanager
    def workspace_operation_lock(
        self, tenant_id: UUID, workspace_id: UUID, operation: str
    ) -> Iterator[None]:
        """Hold a PostgreSQL session lock for one externally visible operation.

        Reindex mutates Qdrant outside a SQL transaction, so a transaction lock
        would be released before the vector replacement completed.  This session
        lock stays leased on one pooled connection until the caller finishes.
        """
        key = f"obelisk:{operation}:{tenant_id}:{workspace_id}"
        with self._connection() as connection:
            self._set_tenant(connection, tenant_id)
            connection.execute("select pg_advisory_lock(hashtextextended(%s, 0))", (key,))
            try:
                yield
            finally:
                connection.execute(
                    "select pg_advisory_unlock(hashtextextended(%s, 0))", (key,)
                )

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

    def provision_workspace(self, workspace: WorkspaceIdentity) -> WorkspaceIdentity:
        """Create or return an existing tenant workspace without granting UPDATE."""
        with self._connection() as connection:
            self._set_tenant(connection, workspace.tenant_id)
            tenant = connection.execute(
                "select id from tenants where id = %s",
                (workspace.tenant_id,),
            ).fetchone()
            if tenant is None:
                raise ValueError("tenant scope is not provisioned")
            try:
                row = connection.execute(
                    """
                    insert into workspaces (id, tenant_id, name)
                    values (%s, %s, %s)
                    on conflict do nothing
                    returning id, tenant_id, name
                    """,
                    (workspace.id, workspace.tenant_id, workspace.name),
                ).fetchone()
            except Exception as exc:
                if _is_unique_violation(exc):
                    raise ValueError("workspace_name already belongs to this tenant") from exc
                raise
            if row is None:
                row = connection.execute(
                    "select id, tenant_id, name from workspaces where id = %s",
                    (workspace.id,),
                ).fetchone()
                if row is None:
                    raise ValueError(
                        "workspace_id already belongs to another tenant or name exists"
                    )
            return WorkspaceIdentity(id=row["id"], tenant_id=row["tenant_id"], name=row["name"])

    def provision_agent_thread(
        self,
        agent: AgentIdentity,
        *,
        thread_id: UUID | None = None,
        thread_status: str = "active",
    ) -> tuple[AgentIdentity, ThreadIdentity | None]:
        """Atomically upsert one scoped agent and optional owned thread."""
        from psycopg.types.json import Jsonb

        with self._connection() as connection:
            self._set_tenant(connection, agent.tenant_id)
            workspace = connection.execute(
                "select id from workspaces where id = %s and tenant_id = %s",
                (agent.workspace_id, agent.tenant_id),
            ).fetchone()
            if workspace is None:
                raise ValueError("tenant/workspace scope is not provisioned")
            try:
                agent_row = connection.execute(
                    f"""
                    insert into agents (id, tenant_id, workspace_id, name, role, config)
                    values (%s, %s, %s, %s, %s, %s)
                    on conflict (id) do update set
                      name = excluded.name,
                      role = excluded.role,
                      config = excluded.config
                    where agents.tenant_id = excluded.tenant_id
                      and agents.workspace_id = excluded.workspace_id
                    returning id, tenant_id, workspace_id, name, role,
                      {_AGENT_CONFIG_SQL} as config
                    """,
                    (
                        agent.id,
                        agent.tenant_id,
                        agent.workspace_id,
                        agent.name,
                        agent.role,
                        Jsonb(self._stored_sensitive_json(connection, agent.config)),
                    ),
                ).fetchone()
            except Exception as exc:
                if _is_unique_violation(exc):
                    raise ValueError("agent_id already belongs to another scope") from exc
                raise
            if agent_row is None:
                raise ValueError("agent_id already belongs to another scope")

            stored_agent = AgentIdentity(
                id=agent_row["id"],
                tenant_id=agent_row["tenant_id"],
                workspace_id=agent_row["workspace_id"],
                name=agent_row["name"],
                role=agent_row["role"],
                config=dict(agent_row["config"]),
            )
            if thread_id is None:
                return stored_agent, None
            try:
                thread_row = connection.execute(
                    """
                    insert into threads (
                      id, tenant_id, workspace_id, owner_agent_id, status
                    ) values (%s, %s, %s, %s, %s)
                    on conflict (id) do update set
                      owner_agent_id = excluded.owner_agent_id,
                      status = excluded.status
                    where threads.tenant_id = excluded.tenant_id
                      and threads.workspace_id = excluded.workspace_id
                    returning id, tenant_id, workspace_id, owner_agent_id, status
                    """,
                    (
                        thread_id,
                        agent.tenant_id,
                        agent.workspace_id,
                        agent.id,
                        thread_status,
                    ),
                ).fetchone()
            except Exception as exc:
                if _is_unique_violation(exc):
                    raise ValueError("thread_id already belongs to another scope") from exc
                raise
            if thread_row is None:
                raise ValueError("thread_id already belongs to another scope")
            return stored_agent, ThreadIdentity(
                id=thread_row["id"],
                tenant_id=thread_row["tenant_id"],
                workspace_id=thread_row["workspace_id"],
                owner_agent_id=thread_row["owner_agent_id"],
                status=thread_row["status"],
            )

    def thread_belongs_to_agent(
        self,
        tenant_id: UUID,
        workspace_id: UUID,
        agent_id: UUID,
        thread_id: UUID,
    ) -> bool:
        """Validate a thread owner under RLS in one bounded query."""
        with self._connection() as connection:
            self._set_tenant(connection, tenant_id)
            row = connection.execute(
                """
                select exists (
                  select 1 from threads
                  where id = %s
                    and workspace_id = %s
                    and owner_agent_id = %s
                ) as owned
                """,
                (thread_id, workspace_id, agent_id),
            ).fetchone()
        return bool(row and row["owned"])

    def retain(
        self,
        item: MemoryItem,
        event: IntegrationEvent,
        idempotency_key: str | None = None,
        audit_event: AuditEvent | None = None,
    ) -> tuple[MemoryItem, bool]:
        """Atomically append memory, provenance, idempotency key and outbox event."""
        idempotency_key = _scope_idempotency_key(item.workspace_id, idempotency_key)
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

            try:
                self._insert_item(connection, item)
            except Exception as exc:
                if _is_foreign_key_violation(exc):
                    raise ValueError("tenant or workspace is not provisioned") from exc
                raise
            if idempotency_key:
                connection.execute(
                    """
                    insert into idempotency_keys (tenant_id, key, memory_item_id)
                    values (%s, %s, %s)
                    """,
                    (item.tenant_id, idempotency_key, item.id),
                )
            self._insert_event(connection, event)
            if audit_event is not None:
                self._insert_audit_event(connection, audit_event)
            return item, True

    def supersede_if_current(
        self,
        item: MemoryItem,
        event: IntegrationEvent,
        *,
        expected_revision: int,
        idempotency_key: str | None = None,
        audit_event: AuditEvent | None = None,
    ) -> tuple[MemoryItem, bool]:
        """CAS append a replacement and its outbox event in one transaction."""
        idempotency_key = _scope_idempotency_key(item.workspace_id, idempotency_key)
        if item.supersedes_id is None:
            raise ValueError("replacement item must declare supersedes_id")
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

            # The runtime application role intentionally has no UPDATE grant on
            # memory_items. A row-level FOR UPDATE lock would therefore fail in
            # a hardened deployment. Serialise replacements for the immutable
            # root ID with a transaction-scoped advisory lock instead, then
            # re-read the current chain while the lock is held.
            connection.execute(
                "select pg_advisory_xact_lock(hashtextextended(%s::text, 0))",
                (item.supersedes_id,),
            )
            parent = connection.execute(
                """
                select id, revision
                from memory_items
                where id = %s and deleted_at is null
                """,
                (item.supersedes_id,),
            ).fetchone()
            if parent is None:
                raise KeyError("memory item not found")

            head = connection.execute(
                """
                with recursive chain as (
                  select id, revision
                  from memory_items
                  where id = %s and deleted_at is null
                  union all
                  select child.id, child.revision
                  from memory_items child
                  join chain parent on child.supersedes_id = parent.id
                  where child.deleted_at is null
                )
                select id, revision
                from chain
                order by revision desc, id desc
                limit 1
                """,
                (item.supersedes_id,),
            ).fetchone()
            actual = head["revision"] if head is not None else parent["revision"]
            if head is not None and (
                head["id"] != item.supersedes_id
                or parent["revision"] != expected_revision
            ):
                raise MemoryRevisionConflictError(
                    item.supersedes_id, expected_revision, actual
                )

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
            if audit_event is not None:
                self._insert_audit_event(connection, audit_event)
            return item, True

    def claim_outbox(
        self,
        tenant_id: UUID,
        worker_id: str,
        *,
        limit: int,
        lease_seconds: int,
    ) -> tuple[ClaimedEvent, ...]:
        """Lease due events concurrently with `FOR UPDATE SKIP LOCKED`."""
        with self._connection() as connection:
            self._set_tenant(connection, tenant_id)
            rows = connection.execute(
                f"""
                with due as (
                  select id
                  from outbox_events
                  where published_at is null
                    and dead_lettered_at is null
                    and available_at <= clock_timestamp()
                    and (lease_until is null or lease_until < clock_timestamp())
                  order by occurred_at, id
                  for update skip locked
                  limit %s
                )
                update outbox_events o
                set lease_owner = %s,
                    lease_until = clock_timestamp() + make_interval(secs => %s),
                    attempts = o.attempts + 1,
                    last_error = null
                from due
                where o.id = due.id
                returning
                  o.id, o.tenant_id, o.workspace_id, o.name,
                  {_OUTBOX_PAYLOAD_SQL} as payload,
                  o.correlation_id, o.occurred_at, o.attempts
                """,
                (limit, worker_id, lease_seconds),
            ).fetchall()
        return tuple(
            ClaimedEvent(
                event=IntegrationEvent(
                    id=row["id"],
                    tenant_id=row["tenant_id"],
                    workspace_id=row["workspace_id"],
                    name=row["name"],
                    payload=row["payload"],
                    correlation_id=row["correlation_id"],
                    occurred_at=row["occurred_at"],
                ),
                attempts=row["attempts"],
            )
            for row in rows
        )

    def mark_outbox_published(
        self, tenant_id: UUID, event_id: UUID, worker_id: str
    ) -> bool:
        """Acknowledge publication only for the worker holding the lease."""
        with self._connection() as connection:
            self._set_tenant(connection, tenant_id)
            row = connection.execute(
                """
                update outbox_events
                set published_at = clock_timestamp(),
                    lease_owner = null,
                    lease_until = null,
                    last_error = null
                where id = %s
                  and lease_owner = %s
                  and published_at is null
                returning id
                """,
                (event_id, worker_id),
            ).fetchone()
        return row is not None

    def release_outbox(
        self,
        tenant_id: UUID,
        event_id: UUID,
        worker_id: str,
        *,
        error: str,
        max_attempts: int,
        retry_delay_seconds: int,
    ) -> bool:
        """Release a failed lease or dead-letter an exhausted event."""
        with self._connection() as connection:
            self._set_tenant(connection, tenant_id)
            row = connection.execute(
                """
                update outbox_events
                set lease_owner = null,
                    lease_until = null,
                    last_error = %s,
                    available_at = case
                      when attempts >= %s then clock_timestamp()
                      else clock_timestamp() + make_interval(secs => %s)
                    end,
                    dead_lettered_at = case
                      when attempts >= %s then clock_timestamp()
                      else dead_lettered_at
                    end
                where id = %s
                  and lease_owner = %s
                  and published_at is null
                returning id
                """,
                (error, max_attempts, retry_delay_seconds, max_attempts, event_id, worker_id),
            ).fetchone()
        return row is not None

    def claim_event_processing(
        self,
        tenant_id: UUID,
        event_id: UUID,
        consumer: str,
        worker_id: str,
        *,
        lease_seconds: int,
    ) -> ConsumerClaim:
        """Acquire a per-consumer event lease without simultaneous duplicates."""
        with self._connection() as connection:
            self._set_tenant(connection, tenant_id)
            inserted = connection.execute(
                """
                insert into processed_events (
                  tenant_id, event_id, consumer, lease_owner, lease_until, attempts
                ) values (
                  %s, %s, %s, %s,
                  clock_timestamp() + make_interval(secs => %s), 1
                )
                on conflict do nothing
                returning event_id
                """,
                (tenant_id, event_id, consumer, worker_id, lease_seconds),
            ).fetchone()
            if inserted is not None:
                return ConsumerClaim.ACQUIRED

            existing = connection.execute(
                """
                select processed_at
                from processed_events
                where event_id = %s and consumer = %s
                for update
                """,
                (event_id, consumer),
            ).fetchone()
            if existing is not None and existing["processed_at"] is not None:
                return ConsumerClaim.COMPLETED

            acquired = connection.execute(
                """
                update processed_events
                set lease_owner = %s,
                    lease_until = clock_timestamp() + make_interval(secs => %s),
                    attempts = attempts + 1,
                    last_error = null
                where event_id = %s
                  and consumer = %s
                  and processed_at is null
                  and (lease_until is null or lease_until < clock_timestamp())
                returning event_id
                """,
                (worker_id, lease_seconds, event_id, consumer),
            ).fetchone()
            return ConsumerClaim.ACQUIRED if acquired is not None else ConsumerClaim.BUSY

    def complete_event_processing(
        self,
        tenant_id: UUID,
        event_id: UUID,
        consumer: str,
        worker_id: str,
    ) -> bool:
        """Persist completion only for the worker holding the consumer lease."""
        with self._connection() as connection:
            self._set_tenant(connection, tenant_id)
            row = connection.execute(
                """
                update processed_events
                set processed_at = clock_timestamp(),
                    lease_owner = null,
                    lease_until = null,
                    last_error = null
                where event_id = %s
                  and consumer = %s
                  and lease_owner = %s
                  and processed_at is null
                returning event_id
                """,
                (event_id, consumer, worker_id),
            ).fetchone()
        return row is not None

    def release_event_processing(
        self,
        tenant_id: UUID,
        event_id: UUID,
        consumer: str,
        worker_id: str,
        *,
        error: str,
    ) -> bool:
        """Release a failed handler lease so JetStream can redeliver."""
        with self._connection() as connection:
            self._set_tenant(connection, tenant_id)
            row = connection.execute(
                """
                update processed_events
                set lease_owner = null,
                    lease_until = null,
                    last_error = %s
                where event_id = %s
                  and consumer = %s
                  and lease_owner = %s
                  and processed_at is null
                returning event_id
                """,
                (error, event_id, consumer, worker_id),
            ).fetchone()
        return row is not None

    def collect_metrics(self, tenant_id: UUID) -> dict[str, float | int]:
        """Collect operational counters under the tenant RLS boundary."""
        with self._connection() as connection:
            self._set_tenant(connection, tenant_id)
            row = connection.execute(
                """
                select
                  (select count(*) from memory_items where deleted_at is null)
                    as memory_items_total,
                  (select count(*) from observations) as observations_total,
                  (select count(*) from checkpoints) as checkpoints_total,
                  (
                    select count(*)
                    from outbox_events
                    where published_at is null and dead_lettered_at is null
                  ) as outbox_pending_total,
                  (
                    select count(*)
                    from outbox_events
                    where dead_lettered_at is not null
                  ) as outbox_dead_letter_total,
                  coalesce((
                    select extract(epoch from clock_timestamp() - min(occurred_at))
                    from outbox_events
                    where published_at is null and dead_lettered_at is null
                  ), 0) as outbox_lag_seconds,
                  (
                    select count(*)
                    from processed_events
                    where processed_at is null
                      and lease_until is not null
                      and lease_until >= clock_timestamp()
                  ) as processed_events_inflight_total,
                  (select count(*) from audit_events) as audit_events_total,
                  (select count(*) from api_key_registry) as api_keys_total,
                  (
                    select count(*)
                    from api_key_registry
                    where revoked_at is not null
                  ) as api_keys_revoked_total
                """
            ).fetchone()
        return {
            "memory_items_total": row["memory_items_total"],
            "observations_total": row["observations_total"],
            "checkpoints_total": row["checkpoints_total"],
            "outbox_pending_total": row["outbox_pending_total"],
            "outbox_dead_letter_total": row["outbox_dead_letter_total"],
            "outbox_lag_seconds": float(row["outbox_lag_seconds"]),
            "processed_events_inflight_total": row["processed_events_inflight_total"],
            "audit_events_total": row["audit_events_total"],
            "api_keys_total": row["api_keys_total"],
            "api_keys_revoked_total": row["api_keys_revoked_total"],
        }

    def workspace_embedding_stale(self, tenant_id: UUID, workspace_id: UUID) -> bool:
        """Return whether a workspace has not completed its embedding delivery.

        An event is stale until the outbox published it *and* the durable
        ``embed-v1`` consumer marked it complete. Dead-lettered events stay
        stale so recall cannot claim a healthy vector index.
        """
        with self._connection() as connection:
            self._set_tenant(connection, tenant_id)
            row = connection.execute(
                """
                select exists(
                  select 1
                  from outbox_events o
                  left join processed_events p
                    on p.tenant_id = o.tenant_id
                   and p.event_id = o.id
                   and p.consumer = 'embed-v1'
                  where o.workspace_id = %s
                    and o.name = 'memory.retained.v1'
                    and (
                      o.published_at is null
                      or o.dead_lettered_at is not null
                      or p.processed_at is null
                    )
                ) as stale
                """,
                (workspace_id,),
            ).fetchone()
        return bool(row and row["stale"])

    def workspace_embedding_freshness(self, tenant_id: UUID, workspace_id: UUID):
        """Return exact durable embedding state for recallable heads.

        Outbox and per-consumer completion records are the source of truth. A
        head with no retained-event delivery is reported as stale rather than
        guessed fresh, which also surfaces legacy/imported rows that require a
        scoped reindex.
        """
        from memory_plane.contracts.dto import IndexFreshness

        with self._connection() as connection:
            self._set_tenant(connection, tenant_id)
            row = connection.execute(
                """
                with active_heads as (
                  select m.id
                  from memory_items m
                  where m.workspace_id = %s
                    and m.deleted_at is null
                    and m.status not in ('rejected', 'archived')
                    and not exists (
                      select 1
                      from memory_items child
                      where child.supersedes_id = m.id
                        and child.deleted_at is null
                    )
                ), deliveries as (
                  select
                    m.id,
                    o.id as event_id,
                    o.published_at,
                    o.dead_lettered_at,
                    p.processed_at,
                    p.lease_until
                  from active_heads m
                  left join lateral (
                    select id, published_at, dead_lettered_at
                    from outbox_events
                    where tenant_id = %s
                      and workspace_id = %s
                      and name = 'memory.retained.v1'
                      and correlation_id = m.id
                    order by occurred_at desc, id desc
                    limit 1
                  ) o on true
                  left join processed_events p
                    on p.tenant_id = %s
                   and p.event_id = o.id
                   and p.consumer = 'embed-v1'
                )
                select
                  count(*) as active_memory_count,
                  count(*) filter (
                    where event_id is null
                       or published_at is null
                       or dead_lettered_at is not null
                       or processed_at is null
                  ) as stale_memory_count,
                  count(*) filter (
                    where event_id is not null
                      and published_at is null
                      and dead_lettered_at is null
                  ) as unpublished_memory_count,
                  count(*) filter (
                    where event_id is not null
                      and published_at is not null
                      and dead_lettered_at is null
                      and processed_at is null
                  ) as processing_memory_count,
                  count(*) filter (where dead_lettered_at is not null) as dead_letter_memory_count,
                  count(*) filter (where event_id is null) as missing_delivery_memory_count
                from deliveries
                """,
                (workspace_id, tenant_id, workspace_id, tenant_id),
            ).fetchone()
        return IndexFreshness(
            active_memory_count=int(row["active_memory_count"]),
            stale_memory_count=int(row["stale_memory_count"]),
            unpublished_memory_count=int(row["unpublished_memory_count"]),
            processing_memory_count=int(row["processing_memory_count"]),
            dead_letter_memory_count=int(row["dead_letter_memory_count"]),
            missing_delivery_memory_count=int(row["missing_delivery_memory_count"]),
        )

    def append_audit_event(self, event: AuditEvent) -> AuditEvent:
        """Append one operator/agent audit event under RLS."""
        from psycopg.types.json import Jsonb

        with self._connection() as connection:
            self._set_tenant(connection, event.tenant_id)
            connection.execute(
                """
                insert into audit_events (
                  id, tenant_id, workspace_id, action, actor, actor_type,
                  resource_type, resource_id, status, metadata, created_at
                ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (id) do nothing
                """,
                (
                    event.id,
                    event.tenant_id,
                    event.workspace_id,
                    event.action,
                    event.actor,
                    event.actor_type,
                    event.resource_type,
                    event.resource_id,
                    event.status,
                    Jsonb(self._stored_sensitive_json(connection, event.metadata)),
                    event.created_at,
                ),
            )
        return event

    def get_audit_event(self, tenant_id: UUID, event_id: UUID) -> AuditEvent | None:
        """Load one audit event under RLS for an operator replay."""
        with self._connection() as connection:
            self._set_tenant(connection, tenant_id)
            row = connection.execute(
                f"""
                select id, tenant_id, workspace_id, action, actor, actor_type,
                  resource_type, resource_id, status, {_AUDIT_METADATA_SQL} as metadata, created_at
                from audit_events a
                where id = %s
                """,
                (event_id,),
            ).fetchone()
        return self._to_audit_event(row) if row is not None else None

    def list_audit_events(
        self,
        tenant_id: UUID,
        *,
        workspace_id: UUID | None = None,
        action: str | None = None,
        resource_type: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        before_event_id: UUID | None = None,
        limit: int = 100,
    ) -> tuple[AuditEvent, ...]:
        """List recent audit events under RLS."""
        safe_limit = max(1, min(int(limit), 500))
        with self._connection() as connection:
            self._set_tenant(connection, tenant_id)
            rows = connection.execute(
                f"""
                select id, tenant_id, workspace_id, action, actor, actor_type,
                  resource_type, resource_id, status, {_AUDIT_METADATA_SQL} as metadata, created_at
                from audit_events a
                where (%s::uuid is null or workspace_id = %s::uuid)
                  and (%s::text is null or action = %s::text)
                  and (%s::text is null or resource_type = %s::text)
                  and (%s::timestamptz is null or created_at >= %s::timestamptz)
                  and (
                    %s::timestamptz is null
                    or created_at < %s::timestamptz
                    or (
                      %s::uuid is not null
                      and created_at = %s::timestamptz
                      and id::text < %s::text
                    )
                  )
                order by created_at desc, id desc
                limit %s
                """,
                (
                    workspace_id,
                    workspace_id,
                    action,
                    action,
                    resource_type,
                    resource_type,
                    created_after,
                    created_after,
                    created_before,
                    created_before,
                    before_event_id,
                    created_before,
                    str(before_event_id) if before_event_id is not None else None,
                    safe_limit,
                ),
            ).fetchall()
        return tuple(self._to_audit_event(row) for row in rows)

    def prune_audit_events(
        self,
        tenant_id: UUID,
        *,
        created_before: datetime,
        workspace_id: UUID | None = None,
        limit: int = 500,
    ) -> int:
        """Delete old audit events under RLS after external retention export."""
        safe_limit = max(1, min(int(limit), 500))
        with self._connection() as connection:
            self._set_tenant(connection, tenant_id)
            connection.execute(
                "select set_config('uam.audit_retention_mode', 'on', true)"
            )
            result = connection.execute(
                """
                with doomed as (
                  select id
                  from audit_events
                  where created_at < %s::timestamptz
                    and (%s::uuid is null or workspace_id = %s::uuid)
                  order by created_at asc, id asc
                  limit %s
                )
                delete from audit_events a
                using doomed
                where a.id = doomed.id
                  and a.tenant_id = %s
                """,
                (
                    created_before,
                    workspace_id,
                    workspace_id,
                    safe_limit,
                    tenant_id,
                ),
            )
            return int(result.rowcount or 0)

    def save_api_key_record(self, record: ApiKeyRecord) -> ApiKeyRecord:
        """Create/update one API-key metadata row under RLS."""
        with self._connection() as connection:
            self._set_tenant(connection, record.tenant_id)
            connection.execute(
                """
                insert into api_key_registry (
                  id, tenant_id, name, secret_fingerprint, scopes, created_at,
                  last_used_at, revoked_at, revoked_reason
                ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (tenant_id, secret_fingerprint) do update set
                  name = excluded.name,
                  scopes = excluded.scopes,
                  last_used_at = coalesce(
                    api_key_registry.last_used_at,
                    excluded.last_used_at
                  ),
                  revoked_at = excluded.revoked_at,
                  revoked_reason = excluded.revoked_reason
                """,
                (
                    record.id,
                    record.tenant_id,
                    record.name,
                    record.secret_fingerprint,
                    list(record.scopes),
                    record.created_at,
                    record.last_used_at,
                    record.revoked_at,
                    record.revoked_reason,
                ),
            )
        stored = self.get_api_key_by_fingerprint(
            record.tenant_id, record.secret_fingerprint
        )
        return record if stored is None else stored

    def get_api_key_by_fingerprint(
        self, tenant_id: UUID, secret_fingerprint: str
    ) -> ApiKeyRecord | None:
        """Load API-key metadata by fingerprint under RLS."""
        with self._connection() as connection:
            self._set_tenant(connection, tenant_id)
            row = connection.execute(
                """
                select id, tenant_id, name, secret_fingerprint, scopes,
                  created_at, last_used_at, revoked_at, revoked_reason
                from api_key_registry
                where secret_fingerprint = %s
                """,
                (secret_fingerprint,),
            ).fetchone()
        return None if row is None else self._to_api_key_record(row)

    def touch_api_key(
        self,
        tenant_id: UUID,
        secret_fingerprint: str,
        *,
        used_at: datetime,
    ) -> ApiKeyRecord | None:
        """Update last-used timestamp for one key under RLS."""
        with self._connection() as connection:
            self._set_tenant(connection, tenant_id)
            row = connection.execute(
                """
                update api_key_registry
                set last_used_at = %s
                where secret_fingerprint = %s
                returning id, tenant_id, name, secret_fingerprint, scopes,
                  created_at, last_used_at, revoked_at, revoked_reason
                """,
                (used_at, secret_fingerprint),
            ).fetchone()
        return None if row is None else self._to_api_key_record(row)

    def list_api_keys(self, tenant_id: UUID) -> tuple[ApiKeyRecord, ...]:
        """List API-key metadata for operator review."""
        with self._connection() as connection:
            self._set_tenant(connection, tenant_id)
            rows = connection.execute(
                """
                select id, tenant_id, name, secret_fingerprint, scopes,
                  created_at, last_used_at, revoked_at, revoked_reason
                from api_key_registry
                order by name, created_at, id
                """
            ).fetchall()
        return tuple(self._to_api_key_record(row) for row in rows)

    def revoke_api_key(
        self,
        tenant_id: UUID,
        key_id: UUID,
        *,
        revoked_at: datetime,
        reason: str = "",
    ) -> ApiKeyRecord:
        """Mark one API key revoked under RLS."""
        with self._connection() as connection:
            self._set_tenant(connection, tenant_id)
            row = connection.execute(
                """
                update api_key_registry
                set revoked_at = %s,
                    revoked_reason = %s
                where id = %s
                returning id, tenant_id, name, secret_fingerprint, scopes,
                  created_at, last_used_at, revoked_at, revoked_reason
                """,
                (revoked_at, reason, key_id),
            ).fetchone()
        if row is None:
            raise KeyError("api key not found")
        return self._to_api_key_record(row)

    def append(
        self, item: MemoryItem, idempotency_key: str | None = None
    ) -> tuple[MemoryItem, bool]:
        """Append without an event for maintenance and import workflows."""
        idempotency_key = _scope_idempotency_key(item.workspace_id, idempotency_key)
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

    def append_turn(
        self,
        turn: ConversationTurn,
        idempotency_key: str | None = None,
        audit_event: AuditEvent | None = None,
        event: IntegrationEvent | None = None,
    ) -> tuple[ConversationTurn, bool]:
        """Append one raw conversation turn and its ordered messages."""
        idempotency_key = _scope_idempotency_key(turn.workspace_id, idempotency_key)
        with self._connection() as connection:
            self._set_tenant(connection, turn.tenant_id)
            if idempotency_key:
                self._lock_idempotency_key(connection, turn.tenant_id, idempotency_key)
                existing = self._get_turn_by_idempotency_key(
                    connection, turn.tenant_id, idempotency_key
                )
                if existing is not None:
                    return existing, False
            self._insert_turn(connection, turn)
            if idempotency_key:
                connection.execute(
                    """
                    insert into conversation_idempotency_keys (
                      tenant_id, key, turn_id
                    ) values (%s, %s, %s)
                    """,
                    (turn.tenant_id, idempotency_key, turn.id),
                )
            if audit_event is not None:
                self._insert_audit_event(connection, audit_event)
            if event is not None:
                self._validate_turn_event(turn, event)
                self._insert_event(connection, event)
            return turn, True

    def get_turn(self, tenant_id: UUID, turn_id: UUID) -> ConversationTurn | None:
        """Load one raw conversation turn under RLS."""
        with self._connection() as connection:
            self._set_tenant(connection, tenant_id)
            row = connection.execute(
                f"""
                select
                  t.id, t.tenant_id, t.workspace_id, t.thread_id, t.agent_id,
                  t.namespace, t.source_kind, t.retention_policy,
                  {_TURN_METADATA_SQL} as metadata,
                  t.raw_content_state, t.created_at, t.expires_at,
                  coalesce(
                    jsonb_agg(
                      jsonb_build_object(
                        'role', m.role,
                        'content', {_CONVERSATION_CONTENT_SQL},
                        'name', m.name,
                        'metadata', {_MESSAGE_METADATA_SQL}
                      )
                      order by m.position
                    ) filter (where m.id is not null),
                    '[]'::jsonb
                  ) as messages
                from conversation_turns t
                left join conversation_messages m on m.turn_id = t.id
                where t.id = %s
                group by t.id
                """,
                (turn_id,),
            ).fetchone()
        return None if row is None else self._to_turn(row)

    def list_turns(
        self,
        tenant_id: UUID,
        workspace_id: UUID,
        *,
        thread_id: UUID | None = None,
        namespace: str | None = None,
        before_created_at: datetime | None = None,
        before_turn_id: UUID | None = None,
        limit: int = 50,
    ) -> tuple[ConversationTurn, ...]:
        """List recent raw conversation turns with their ordered messages."""
        safe_limit = max(1, min(int(limit), 200))
        with self._connection() as connection:
            self._set_tenant(connection, tenant_id)
            rows = connection.execute(
                f"""
                select
                  t.id, t.tenant_id, t.workspace_id, t.thread_id, t.agent_id,
                  t.namespace, t.source_kind, t.retention_policy,
                  {_TURN_METADATA_SQL} as metadata,
                  t.raw_content_state, t.created_at, t.expires_at,
                  coalesce(
                    jsonb_agg(
                      jsonb_build_object(
                        'role', m.role,
                        'content', {_CONVERSATION_CONTENT_SQL},
                        'name', m.name,
                        'metadata', {_MESSAGE_METADATA_SQL}
                      )
                      order by m.position
                    ) filter (where m.id is not null),
                    '[]'::jsonb
                  ) as messages
                from conversation_turns t
                left join conversation_messages m on m.turn_id = t.id
                where t.workspace_id = %s
                  and (%s::uuid is null or t.thread_id = %s::uuid)
                  and (%s::text is null or t.namespace = %s::text)
                  and (
                    %s::timestamptz is null
                    or t.created_at < %s::timestamptz
                    or (
                      %s::uuid is not null
                      and t.created_at = %s::timestamptz
                      and t.id::text < %s::text
                    )
                  )
                group by t.id
                order by t.created_at desc, t.id desc
                limit %s
                """,
                (workspace_id, thread_id, thread_id, namespace, namespace,
                 before_created_at, before_created_at, before_turn_id,
                 before_created_at, str(before_turn_id) if before_turn_id else None, safe_limit),
            ).fetchall()
        return tuple(self._to_turn(row) for row in rows)

    def purge_turn_content(self, tenant_id: UUID, turn_id: UUID) -> bool:
        """Irreversibly redact transcript messages while preserving turn identity."""
        with self._connection() as connection:
            self._set_tenant(connection, tenant_id)
            purged = connection.execute(
                """
                update conversation_turns
                set raw_content_state = 'purged_after_curation'
                where id = %s
                returning id
                """,
                (turn_id,),
            ).fetchone()
            if purged is None:
                return False
            connection.execute(
                """
                update conversation_messages
                set content = %s
                where turn_id = %s
                """,
                (PURGED_CONVERSATION_CONTENT, turn_id),
            )
            return True

    def purge_expired_turns(
        self,
        tenant_id: UUID,
        workspace_id: UUID,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[UUID, ...]:
        """Purge a bounded batch of expired staged transcript text under RLS."""
        safe_limit = max(1, min(int(limit), 5000))
        with self._connection() as connection:
            self._set_tenant(connection, tenant_id)
            rows = connection.execute(
                """
                with due as (
                  select id
                  from conversation_turns
                  where workspace_id = %s
                    and expires_at is not null
                    and expires_at <= %s
                    and raw_content_state = 'active'
                  order by expires_at, id
                  limit %s
                  for update skip locked
                ), updated_turns as (
                  update conversation_turns t
                  set raw_content_state = 'purged_after_expiry'
                  from due
                  where t.id = due.id
                  returning t.id
                )
                update conversation_messages m
                set content = %s
                from updated_turns u
                where m.turn_id = u.id
                returning u.id
                """,
                (workspace_id, now, safe_limit, PURGED_CONVERSATION_CONTENT),
            ).fetchall()
        return tuple(dict.fromkeys(row["id"] for row in rows))

    def append_proposal(
        self,
        proposal: MemoryProposal,
        idempotency_key: str | None = None,
        audit_event: AuditEvent | None = None,
    ) -> tuple[MemoryProposal, bool]:
        """Append one memory proposal under the tenant boundary."""
        idempotency_key = _scope_idempotency_key(proposal.workspace_id, idempotency_key)
        with self._connection() as connection:
            self._set_tenant(connection, proposal.tenant_id)
            if idempotency_key:
                self._lock_idempotency_key(
                    connection,
                    proposal.tenant_id,
                    idempotency_key,
                )
                existing = self._get_proposal_by_idempotency_key(
                    connection,
                    proposal.tenant_id,
                    idempotency_key,
                )
                if existing is not None:
                    return existing, False
            self._insert_proposal(connection, proposal)
            if idempotency_key:
                connection.execute(
                    """
                    insert into memory_proposal_idempotency_keys (
                      tenant_id, key, proposal_id
                    ) values (%s, %s, %s)
                    """,
                    (proposal.tenant_id, idempotency_key, proposal.id),
                )
            if audit_event is not None:
                self._insert_audit_event(connection, audit_event)
            return proposal, True

    def get_proposal(
        self, tenant_id: UUID, proposal_id: UUID
    ) -> MemoryProposal | None:
        """Load one memory proposal under RLS."""
        with self._connection() as connection:
            self._set_tenant(connection, tenant_id)
            row = connection.execute(
                f"""
                select id, tenant_id, workspace_id, agent_id, thread_id, namespace,
                  requester, target, {_PROPOSAL_TEXT_SQL} as proposal,
                  {_PROPOSAL_EVIDENCE_SQL} as evidence, confidence, importance,
                  status, {_PROPOSAL_METADATA_SQL} as metadata,
                  created_at, reviewed_at, reviewer, review_reason
                from memory_proposals p
                where id = %s
                """,
                (proposal_id,),
            ).fetchone()
        return None if row is None else self._to_proposal(row)

    def save_proposal_review(
        self, proposal: MemoryProposal, audit_event: AuditEvent | None = None
    ) -> MemoryProposal:
        """Persist proposal review fields under RLS."""
        from psycopg.types.json import Jsonb

        with self._connection() as connection:
            self._set_tenant(connection, proposal.tenant_id)
            row = connection.execute(
                """
                update memory_proposals
                set status = %s,
                    metadata = %s,
                    reviewed_at = %s,
                    reviewer = %s,
                    review_reason = %s
                where id = %s
                returning id
                """,
                (
                    proposal.status.value,
                    Jsonb(self._stored_sensitive_json(connection, proposal.metadata)),
                    proposal.reviewed_at,
                    proposal.reviewer,
                    proposal.review_reason,
                    proposal.id,
                ),
            ).fetchone()
            if audit_event is not None:
                self._insert_audit_event(connection, audit_event)
        if row is None:
            raise KeyError("memory proposal not found")
        return proposal

    def accept_proposal_with_memory(
        self,
        proposal: MemoryProposal,
        item: MemoryItem,
        event: IntegrationEvent,
        *,
        reviewer: str,
        reason: str,
        idempotency_key: str,
        audit_event: AuditEvent | None = None,
    ) -> tuple[MemoryProposal, MemoryItem, bool]:
        """Atomically accept an open proposal with its memory and outbox event."""
        from psycopg.types.json import Jsonb

        idempotency_key = _scope_idempotency_key(item.workspace_id, idempotency_key)
        assert idempotency_key is not None
        self._validate_event(item, event)
        with self._connection() as connection:
            self._set_tenant(connection, proposal.tenant_id)
            self._lock_idempotency_key(connection, item.tenant_id, idempotency_key)
            row = connection.execute(
                f"""
                select id, tenant_id, workspace_id, agent_id, thread_id, namespace,
                  requester, target, {_PROPOSAL_TEXT_SQL} as proposal,
                  {_PROPOSAL_EVIDENCE_SQL} as evidence, confidence, importance,
                  status, {_PROPOSAL_METADATA_SQL} as metadata,
                  created_at, reviewed_at, reviewer, review_reason
                from memory_proposals p where p.id = %s for update
                """,
                (proposal.id,),
            ).fetchone()
            if row is None:
                raise KeyError("memory proposal not found")
            current = self._to_proposal(row)
            if current.status == MemoryProposalStatus.REJECTED:
                raise ValueError("rejected proposal cannot be accepted")
            existing = self._get_by_idempotency_key(connection, item.tenant_id, idempotency_key)
            if existing is not None:
                return current, existing, False
            if current.status == MemoryProposalStatus.ACCEPTED:
                raise RuntimeError("accepted proposal is missing its idempotent memory record")
            try:
                self._insert_item(connection, item)
            except Exception as exc:
                if _is_foreign_key_violation(exc):
                    raise ValueError("tenant or workspace is not provisioned") from exc
                raise
            connection.execute(
                """
                insert into idempotency_keys (tenant_id, key, memory_item_id)
                values (%s, %s, %s)
                """,
                (item.tenant_id, idempotency_key, item.id),
            )
            self._insert_event(connection, event)
            metadata = {**current.metadata, "accepted_memory_id": str(item.id)}
            updated = connection.execute(
                """
                update memory_proposals
                set status = 'accepted', metadata = %s, reviewed_at = %s,
                    reviewer = %s, review_reason = %s
                where id = %s
                returning id
                """,
                (
                    Jsonb(self._stored_sensitive_json(connection, metadata)),
                    datetime.now(UTC),
                    reviewer.strip()[:120] or "operator",
                    reason.strip()[:1000],
                    proposal.id,
                ),
            ).fetchone()
            if updated is None:
                raise KeyError("memory proposal not found")
            if audit_event is not None:
                self._insert_audit_event(connection, audit_event)
            decoded = connection.execute(
                f"""
                select p.id, p.tenant_id, p.workspace_id, p.agent_id, p.thread_id,
                  p.namespace, p.requester, p.target,
                  {_PROPOSAL_TEXT_SQL} as proposal, {_PROPOSAL_EVIDENCE_SQL} as evidence,
                  p.confidence, p.importance, p.status,
                  {_PROPOSAL_METADATA_SQL} as metadata, p.created_at,
                  p.reviewed_at, p.reviewer, p.review_reason
                from memory_proposals p where p.id = %s
                """,
                (proposal.id,),
            ).fetchone()
            if decoded is None:
                raise KeyError("memory proposal not found")
            return self._to_proposal(decoded), item, True

    def list_proposals(
        self,
        tenant_id: UUID,
        workspace_id: UUID,
        *,
        namespace: str | None = None,
        status: MemoryProposalStatus | None = None,
        before_created_at: datetime | None = None,
        before_proposal_id: UUID | None = None,
        limit: int = 50,
    ) -> tuple[MemoryProposal, ...]:
        """List recent memory proposals under RLS."""
        safe_limit = max(1, min(int(limit), 200))
        with self._connection() as connection:
            self._set_tenant(connection, tenant_id)
            rows = connection.execute(
                f"""
                select id, tenant_id, workspace_id, agent_id, thread_id, namespace,
                  requester, target, {_PROPOSAL_TEXT_SQL} as proposal,
                  {_PROPOSAL_EVIDENCE_SQL} as evidence, confidence, importance,
                  status, {_PROPOSAL_METADATA_SQL} as metadata,
                  created_at, reviewed_at, reviewer, review_reason
                from memory_proposals p
                where workspace_id = %s
                  and (%s::text is null or namespace = %s::text)
                  and (%s::text is null or status = %s::text)
                  and (
                    %s::timestamptz is null
                    or (created_at, id) < (%s::timestamptz, %s::uuid)
                  )
                order by created_at desc, id desc
                limit %s
                """,
                (
                    workspace_id,
                    namespace,
                    namespace,
                    status.value if status else None,
                    status.value if status else None,
                    before_created_at,
                    before_created_at,
                    before_proposal_id,
                    safe_limit,
                ),
            ).fetchall()
        return tuple(self._to_proposal(row) for row in rows)

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

    def is_recallable_head(self, tenant_id: UUID, item_id: UUID) -> bool:
        """Check canonical active-head state under the tenant RLS boundary."""
        return item_id in self.filter_recallable_heads(tenant_id, (item_id,))

    def filter_recallable_heads(
        self,
        tenant_id: UUID,
        item_ids: tuple[UUID, ...],
    ) -> frozenset[UUID]:
        """Resolve active heads for vector candidates in one SQL query."""
        if not item_ids:
            return frozenset()
        with self._connection() as connection:
            self._set_tenant(connection, tenant_id)
            rows = connection.execute(
                """
                select m.id
                from memory_items m
                where m.id = any(%s)
                  and m.deleted_at is null
                  and m.status not in ('rejected', 'archived')
                  and not exists (
                    select 1
                    from memory_items child
                    where child.supersedes_id = m.id
                      and child.deleted_at is null
                  )
                """,
                (list(item_ids),),
            ).fetchall()
        return frozenset(row["id"] for row in rows)

    def list_for_workspace(
        self,
        tenant_id: UUID,
        workspace_id: UUID,
        *,
        layers: tuple[MemoryLayer, ...] = (),
        status: MemoryStatus | None = None,
        label: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[MemoryItem, ...]:
        """List canonical workspace memory in deterministic creation order."""
        with self._connection() as connection:
            self._set_tenant(connection, tenant_id)
            params: list[Any] = [workspace_id]
            layer_filter = ""
            if layers:
                layer_filter = "and m.layer = any(%s)"
                params.append([layer.value for layer in layers])
            status_filter = ""
            if status is not None:
                status_filter = "and m.status = %s"
                params.append(status.value)
            label_filter = ""
            if label:
                label_filter = "and %s = any(m.labels)"
                params.append(label)
            page_filter = ""
            if limit is not None:
                page_filter = "limit %s offset %s"
                params.extend((max(1, limit), max(0, offset)))
            rows = connection.execute(
                f"""
                select {_ITEM_COLUMNS}
                from memory_items m
                join memory_provenance p on p.memory_item_id = m.id
                where m.workspace_id = %s
                  and m.deleted_at is null
                  {layer_filter}
                  {status_filter}
                  {label_filter}
                order by m.created_at, m.id
                {page_filter}
                """,
                params,
            ).fetchall()
            return tuple(self._to_item(row) for row in rows)

    def search(self, query: RecallQuery) -> tuple[Candidate, ...]:
        """Provide a durable lexical fallback until the optional vector index is enabled."""
        query_terms = self._terms(query.text)
        candidates: list[Candidate] = []
        if not self._text_encryption_enabled:
            items = self._search_unencrypted_workspace(query)
        elif self._protected_search_index_is_complete(query):
            items = self._search_protected_workspace(query)
        else:
            items = self.list_for_workspace(
                query.tenant_id,
                query.workspace_id,
                layers=query.layers,
            )
        superseded_ids = {
            item.supersedes_id for item in items if item.supersedes_id is not None
        }
        for item in items:
            if item.id in superseded_ids:
                continue
            if item.status in (MemoryStatus.REJECTED, MemoryStatus.ARCHIVED):
                continue
            if item.scope == MemoryScope.THREAD and item.thread_id != query.thread_id:
                continue
            if item.scope == MemoryScope.PRIVATE and item.agent_id != query.agent_id:
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

    def _protected_search_index_is_complete(self, query: RecallQuery) -> bool:
        """Use token index only after every active row proves index coverage."""
        if self._protected_search_index_mode != "hmac-v1":
            return False
        if not protected_tokens(query.text, self._protected_search_index_key):
            return False
        marker = protected_document_marker(self._protected_search_index_key)
        with self._connection() as connection:
            self._set_tenant(connection, query.tenant_id)
            row = connection.execute(
                """
                select not exists (
                  select 1
                  from memory_items m
                  where m.workspace_id = %s
                    and m.deleted_at is null
                    and not exists (
                      select 1
                      from memory_search_tokens t
                      where t.tenant_id = m.tenant_id
                        and t.workspace_id = m.workspace_id
                        and t.memory_item_id = m.id
                        and t.key_version = %s
                        and t.digest = %s
                    )
                ) as complete
                """,
                (query.workspace_id, self._protected_search_index_key_version, marker),
            ).fetchone()
        return bool(row and row["complete"])

    def _search_protected_workspace(self, query: RecallQuery) -> tuple[MemoryItem, ...]:
        """Bound pgcrypto recall through HMAC terms after coverage verification."""
        digests = list(protected_tokens(query.text, self._protected_search_index_key))
        if not digests:
            return ()
        with self._connection() as connection:
            self._set_tenant(connection, query.tenant_id)
            params: list[Any] = [query.workspace_id]
            layer_filter = ""
            if query.layers:
                layer_filter = "and m.layer = any(%s)"
                params.append([layer.value for layer in query.layers])
            thread_filter = "and m.scope <> 'thread'"
            if query.thread_id is not None:
                thread_filter = "and (m.scope <> 'thread' or m.thread_id = %s)"
                params.append(query.thread_id)
            private_filter = "and m.scope <> 'private'"
            if query.agent_id is not None:
                private_filter = "and (m.scope <> 'private' or m.agent_id = %s)"
                params.append(query.agent_id)
            label_filter = ""
            if query.labels:
                label_filter = "and m.labels @> %s"
                params.append(list(query.labels))
            validity_filter = ""
            if query.valid_at is not None:
                validity_filter = (
                    "and (m.valid_from is null or m.valid_from <= %s) "
                    "and (m.valid_to is null or m.valid_to >= %s)"
                )
                params.extend((query.valid_at, query.valid_at))
            params.extend(
                (
                    self._protected_search_index_key_version,
                    digests,
                    max(1, query.top_k * 3),
                )
            )
            rows = connection.execute(
                f"""
                select {_ITEM_COLUMNS}
                from memory_items m
                join memory_provenance p on p.memory_item_id = m.id
                where m.workspace_id = %s
                  and m.deleted_at is null
                  and m.status not in ('rejected', 'archived')
                  and not exists (
                    select 1 from memory_items child
                    where child.supersedes_id = m.id and child.deleted_at is null
                  )
                  {layer_filter}
                  {thread_filter}
                  {private_filter}
                  {label_filter}
                  {validity_filter}
                  and (
                    m.layer in ('core', 'working')
                    or exists (
                      select 1 from memory_search_tokens t
                      where t.tenant_id = m.tenant_id
                        and t.workspace_id = m.workspace_id
                        and t.memory_item_id = m.id
                        and t.key_version = %s
                        and t.digest = any(%s)
                    )
                  )
                order by m.created_at desc, m.id desc
                limit %s
                """,
                params,
            ).fetchall()
        return tuple(self._to_item(row) for row in rows)

    def _search_unencrypted_workspace(self, query: RecallQuery) -> tuple[MemoryItem, ...]:
        """Use PostgreSQL FTS to bound plaintext lexical candidates before decoding.

        When pgcrypto text encryption is enabled, PostgreSQL only sees
        ciphertext and this path would be both incorrect and non-indexable.
        That protected mode deliberately keeps the existing post-decryption
        fallback until a separately designed protected search index exists.
        """
        with self._connection() as connection:
            self._set_tenant(connection, query.tenant_id)
            params: list[Any] = [query.workspace_id]
            layer_filter = ""
            if query.layers:
                layer_filter = "and m.layer = any(%s)"
                params.append([layer.value for layer in query.layers])
            thread_filter = "and m.scope <> 'thread'"
            if query.thread_id is not None:
                thread_filter = "and (m.scope <> 'thread' or m.thread_id = %s)"
                params.append(query.thread_id)
            private_filter = "and m.scope <> 'private'"
            if query.agent_id is not None:
                private_filter = "and (m.scope <> 'private' or m.agent_id = %s)"
                params.append(query.agent_id)
            label_filter = ""
            if query.labels:
                label_filter = "and m.labels @> %s"
                params.append(list(query.labels))
            validity_filter = ""
            if query.valid_at is not None:
                validity_filter = (
                    "and (m.valid_from is null or m.valid_from <= %s) "
                    "and (m.valid_to is null or m.valid_to >= %s)"
                )
                params.extend((query.valid_at, query.valid_at))
            # Keep core/working memory available for compact system context
            # even when a lexical query has no term overlap.
            params.extend((query.text, query.text, max(1, query.top_k * 3)))
            rows = connection.execute(
                f"""
                select {_ITEM_COLUMNS}
                from memory_items m
                join memory_provenance p on p.memory_item_id = m.id
                where m.workspace_id = %s
                  and m.deleted_at is null
                  and m.status not in ('rejected', 'archived')
                  and not exists (
                    select 1 from memory_items child
                    where child.supersedes_id = m.id and child.deleted_at is null
                  )
                  {layer_filter}
                  {thread_filter}
                  {private_filter}
                  {label_filter}
                  {validity_filter}
                  and (
                    to_tsvector('simple', m.text) @@ plainto_tsquery('simple', %s)
                    or m.layer in ('core', 'working')
                  )
                order by
                  (to_tsvector('simple', m.text) @@ plainto_tsquery('simple', %s)) desc,
                  m.created_at desc, m.id desc
                limit %s
                """,
                params,
            ).fetchall()
        return tuple(self._to_item(row) for row in rows)

    def save(
        self, observation: Observation, audit_event: AuditEvent | None = None
    ) -> Observation:
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
                    self._stored_sensitive_text(connection, observation.summary),
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
            if audit_event is not None:
                self._insert_audit_event(connection, audit_event)
        return observation

    def save_conflict_review(
        self, decision: ConflictReviewDecision, audit_event: AuditEvent | None = None
    ) -> ConflictReviewDecision:
        """Create or replace a persisted human decision for one conflict case."""
        with self._connection() as connection:
            self._set_tenant(connection, decision.tenant_id)
            self._upsert_conflict_review(connection, decision)
            if audit_event is not None:
                self._insert_audit_event(connection, audit_event)
        return decision

    def apply_conflict_resolution(
        self,
        decision: ConflictReviewDecision,
        writes: tuple[tuple[MemoryItem, IntegrationEvent, int], ...],
        audit_event: AuditEvent | None = None,
    ) -> ConflictReviewDecision:
        """Atomically apply all winner/loser revisions, outbox events and review."""
        with self._connection() as connection:
            self._set_tenant(connection, decision.tenant_id)
            connection.execute(
                "select pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"{decision.tenant_id}:conflict:{decision.case_id}",),
            )
            existing = connection.execute(
                """
                select status, winner_value, applied_memory_id
                from conflict_reviews
                where case_id = %s
                for update
                """,
                (decision.case_id,),
            ).fetchone()
            if (
                existing is not None
                and existing["status"] == decision.status.value
                and existing["winner_value"] == decision.winner_value
                and existing["applied_memory_id"] is not None
            ):
                return replace(decision, applied_memory_id=existing["applied_memory_id"])
            if existing is not None and existing["applied_memory_id"] is not None:
                raise ValueError("conflict resolution is already applied and immutable")
            for item, event, expected_revision in writes:
                if (
                    item.tenant_id != decision.tenant_id
                    or item.workspace_id != decision.workspace_id
                ):
                    raise ValueError("conflict resolution write crosses decision scope")
                self._append_conflict_resolution_write(
                    connection,
                    item,
                    event,
                    expected_revision,
                )
            self._upsert_conflict_review(connection, decision)
            if audit_event is not None:
                self._insert_audit_event(connection, audit_event)
        return decision

    def list_conflict_reviews(
        self, tenant_id: UUID, workspace_id: UUID
    ) -> tuple[ConflictReviewDecision, ...]:
        """List conflict-review decisions under RLS."""
        with self._connection() as connection:
            self._set_tenant(connection, tenant_id)
            rows = connection.execute(
                """
                select tenant_id, workspace_id, case_id, status, winner_value, reason,
                       applied_memory_id, updated_at
                from conflict_reviews
                where workspace_id = %s
                order by updated_at, case_id
                """,
                (workspace_id,),
            ).fetchall()
        return tuple(
            ConflictReviewDecision(
                tenant_id=row["tenant_id"],
                workspace_id=row["workspace_id"],
                case_id=row["case_id"],
                status=ConflictReviewStatus(row["status"]),
                winner_value=row["winner_value"],
                reason=row["reason"],
                applied_memory_id=row["applied_memory_id"],
                updated_at=row["updated_at"],
            )
            for row in rows
        )

    def save_edge(self, edge: MemoryEdge, audit_event: AuditEvent | None = None) -> MemoryEdge:
        """Persist one graph edge under RLS."""
        with self._connection() as connection:
            self._set_tenant(connection, edge.tenant_id)
            connection.execute(
                """
                insert into memory_edges (
                  id, tenant_id, workspace_id, src_id, dst_id, edge_type, weight,
                  valid_from, valid_to, provenance_item_id, created_at
                ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (id) do nothing
                """,
                (
                    edge.id,
                    edge.tenant_id,
                    edge.workspace_id,
                    edge.src_id,
                    edge.dst_id,
                    edge.edge_type.value,
                    edge.weight,
                    edge.valid_from,
                    edge.valid_to,
                    edge.provenance_item_id,
                    edge.created_at,
                ),
            )
            if audit_event is not None:
                self._insert_audit_event(connection, audit_event)
        return edge

    def list_neighbors(
        self,
        tenant_id: UUID,
        workspace_id: UUID,
        item_id: UUID,
        *,
        edge_type: MemoryEdgeType | None = None,
    ) -> tuple[MemoryEdge, ...]:
        """List incoming/outgoing graph edges under RLS."""
        with self._connection() as connection:
            self._set_tenant(connection, tenant_id)
            rows = connection.execute(
                """
                select id, tenant_id, workspace_id, src_id, dst_id, edge_type, weight,
                  valid_from, valid_to, provenance_item_id, created_at
                from memory_edges
                where workspace_id = %s
                  and (src_id = %s or dst_id = %s)
                  and (%s::text is null or edge_type = %s::text)
                order by created_at, id
                """,
                (
                    workspace_id,
                    item_id,
                    item_id,
                    edge_type.value if edge_type else None,
                    edge_type.value if edge_type else None,
                ),
            ).fetchall()
        return tuple(
            MemoryEdge(
                id=row["id"],
                tenant_id=row["tenant_id"],
                workspace_id=row["workspace_id"],
                src_id=row["src_id"],
                dst_id=row["dst_id"],
                edge_type=MemoryEdgeType(row["edge_type"]),
                weight=row["weight"],
                valid_from=row["valid_from"],
                valid_to=row["valid_to"],
                provenance_item_id=row["provenance_item_id"],
                created_at=row["created_at"],
            )
            for row in rows
        )

    def list_edges_for_workspace(
        self, tenant_id: UUID, workspace_id: UUID
    ) -> tuple[MemoryEdge, ...]:
        """List all graph edges for one workspace under RLS."""
        with self._connection() as connection:
            self._set_tenant(connection, tenant_id)
            rows = connection.execute(
                """
                select id, tenant_id, workspace_id, src_id, dst_id, edge_type, weight,
                  valid_from, valid_to, provenance_item_id, created_at
                from memory_edges
                where workspace_id = %s
                order by created_at, id
                """,
                (workspace_id,),
            ).fetchall()
        return tuple(
            MemoryEdge(
                id=row["id"],
                tenant_id=row["tenant_id"],
                workspace_id=row["workspace_id"],
                src_id=row["src_id"],
                dst_id=row["dst_id"],
                edge_type=MemoryEdgeType(row["edge_type"]),
                weight=row["weight"],
                valid_from=row["valid_from"],
                valid_to=row["valid_to"],
                provenance_item_id=row["provenance_item_id"],
                created_at=row["created_at"],
            )
            for row in rows
        )

    def list_observations(
        self, tenant_id: UUID, workspace_id: UUID
    ) -> tuple[Observation, ...]:
        """List derived observations with their evidence under RLS."""
        with self._connection() as connection:
            self._set_tenant(connection, tenant_id)
            rows = connection.execute(
                f"""
                select
                  o.id, o.tenant_id, o.workspace_id,
                  {_OBSERVATION_SUMMARY_SQL} as summary, o.confidence,
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
        """Lease a bounded pooled transaction with dictionary-shaped rows."""
        try:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool
        except ImportError as error:
            raise RuntimeError(
                'PostgreSQL support is not installed; run pip install -e ".[postgres]"'
            ) from error

        if self._pool is None:
            self._pool = ConnectionPool(
                self.dsn,
                min_size=max(1, int(os.getenv("UAM_POSTGRES_POOL_MIN_SIZE", "1"))),
                max_size=max(1, int(os.getenv("UAM_POSTGRES_POOL_MAX_SIZE", "10"))),
                timeout=float(os.getenv("UAM_POSTGRES_POOL_TIMEOUT_SECONDS", "30")),
                kwargs={"row_factory": dict_row},
                open=True,
            )
        with self._pool.connection() as connection:
            if self._text_encryption_key:
                connection.execute(
                    "select set_config('app.memory_text_encryption_key', %s, true)",
                    (self._text_encryption_key,),
                )
            yield connection

    def close(self) -> None:
        """Release pooled database connections during orderly process shutdown."""
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    @property
    def _text_encryption_enabled(self) -> bool:
        return self._text_encryption_mode == "pgcrypto"

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

    def _get_turn_by_idempotency_key(
        self, connection: Any, tenant_id: UUID, key: str
    ) -> ConversationTurn | None:
        row = connection.execute(
            f"""
            select
              t.id, t.tenant_id, t.workspace_id, t.thread_id, t.agent_id,
              t.namespace, t.source_kind, t.retention_policy,
              {_TURN_METADATA_SQL} as metadata,
              t.raw_content_state, t.created_at, t.expires_at,
              coalesce(
                jsonb_agg(
                  jsonb_build_object(
                    'role', m.role,
                    'content', {_CONVERSATION_CONTENT_SQL},
                    'name', m.name,
                    'metadata', {_MESSAGE_METADATA_SQL}
                  )
                  order by m.position
                ) filter (where m.id is not null),
                '[]'::jsonb
              ) as messages
            from conversation_idempotency_keys i
            join conversation_turns t on t.id = i.turn_id
            left join conversation_messages m on m.turn_id = t.id
            where i.tenant_id = %s and i.key = %s
            group by t.id
            """,
            (tenant_id, key),
        ).fetchone()
        return None if row is None else self._to_turn(row)

    def _get_proposal_by_idempotency_key(
        self, connection: Any, tenant_id: UUID, key: str
    ) -> MemoryProposal | None:
        row = connection.execute(
            f"""
            select p.id, p.tenant_id, p.workspace_id, p.agent_id, p.thread_id,
              p.namespace, p.requester, p.target,
              {_PROPOSAL_TEXT_SQL} as proposal, {_PROPOSAL_EVIDENCE_SQL} as evidence,
              p.confidence, p.importance, p.status,
              {_PROPOSAL_METADATA_SQL} as metadata, p.created_at,
              p.reviewed_at, p.reviewer, p.review_reason
            from memory_proposal_idempotency_keys i
            join memory_proposals p on p.id = i.proposal_id
            where i.tenant_id = %s and i.key = %s
            """,
            (tenant_id, key),
        ).fetchone()
        return None if row is None else self._to_proposal(row)

    def _upsert_conflict_review(
        self,
        connection: Any,
        decision: ConflictReviewDecision,
    ) -> None:
        connection.execute(
            """
            insert into conflict_reviews (
              tenant_id, workspace_id, case_id, status, winner_value, reason,
              applied_memory_id, updated_at
            ) values (%s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (tenant_id, case_id) do update set
              workspace_id = excluded.workspace_id,
              status = excluded.status,
              winner_value = excluded.winner_value,
              reason = excluded.reason,
              applied_memory_id = excluded.applied_memory_id,
              updated_at = excluded.updated_at
            """,
            (
                decision.tenant_id,
                decision.workspace_id,
                decision.case_id,
                decision.status.value,
                decision.winner_value,
                decision.reason,
                decision.applied_memory_id,
                decision.updated_at,
            ),
        )

    def _append_conflict_resolution_write(
        self,
        connection: Any,
        item: MemoryItem,
        event: IntegrationEvent,
        expected_revision: int,
    ) -> None:
        if item.supersedes_id is None:
            raise ValueError("conflict resolution replacement must declare supersedes_id")
        self._validate_event(item, event)
        parent = connection.execute(
            """
            select id, revision
            from memory_items
            where id = %s and deleted_at is null
            """,
            (item.supersedes_id,),
        ).fetchone()
        if parent is None:
            raise KeyError("conflict evidence memory not found")
        head = connection.execute(
            """
            with recursive chain as (
              select id, revision
              from memory_items
              where id = %s and deleted_at is null
              union all
              select child.id, child.revision
              from memory_items child
              join chain parent on child.supersedes_id = parent.id
              where child.deleted_at is null
            )
            select id, revision
            from chain
            order by revision desc, id desc
            limit 1
            """,
            (item.supersedes_id,),
        ).fetchone()
        actual = head["revision"] if head is not None else parent["revision"]
        if head is not None and (
            head["id"] != item.supersedes_id or parent["revision"] != expected_revision
        ):
            raise MemoryRevisionConflictError(
                item.supersedes_id,
                expected_revision,
                actual,
            )
        self._insert_item(connection, item)
        self._insert_event(connection, event)

    def _insert_item(self, connection: Any, item: MemoryItem) -> None:
        from psycopg.types.json import Jsonb

        stored_text = self._stored_memory_text(connection, item)
        connection.execute(
            """
            insert into memory_items (
              id, tenant_id, workspace_id, agent_id, thread_id, layer, scope,
              kind, text, labels, metadata, status, importance, salience, confidence,
              observed_at, valid_from, valid_to, revision, supersedes_id, created_at
            ) values (
              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
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
                stored_text,
                list(item.labels),
                Jsonb(self._stored_sensitive_json(connection, item.metadata)),
                item.status.value,
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
        self._insert_protected_search_tokens(connection, item)
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
                self._stored_sensitive_text(connection, provenance.quote),
                provenance.extraction_version,
            ),
        )

    def _insert_protected_search_tokens(self, connection: Any, item: MemoryItem) -> None:
        """Dual-write blind-index terms in the canonical item transaction."""
        if self._protected_search_index_mode != "hmac-v1":
            return
        for digest in protected_index_digests(item.text, self._protected_search_index_key):
            connection.execute(
                """
                insert into memory_search_tokens (
                  tenant_id, workspace_id, memory_item_id, key_version, digest
                ) values (%s, %s, %s, %s, %s)
                on conflict do nothing
                """,
                (
                    item.tenant_id,
                    item.workspace_id,
                    item.id,
                    self._protected_search_index_key_version,
                    digest,
                ),
            )

    def _stored_memory_text(self, connection: Any, item: MemoryItem) -> str:
        """Return plaintext or pgcrypto ciphertext for memory_items.text."""
        if not self._should_encrypt_item(item):
            return item.text
        stored = self._stored_sensitive_text(connection, item.text)
        if stored is None:
            raise RuntimeError("pgcrypto did not return encrypted memory text")
        return stored

    def _stored_sensitive_text(self, connection: Any, value: str | None) -> str | None:
        """Encrypt a non-indexed sensitive value when pgcrypto is enabled."""
        if value is None:
            return None
        if not self._text_encryption_enabled:
            return value
        row = connection.execute(
            """
            select %s || encode(
              pgp_sym_encrypt(%s, %s, 'cipher-algo=aes256,compress-algo=0'),
              'base64'
            ) as encrypted_text
            """,
            (_PGCRYPTO_TEXT_PREFIX, value, self._text_encryption_key),
        ).fetchone()
        if row is None:
            raise RuntimeError("pgcrypto did not return encrypted memory text")
        return str(row["encrypted_text"])

    def _stored_sensitive_json(
        self, connection: Any, value: dict[str, Any]
    ) -> dict[str, Any]:
        """Encrypt a JSON payload while keeping its JSONB column type stable."""
        if not self._text_encryption_enabled:
            return value
        plaintext = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        ciphertext = self._stored_sensitive_text(connection, plaintext)
        if ciphertext is None:
            raise RuntimeError("pgcrypto did not return encrypted JSON")
        return {_PGCRYPTO_JSON_KEY: ciphertext}

    def _should_encrypt_item(self, item: MemoryItem) -> bool:
        """Return whether canonical text for this item must be encrypted at rest."""
        if not self._text_encryption_enabled:
            return False
        return self._text_encryption_scopes is None or item.scope in self._text_encryption_scopes

    def _insert_event(self, connection: Any, event: IntegrationEvent) -> None:
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
                Jsonb(self._stored_sensitive_json(connection, event.payload)),
                event.correlation_id,
                event.occurred_at,
            ),
        )

    @staticmethod
    def _validate_turn_event(turn: ConversationTurn, event: IntegrationEvent) -> None:
        """Forbid an outbox event from escaping the appended turn's scope."""
        if event.tenant_id != turn.tenant_id or event.workspace_id != turn.workspace_id:
            raise ValueError("conversation outbox event scope must match the turn")
        if event.correlation_id != turn.id:
            raise ValueError("conversation outbox event correlation must be the turn ID")

    def _insert_audit_event(self, connection: Any, event: AuditEvent) -> None:
        """Write audit evidence inside an existing canonical transaction."""
        from psycopg.types.json import Jsonb
        connection.execute(
            """insert into audit_events (id, tenant_id, workspace_id, action, actor, actor_type,
            resource_type, resource_id, status, metadata, created_at)
            values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (event.id,event.tenant_id,event.workspace_id,event.action,event.actor,event.actor_type,
             event.resource_type,event.resource_id,event.status,
             Jsonb(self._stored_sensitive_json(connection,event.metadata)),event.created_at),
        )

    def _insert_turn(self, connection: Any, turn: ConversationTurn) -> None:
        from psycopg.types.json import Jsonb

        connection.execute(
            """
            insert into conversation_turns (
              id, tenant_id, workspace_id, thread_id, agent_id, namespace,
              source_kind, retention_policy, metadata, created_at, expires_at
            ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                turn.id,
                turn.tenant_id,
                turn.workspace_id,
                turn.thread_id,
                turn.agent_id,
                turn.namespace,
                turn.source_kind,
                turn.retention_policy.value,
                Jsonb(self._stored_sensitive_json(connection, turn.metadata)),
                turn.created_at,
                turn.expires_at,
            ),
        )
        for position, message in enumerate(turn.messages):
            connection.execute(
                """
                insert into conversation_messages (
                  tenant_id, turn_id, position, role, name, content, metadata
                ) values (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    turn.tenant_id,
                    turn.id,
                    position,
                    message.role,
                    message.name,
                    self._stored_sensitive_text(connection, message.content),
                    Jsonb(self._stored_sensitive_json(connection, message.metadata)),
                ),
            )

    def _insert_proposal(self, connection: Any, proposal: MemoryProposal) -> None:
        from psycopg.types.json import Jsonb

        connection.execute(
            """
            insert into memory_proposals (
              id, tenant_id, workspace_id, agent_id, thread_id, namespace,
              requester, target, proposal, evidence, confidence, importance,
              status, metadata, created_at, reviewed_at, reviewer, review_reason
            ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                proposal.id,
                proposal.tenant_id,
                proposal.workspace_id,
                proposal.agent_id,
                proposal.thread_id,
                proposal.namespace,
                proposal.requester,
                proposal.target.value,
                self._stored_sensitive_text(connection, proposal.proposal),
                self._stored_sensitive_text(connection, proposal.evidence),
                proposal.confidence,
                proposal.importance,
                proposal.status.value,
                Jsonb(self._stored_sensitive_json(connection, proposal.metadata)),
                proposal.created_at,
                proposal.reviewed_at,
                proposal.reviewer,
                proposal.review_reason,
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
            status=MemoryStatus(row["status"]),
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
    def _to_turn(row: dict[str, Any]) -> ConversationTurn:
        metadata = dict(row["metadata"])
        raw_content_state = str(row.get("raw_content_state") or "active")
        if raw_content_state != "active":
            metadata["retention"] = {"raw_content": raw_content_state}
        return ConversationTurn(
            id=row["id"],
            tenant_id=row["tenant_id"],
            workspace_id=row["workspace_id"],
            thread_id=row["thread_id"],
            agent_id=row["agent_id"],
            namespace=row["namespace"],
            source_kind=row["source_kind"],
            retention_policy=ConversationRetentionPolicy(row["retention_policy"]),
            metadata=metadata,
            created_at=row["created_at"],
            expires_at=row.get("expires_at"),
            messages=tuple(
                ConversationMessage(
                    role=str(message.get("role") or ""),
                    name=message.get("name"),
                    content=str(message.get("content") or ""),
                    metadata=dict(message.get("metadata") or {}),
                )
                for message in row["messages"]
            ),
        )

    @staticmethod
    def _to_proposal(row: dict[str, Any]) -> MemoryProposal:
        return MemoryProposal(
            id=row["id"],
            tenant_id=row["tenant_id"],
            workspace_id=row["workspace_id"],
            agent_id=row["agent_id"],
            thread_id=row["thread_id"],
            namespace=row["namespace"],
            requester=row["requester"],
            target=MemoryProposalTarget(row["target"]),
            proposal=row["proposal"],
            evidence=row["evidence"] or "",
            confidence=row["confidence"],
            importance=row["importance"],
            status=MemoryProposalStatus(row["status"]),
            metadata=row["metadata"],
            created_at=row["created_at"],
            reviewed_at=row["reviewed_at"],
            reviewer=row["reviewer"],
            review_reason=row["review_reason"] or "",
        )

    @staticmethod
    def _to_audit_event(row: dict[str, Any]) -> AuditEvent:
        return AuditEvent(
            id=row["id"],
            tenant_id=row["tenant_id"],
            workspace_id=row["workspace_id"],
            action=row["action"],
            actor=row["actor"],
            actor_type=row["actor_type"],
            resource_type=row["resource_type"],
            resource_id=row["resource_id"],
            status=row["status"],
            metadata=row["metadata"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _to_api_key_record(row: dict[str, Any]) -> ApiKeyRecord:
        return ApiKeyRecord(
            id=row["id"],
            tenant_id=row["tenant_id"],
            name=row["name"],
            secret_fingerprint=row["secret_fingerprint"],
            scopes=tuple(row["scopes"]),
            created_at=row["created_at"],
            last_used_at=row["last_used_at"],
            revoked_at=row["revoked_at"],
            revoked_reason=row["revoked_reason"] or "",
        )

    @staticmethod
    def _terms(text: str) -> set[str]:
        return {match.group(0).casefold() for match in _WORD.finditer(text)}


class PostgresObservationRepository:
    """Observation-port view over the shared PostgreSQL store."""

    def __init__(self, store: PostgresMemoryLedger) -> None:
        self._store = store

    def save(self, observation: Observation, audit_event: AuditEvent | None = None) -> Observation:
        return self._store.save(observation, audit_event=audit_event)

    def list_for_workspace(
        self, tenant_id: UUID, workspace_id: UUID, *, limit: int | None = None, offset: int = 0
    ) -> tuple[Observation, ...]:
        return self._store.list_observations(tenant_id, workspace_id)


class PostgresConflictReviewRepository:
    """Conflict-review port view over the shared PostgreSQL ledger."""

    def __init__(self, store: PostgresMemoryLedger) -> None:
        """Retain shared connection configuration."""
        self._store = store

    def save(
        self, decision: ConflictReviewDecision, audit_event: AuditEvent | None = None
    ) -> ConflictReviewDecision:
        """Delegate decision persistence."""
        return self._store.save_conflict_review(decision, audit_event=audit_event)

    def apply_resolution(
        self,
        decision: ConflictReviewDecision,
        writes: tuple[tuple[MemoryItem, IntegrationEvent, int], ...],
        audit_event: AuditEvent | None = None,
    ) -> ConflictReviewDecision:
        """Delegate atomic canonical conflict resolution."""
        return self._store.apply_conflict_resolution(decision, writes, audit_event=audit_event)

    def list_for_workspace(
        self, tenant_id: UUID, workspace_id: UUID
    ) -> tuple[ConflictReviewDecision, ...]:
        """Delegate tenant-safe review listing."""
        return self._store.list_conflict_reviews(tenant_id, workspace_id)


class PostgresGraphRepository:
    """Graph port view over the shared PostgreSQL ledger."""

    def __init__(self, store: PostgresMemoryLedger) -> None:
        """Retain shared connection configuration."""
        self._store = store

    def save_edge(self, edge: MemoryEdge, audit_event: AuditEvent | None = None) -> MemoryEdge:
        """Delegate edge persistence."""
        return self._store.save_edge(edge, audit_event=audit_event)

    def list_neighbors(
        self,
        tenant_id: UUID,
        workspace_id: UUID,
        item_id: UUID,
        *,
        edge_type: MemoryEdgeType | None = None,
    ) -> tuple[MemoryEdge, ...]:
        """Delegate neighbor lookup."""
        return self._store.list_neighbors(
            tenant_id,
            workspace_id,
            item_id,
            edge_type=edge_type,
        )

    def list_for_workspace(
        self, tenant_id: UUID, workspace_id: UUID
    ) -> tuple[MemoryEdge, ...]:
        """Delegate workspace edge listing under the current tenant scope."""
        return self._store.list_edges_for_workspace(tenant_id, workspace_id)


class PostgresCheckpointStore:
    """CAS-protected checkpoint storage backed by the existing checkpoints table."""

    def __init__(self, ledger: PostgresMemoryLedger) -> None:
        self._ledger = ledger

    def save(self, checkpoint: Checkpoint, audit_event: AuditEvent | None = None) -> Checkpoint:
        """Append a new checkpoint revision unconditionally."""
        from psycopg.types.json import Jsonb

        with self._ledger._connection() as connection:
            self._ledger._set_tenant(connection, checkpoint.tenant_id)
            connection.execute(
                """
                insert into checkpoints (
                  id, tenant_id, workspace_id, thread_id, revision, state, created_at
                ) values (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    checkpoint.id,
                    checkpoint.tenant_id,
                    checkpoint.workspace_id,
                    checkpoint.thread_id,
                    checkpoint.revision,
                    Jsonb(
                        self._ledger._stored_sensitive_json(connection, checkpoint.state)
                    ),
                    checkpoint.created_at,
                ),
            )
            if audit_event is not None:
                self._ledger._insert_audit_event(connection, audit_event)
        return checkpoint

    def save_if_head(
        self,
        checkpoint: Checkpoint,
        expected_revision: int,
        audit_event: AuditEvent | None = None,
    ) -> Checkpoint:
        """CAS: append only when current head revision equals *expected_revision*."""
        from psycopg.types.json import Jsonb

        with self._ledger._connection() as connection:
            self._ledger._set_tenant(connection, checkpoint.tenant_id)
            connection.execute(
                "select pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"{checkpoint.tenant_id}:{checkpoint.thread_id}",),
            )
            row = connection.execute(
                """
                select revision as head
                from checkpoints
                where thread_id = %s
                order by revision desc
                limit 1
                """,
                (checkpoint.thread_id,),
            ).fetchone()
            actual = row["head"] if row and row["head"] is not None else 0
            if actual != expected_revision:
                raise StaleRevisionError(
                    checkpoint.thread_id, expected_revision, actual
                )
            connection.execute(
                """
                insert into checkpoints (
                  id, tenant_id, workspace_id, thread_id, revision, state, created_at
                ) values (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    checkpoint.id,
                    checkpoint.tenant_id,
                    checkpoint.workspace_id,
                    checkpoint.thread_id,
                    checkpoint.revision,
                    Jsonb(
                        self._ledger._stored_sensitive_json(connection, checkpoint.state)
                    ),
                    checkpoint.created_at,
                ),
            )
            if audit_event is not None:
                self._ledger._insert_audit_event(connection, audit_event)
        return checkpoint

    def get_head(
        self, tenant_id: UUID, thread_id: UUID
    ) -> Checkpoint | None:
        """Return the latest revision for a thread."""
        with self._ledger._connection() as connection:
            self._ledger._set_tenant(connection, tenant_id)
            row = connection.execute(
                f"""
                select id, tenant_id, workspace_id, thread_id,
                       revision, {_CHECKPOINT_STATE_SQL} as state, created_at
                from checkpoints
                where thread_id = %s
                order by revision desc
                limit 1
                """,
                (thread_id,),
            ).fetchone()
        return None if row is None else self._to_checkpoint(row)

    def get_revision(
        self, tenant_id: UUID, thread_id: UUID, revision: int
    ) -> Checkpoint | None:
        """Return a specific historical revision."""
        with self._ledger._connection() as connection:
            self._ledger._set_tenant(connection, tenant_id)
            row = connection.execute(
                f"""
                select id, tenant_id, workspace_id, thread_id,
                       revision, {_CHECKPOINT_STATE_SQL} as state, created_at
                from checkpoints
                where thread_id = %s and revision = %s
                """,
                (thread_id, revision),
            ).fetchone()
        return None if row is None else self._to_checkpoint(row)

    def list_for_workspace(
        self, tenant_id: UUID, workspace_id: UUID, *, limit: int | None = None, offset: int = 0
    ) -> tuple[Checkpoint, ...]:
        """List head checkpoints for every thread in a workspace."""
        with self._ledger._connection() as connection:
            self._ledger._set_tenant(connection, tenant_id)
            rows = connection.execute(
                f"""
                with heads as (
                  select distinct on (thread_id)
                       id, tenant_id, workspace_id, thread_id,
                       revision, {_CHECKPOINT_STATE_SQL} as state, created_at
                  from checkpoints
                  where workspace_id = %s
                  order by thread_id, revision desc
                )
                select * from heads
                order by created_at, id
                limit %s offset %s
                """,
                (workspace_id, limit if limit is not None else 2147483647, max(0, offset)),
            ).fetchall()
        return tuple(self._to_checkpoint(row) for row in rows)

    def compact(
        self,
        tenant_id: UUID,
        thread_id: UUID,
        *,
        keep_last: int = 3,
        audit_event: AuditEvent | None = None,
    ) -> int:
        """Delete old revisions keeping the most recent *keep_last*."""
        with self._ledger._connection() as connection:
            self._ledger._set_tenant(connection, tenant_id)
            row = connection.execute(
                """
                with deletable as (
                  select id
                  from checkpoints
                  where thread_id = %s
                  order by revision desc
                  offset %s
                )
                delete from checkpoints
                where id in (select id from deletable)
                returning id
                """,
                (thread_id, keep_last),
            ).fetchall()
            if audit_event is not None:
                self._ledger._insert_audit_event(connection, audit_event)
        return len(row)

    @staticmethod
    def _to_checkpoint(row: dict[str, Any]) -> Checkpoint:
        return Checkpoint(
            id=row["id"],
            tenant_id=row["tenant_id"],
            workspace_id=row["workspace_id"],
            thread_id=row["thread_id"],
            revision=row["revision"],
            state=row["state"],
            created_at=row["created_at"],
        )
