from __future__ import annotations

import importlib.util
import os
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from unittest.mock import patch
from uuid import uuid4

from memory_plane.adapters.postgres import (
    PostgresCheckpointStore,
    PostgresConflictReviewRepository,
    PostgresMemoryLedger,
)
from memory_plane.contracts.dto import RecallQuery
from memory_plane.contracts.events import ConsumerClaim, IntegrationEvent
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
from memory_plane.domain.identity import AgentIdentity
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
from memory_plane.services.conflicts import ConflictService
from memory_plane.services.conversations import (
    AppendConversationTurnCommand,
    ConversationCurator,
    ConversationService,
    CurateConversationTurnCommand,
)
from memory_plane.services.proposals import (
    MemoryProposalService,
    ReviewMemoryProposalCommand,
    SubmitMemoryProposalCommand,
)
from memory_plane.services.retention import RetentionService

ROOT = Path(__file__).resolve().parents[2]


def _load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


protected_search_backfill = _load_script("backfill_protected_search_tokens")
BackfillCursor = protected_search_backfill.BackfillCursor
backfill_workspace = protected_search_backfill.backfill_workspace
protected_search_index_probe = _load_script("protected_search_index_probe")
capture_protected_search_probe = protected_search_index_probe.capture_probe

DATABASE_URL = os.getenv("UAM_TEST_DATABASE_URL")
if DATABASE_URL:
    from psycopg import Error as PostgresError
else:
    PostgresError = RuntimeError


@unittest.skipUnless(DATABASE_URL, "set UAM_TEST_DATABASE_URL to run PostgreSQL tests")
class PostgresMemoryLedgerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        store = PostgresMemoryLedger(DATABASE_URL or "")
        with store._connection() as connection:
            row = connection.execute(
                """
                select r.rolsuper
                from pg_roles r
                where r.rolname = current_user
                """
            ).fetchone()
        if row is None or row["rolsuper"]:
            raise RuntimeError("integration tests must use a non-superuser PostgreSQL role")

    def setUp(self) -> None:
        self.store = PostgresMemoryLedger(DATABASE_URL or "")
        self.tenant = uuid4()
        self.workspace = uuid4()
        self._seed_scope()

    def _seed_scope(self) -> None:
        with self.store._connection() as connection:
            connection.execute(
                "insert into tenants (id, slug) values (%s, %s)",
                (self.tenant, f"tenant-{self.tenant}"),
            )
            self.store._set_tenant(connection, self.tenant)
            connection.execute(
                "insert into workspaces (id, tenant_id, name) values (%s, %s, %s)",
                (self.workspace, self.tenant, "integration"),
            )

    def _item(self, text: str = "PostgreSQL remembers this") -> MemoryItem:
        return MemoryItem(
            tenant_id=self.tenant,
            workspace_id=self.workspace,
            layer=MemoryLayer.SEMANTIC,
            scope=MemoryScope.WORKSPACE,
            kind="fact",
            text=text,
            labels=("postgres",),
            metadata={"source": "integration-test"},
            provenance=Provenance(
                source_kind="test",
                origin_uri="test://postgres",
                quote=text,
            ),
        )

    @staticmethod
    def _event(item: MemoryItem, *, event_id=None) -> IntegrationEvent:
        values = {
            "name": "memory.retained.v1",
            "tenant_id": item.tenant_id,
            "workspace_id": item.workspace_id,
            "correlation_id": item.id,
            "payload": {"memory_id": str(item.id), "jobs": ["embed"]},
        }
        if event_id is not None:
            values["id"] = event_id
        return IntegrationEvent(**values)

    def test_workspace_operation_lock_serializes_independent_postgres_ledgers(self) -> None:
        other = PostgresMemoryLedger(DATABASE_URL or "")
        first_entered = threading.Event()
        release_first = threading.Event()
        second_entered = threading.Event()
        errors: list[BaseException] = []

        def first_worker() -> None:
            try:
                with self.store.workspace_operation_lock(
                    self.tenant, self.workspace, "embedding-reindex"
                ):
                    first_entered.set()
                    if not release_first.wait(timeout=3):
                        raise TimeoutError("test did not release first workspace lock")
            except BaseException as exc:  # noqa: BLE001 - return worker failure to test thread.
                errors.append(exc)

        def second_worker() -> None:
            try:
                if not first_entered.wait(timeout=3):
                    raise TimeoutError("first workspace lock was not acquired")
                with other.workspace_operation_lock(
                    self.tenant, self.workspace, "embedding-reindex"
                ):
                    second_entered.set()
            except BaseException as exc:  # noqa: BLE001 - return worker failure to test thread.
                errors.append(exc)

        first_thread = threading.Thread(target=first_worker)
        second_thread = threading.Thread(target=second_worker)
        first_thread.start()
        second_thread.start()
        self.assertTrue(first_entered.wait(timeout=3))
        time.sleep(0.15)
        self.assertFalse(second_entered.is_set())
        release_first.set()
        first_thread.join(timeout=3)
        second_thread.join(timeout=3)
        other.close()

        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        self.assertEqual([], errors)
        self.assertTrue(second_entered.is_set())

    def test_retain_round_trip_is_idempotent_and_queues_once(self) -> None:
        item = self._item()
        event = self._event(item)

        first, first_created = self.store.retain(item, event, "request-1")
        retry_item = self._item("retry body must be ignored")
        retry, retry_created = self.store.retain(
            retry_item,
            self._event(retry_item),
            "request-1",
        )

        self.assertTrue(first_created)
        self.assertFalse(retry_created)
        self.assertEqual(first.id, retry.id)
        self.assertEqual(item, self.store.get(self.tenant, item.id))
        with self.store._connection() as connection:
            self.store._set_tenant(connection, self.tenant)
            count = connection.execute(
                "select count(*) as count from outbox_events where correlation_id = %s",
                (item.id,),
            ).fetchone()["count"]
        self.assertEqual(1, count)

    def test_single_audit_lookup_is_tenant_scoped_and_returns_metadata(self) -> None:
        """Exercise the SQL path used by operator recall replay, not just list/export."""
        event = AuditEvent(
            tenant_id=self.tenant,
            workspace_id=self.workspace,
            action="memory.recall",
            actor="integration-test",
            actor_type="system",
            resource_type="memory_recall",
            metadata={"trace_ids": [], "query_sha256": "a" * 64},
        )
        self.store.append_audit_event(event)

        loaded = self.store.get_audit_event(self.tenant, event.id)

        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(event.id, loaded.id)
        self.assertEqual(event.workspace_id, loaded.workspace_id)
        self.assertEqual(event.metadata, loaded.metadata)
        self.assertIsNone(self.store.get_audit_event(uuid4(), event.id))

    def test_protected_search_dual_write_is_scoped_and_uses_runtime_role(self) -> None:
        key = "integration-blind-index-" + "a" * 40
        with patch.dict(
            os.environ,
            {
                "UAM_PROTECTED_SEARCH_INDEX": "hmac-v1",
                "UAM_PROTECTED_SEARCH_INDEX_KEY": key,
                "UAM_PROTECTED_SEARCH_INDEX_KEY_VERSION": "3",
            },
            clear=False,
        ):
            protected = PostgresMemoryLedger(DATABASE_URL or "")
            item = self._item("Sensitive preference is stored as blind tokens")
            protected.retain(item, self._event(item), "protected-search-dual-write")

        with self.store._connection() as connection:
            self.store._set_tenant(connection, self.tenant)
            rows = connection.execute(
                """
                select key_version, digest
                from memory_search_tokens
                where memory_item_id = %s
                """,
                (item.id,),
            ).fetchall()
        self.assertGreater(len(rows), 0)
        self.assertTrue(all(row["key_version"] == 3 for row in rows))
        self.assertTrue(all(len(row["digest"]) == 32 for row in rows))

        other_workspace = uuid4()
        with self.store._connection() as connection:
            self.store._set_tenant(connection, self.tenant)
            connection.execute(
                "insert into workspaces (id, tenant_id, name) values (%s, %s, %s)",
                (other_workspace, self.tenant, "other-workspace"),
            )
            with self.assertRaises(PostgresError):
                connection.execute(
                    """
                    insert into memory_search_tokens (
                      tenant_id, workspace_id, memory_item_id, key_version, digest
                    ) values (%s, %s, %s, %s, %s)
                    """,
                    (self.tenant, other_workspace, item.id, 3, b"x" * 32),
                )

    def test_protected_search_backfill_resumes_under_runtime_role(self) -> None:
        first = self._item("legacy blind index backfill alpha")
        second = self._item("legacy blind index backfill beta")
        self.store.retain(first, self._event(first), "legacy-backfill-first")
        self.store.retain(second, self._event(second), "legacy-backfill-second")
        key = "integration-backfill-index-" + "a" * 40
        with patch.dict(
            os.environ,
            {
                "UAM_PROTECTED_SEARCH_INDEX": "hmac-v1",
                "UAM_PROTECTED_SEARCH_INDEX_KEY": key,
                "UAM_PROTECTED_SEARCH_INDEX_KEY_VERSION": "5",
            },
            clear=False,
        ):
            protected = PostgresMemoryLedger(DATABASE_URL or "")
            first_run = backfill_workspace(
                protected,
                tenant_id=self.tenant,
                workspace_id=self.workspace,
                batch_size=1,
                max_batches=1,
            )
            second_run = backfill_workspace(
                protected,
                tenant_id=self.tenant,
                workspace_id=self.workspace,
                cursor=BackfillCursor(**first_run["cursor"]),
                batch_size=1,
                max_batches=1,
            )
            completion_run = backfill_workspace(
                protected,
                tenant_id=self.tenant,
                workspace_id=self.workspace,
                cursor=BackfillCursor(**second_run["cursor"]),
                batch_size=1,
                max_batches=1,
            )

        self.assertEqual(1, first_run["rows_scanned"])
        self.assertEqual(1, second_run["rows_scanned"])
        self.assertTrue(completion_run["complete"])
        self.assertEqual(0, completion_run["rows_scanned"])
        with self.store._connection() as connection:
            self.store._set_tenant(connection, self.tenant)
            count = connection.execute(
                """
                select count(*) as count
                from memory_search_tokens
                where workspace_id = %s and key_version = 5
                """,
                (self.workspace,),
            ).fetchone()["count"]
        self.assertGreater(count, 0)

    def test_pgcrypto_search_uses_blind_index_only_after_complete_backfill(self) -> None:
        key = "integration-encrypted-search-" + "a" * 40
        item = self._item("encrypted recall marker nebula-4821")
        tokenless = self._item("🤖")
        with patch.dict(
            os.environ,
            {
                "UAM_MEMORY_TEXT_ENCRYPTION": "pgcrypto",
                "UAM_MEMORY_TEXT_ENCRYPTION_KEY": key,
                "UAM_PROTECTED_SEARCH_INDEX": "off",
            },
            clear=False,
        ):
            encrypted = PostgresMemoryLedger(DATABASE_URL or "")
            encrypted.retain(item, self._event(item), "encrypted-protected-reader")
            encrypted.retain(tokenless, self._event(tokenless), "encrypted-tokenless-marker")

        with patch.dict(
            os.environ,
            {
                "UAM_MEMORY_TEXT_ENCRYPTION": "pgcrypto",
                "UAM_MEMORY_TEXT_ENCRYPTION_KEY": key,
                "UAM_PROTECTED_SEARCH_INDEX": "hmac-v1",
                "UAM_PROTECTED_SEARCH_INDEX_KEY": "blind-index-reader-" + "b" * 40,
                "UAM_PROTECTED_SEARCH_INDEX_KEY_VERSION": "6",
            },
            clear=False,
        ):
            protected = PostgresMemoryLedger(DATABASE_URL or "")
            query = RecallQuery(
                tenant_id=self.tenant,
                workspace_id=self.workspace,
                text="nebula 4821",
                top_k=3,
            )
            self.assertEqual(item.id, protected.search(query)[0].item.id)
            backfill_workspace(
                protected,
                tenant_id=self.tenant,
                workspace_id=self.workspace,
                batch_size=10,
            )
            probe = capture_protected_search_probe(
                protected,
                tenant_id=self.tenant,
                workspace_id=self.workspace,
                query=query.text,
            )
            with patch.object(protected, "list_for_workspace", side_effect=AssertionError):
                indexed = protected.search(query)

        self.assertEqual(item.id, indexed[0].item.id)
        self.assertTrue(probe["coverage_complete"])
        self.assertTrue(probe["index_used"])

    def test_pgcrypto_protects_noncanonical_memory_fields_and_round_trips(self) -> None:
        """Operational evidence must not leave quotes, summaries or state in plaintext."""
        key = "integration-pgcrypto-" + "a" * 40
        with patch.dict(
            os.environ,
            {
                "UAM_MEMORY_TEXT_ENCRYPTION": "pgcrypto",
                "UAM_MEMORY_TEXT_ENCRYPTION_KEY": key,
                "UAM_MEMORY_TEXT_ENCRYPTION_SCOPES": "all",
            },
            clear=False,
        ):
            protected = PostgresMemoryLedger(DATABASE_URL or "")
            item = self._item("protected canonical text")
            item = replace(
                item,
                provenance=Provenance(
                    source_kind="test",
                    quote="private supporting quote 9001",
                ),
            )
            protected.retain(item, self._event(item), "protected-noncanonical-fields")
            self.assertEqual(item, protected.get(self.tenant, item.id))

            agent, _ = protected.provision_agent_thread(
                AgentIdentity(
                    id=uuid4(),
                    tenant_id=self.tenant,
                    workspace_id=self.workspace,
                    name="Protected metadata agent",
                    role="integration",
                    config={"secret": "private agent configuration 9005"},
                )
            )
            self.assertEqual("private agent configuration 9005", agent.config["secret"])

            turn = ConversationTurn(
                tenant_id=self.tenant,
                workspace_id=self.workspace,
                thread_id=uuid4(),
                metadata={"secret": "private turn metadata 9006"},
                messages=(
                    ConversationMessage(
                        role="user",
                        content="private conversation content 9007",
                        metadata={"secret": "private message metadata 9008"},
                    ),
                ),
            )
            protected.append_turn(turn, "protected-turn-metadata")
            self.assertEqual(turn, protected.get_turn(self.tenant, turn.id))

            proposal = MemoryProposal(
                tenant_id=self.tenant,
                workspace_id=self.workspace,
                namespace="protected-metadata",
                requester="integration-test",
                target=MemoryProposalTarget.FACT,
                proposal="private proposal content 9009",
                evidence="private proposal evidence 9010",
                metadata={"secret": "private proposal metadata 9011"},
            )
            protected.append_proposal(proposal, "protected-proposal-metadata")
            self.assertEqual(proposal, protected.get_proposal(self.tenant, proposal.id))

            observation = Observation(
                tenant_id=self.tenant,
                workspace_id=self.workspace,
                summary="private derived observation 9002",
                evidence_ids=(item.id,),
            )
            protected.save(observation)
            self.assertEqual(
                observation.summary,
                protected.list_observations(self.tenant, self.workspace)[-1].summary,
            )

            audit = AuditEvent(
                tenant_id=self.tenant,
                workspace_id=self.workspace,
                action="integration.protected_fields",
                actor="test",
                actor_type="system",
                resource_type="memory_item",
                resource_id=str(item.id),
                metadata={"detail": "private audit detail 9003"},
            )
            protected.append_audit_event(audit)
            self.assertEqual(
                audit.metadata,
                protected.list_audit_events(self.tenant, workspace_id=self.workspace)[0].metadata,
            )

            thread_id = uuid4()
            with protected._connection() as connection:
                protected._set_tenant(connection, self.tenant)
                connection.execute(
                    """
                    insert into threads (id, tenant_id, workspace_id, owner_agent_id, status)
                    values (%s, %s, %s, null, 'active')
                    """,
                    (thread_id, self.tenant, self.workspace),
                )
            checkpoint = Checkpoint(
                tenant_id=self.tenant,
                workspace_id=self.workspace,
                thread_id=thread_id,
                revision=1,
                state={"secret": "private checkpoint state 9004"},
            )
            checkpoint_store = PostgresCheckpointStore(protected)
            checkpoint_store.save(checkpoint)
            restored = checkpoint_store.get_head(self.tenant, thread_id)
            self.assertIsNotNone(restored)
            self.assertEqual(checkpoint.state, restored.state if restored else None)

            with protected._connection() as connection:
                protected._set_tenant(connection, self.tenant)
                quote = connection.execute(
                    "select quote_text from memory_provenance where memory_item_id = %s",
                    (item.id,),
                ).fetchone()["quote_text"]
                summary = connection.execute(
                    "select summary from observations where id = %s",
                    (observation.id,),
                ).fetchone()["summary"]
                metadata = connection.execute(
                    "select metadata from audit_events where id = %s",
                    (audit.id,),
                ).fetchone()["metadata"]
                item_metadata = connection.execute(
                    "select metadata from memory_items where id = %s",
                    (item.id,),
                ).fetchone()["metadata"]
                event_payload = connection.execute(
                    "select payload from outbox_events where correlation_id = %s",
                    (item.id,),
                ).fetchone()["payload"]
                agent_config = connection.execute(
                    "select config from agents where id = %s",
                    (agent.id,),
                ).fetchone()["config"]
                turn_metadata = connection.execute(
                    "select metadata from conversation_turns where id = %s",
                    (turn.id,),
                ).fetchone()["metadata"]
                message_metadata = connection.execute(
                    "select metadata from conversation_messages where turn_id = %s",
                    (turn.id,),
                ).fetchone()["metadata"]
                proposal_row = connection.execute(
                    "select proposal, evidence, metadata from memory_proposals where id = %s",
                    (proposal.id,),
                ).fetchone()
                state = connection.execute(
                    "select state from checkpoints where id = %s",
                    (checkpoint.id,),
                ).fetchone()["state"]
            self.assertNotIn("private supporting quote 9001", quote)
            self.assertNotIn("private derived observation 9002", summary)
            self.assertNotIn("private audit detail 9003", str(metadata))
            self.assertNotIn("private checkpoint state 9004", str(state))
            for marker, raw_value in (
                ("integration-test", item_metadata),
                ("memory_id", event_payload),
                ("private agent configuration 9005", agent_config),
                ("private turn metadata 9006", turn_metadata),
                ("private message metadata 9008", message_metadata),
                ("private proposal content 9009", proposal_row["proposal"]),
                ("private proposal evidence 9010", proposal_row["evidence"]),
                ("private proposal metadata 9011", proposal_row["metadata"]),
            ):
                self.assertNotIn(marker, str(raw_value))

    def test_plaintext_lexical_search_uses_postgres_fts_candidate_path(self) -> None:
        """Plaintext deployments must bound lexical recall in PostgreSQL."""
        self.assertFalse(self.store._text_encryption_enabled)
        item = self._item("Obelisk remembers unique FTS marker quartz-astronomy-417.")
        self.store.retain(item, self._event(item), "fts-candidate-path")

        results = self.store.search(
            RecallQuery(
                tenant_id=self.tenant,
                workspace_id=self.workspace,
                text="quartz astronomy 417",
                top_k=3,
            )
        )

        self.assertTrue(results)
        self.assertEqual(item.id, results[0].item.id)
        self.assertGreater(results[0].lexical, 0)

    def test_plaintext_fts_search_enforces_thread_and_private_scope(self) -> None:
        """SQL candidate filtering must not broaden agent or thread visibility."""
        self.assertFalse(self.store._text_encryption_enabled)
        agent_id = uuid4()
        thread_id = uuid4()
        self.store.provision_agent_thread(
            AgentIdentity(
                id=agent_id,
                tenant_id=self.tenant,
                workspace_id=self.workspace,
                name="FTS scope test agent",
                role="integration",
            ),
            thread_id=thread_id,
        )
        workspace_item = self._item("scope marker visible to workspace")
        thread_item = replace(
            self._item("scope marker visible only to thread"),
            scope=MemoryScope.THREAD,
            agent_id=agent_id,
            thread_id=thread_id,
        )
        private_item = replace(
            self._item("scope marker visible only to agent"),
            scope=MemoryScope.PRIVATE,
            agent_id=agent_id,
        )
        for index, item in enumerate((workspace_item, thread_item, private_item), start=1):
            self.store.retain(item, self._event(item), f"fts-scope-{index}")

        anonymous = self.store.search(
            RecallQuery(
                tenant_id=self.tenant,
                workspace_id=self.workspace,
                text="scope marker visible",
                top_k=10,
            )
        )
        scoped = self.store.search(
            RecallQuery(
                tenant_id=self.tenant,
                workspace_id=self.workspace,
                agent_id=agent_id,
                thread_id=thread_id,
                text="scope marker visible",
                top_k=10,
            )
        )

        self.assertEqual({workspace_item.id}, {candidate.item.id for candidate in anonymous})
        self.assertEqual(
            {workspace_item.id, thread_item.id, private_item.id},
            {candidate.item.id for candidate in scoped},
        )

    def test_proposal_accept_rolls_back_memory_and_status_when_outbox_insert_fails(self) -> None:
        """A failed atomic accept must never turn an LLM proposal into a fact."""
        service = MemoryProposalService(self.store, RetentionService(self.store))
        submitted = service.submit(
            SubmitMemoryProposalCommand(
                tenant_id=self.tenant,
                workspace_id=self.workspace,
                namespace="postgres-failure-injection",
                requester="integration-test",
                target=MemoryProposalTarget.FACT,
                proposal="Synthetic proposal must not survive a failed outbox write.",
                evidence="Failure injection test evidence.",
                idempotency_key="proposal-outbox-failure",
            )
        )

        with patch.object(
            self.store,
            "_insert_event",
            side_effect=RuntimeError("outbox unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "outbox unavailable"):
                service.accept(
                    ReviewMemoryProposalCommand(
                        tenant_id=self.tenant,
                        proposal_id=submitted.proposal.id,
                        reviewer="integration-test",
                        reason="intentional failure injection",
                    )
                )

        proposal = self.store.get_proposal(self.tenant, submitted.proposal.id)
        self.assertIsNotNone(proposal)
        assert proposal is not None
        self.assertEqual(MemoryProposalStatus.OPEN, proposal.status)
        self.assertNotIn("accepted_memory_id", proposal.metadata)
        with self.store._connection() as connection:
            self.store._set_tenant(connection, self.tenant)
            memory_count = connection.execute(
                """
                select count(*) as count
                from memory_items m
                join memory_provenance p on p.memory_item_id = m.id
                where m.workspace_id = %s and p.origin_uri = %s
                """,
                (self.workspace, f"proposal://{submitted.proposal.id}"),
            ).fetchone()["count"]
            outbox_count = connection.execute(
                """
                select count(*) as count
                from outbox_events
                where workspace_id = %s and correlation_id is not null
                """,
                (self.workspace,),
            ).fetchone()["count"]
        self.assertEqual(0, memory_count)
        self.assertEqual(0, outbox_count)

    def test_proposal_submit_audit_failure_rolls_back_proposal(self) -> None:
        service = MemoryProposalService(self.store, RetentionService(self.store))
        audit = AuditEvent(
            tenant_id=self.tenant,
            workspace_id=self.workspace,
            action="proposal.submit",
            actor="integration",
            actor_type="system",
            resource_type="memory_proposal",
        )
        with patch.object(
            self.store, "_insert_audit_event", side_effect=RuntimeError("audit down")
        ):
            with self.assertRaisesRegex(RuntimeError, "audit down"):
                service.submit(
                    SubmitMemoryProposalCommand(
                        tenant_id=self.tenant,
                        workspace_id=self.workspace,
                        namespace="postgres-submit-audit-failure",
                        requester="integration-test",
                        target=MemoryProposalTarget.FACT,
                        proposal="Submit audit failure must not store a proposal.",
                    ),
                    audit_event=audit,
                )
        self.assertEqual(
            (),
            self.store.list_proposals(
                self.tenant, self.workspace, namespace="postgres-submit-audit-failure"
            ),
        )

    def test_proposal_listing_uses_stable_keyset_cursor(self) -> None:
        service = MemoryProposalService(self.store, RetentionService(self.store))
        namespace = "postgres-proposal-pagination"
        for index in range(3):
            service.submit(
                SubmitMemoryProposalCommand(
                    tenant_id=self.tenant,
                    workspace_id=self.workspace,
                    namespace=namespace,
                    requester="integration-test",
                    target=MemoryProposalTarget.FACT,
                    proposal=f"Stable proposal page {index}",
                    idempotency_key=f"postgres-proposal-page-{index}",
                )
            )

        first = self.store.list_proposals(
            self.tenant,
            self.workspace,
            namespace=namespace,
            limit=2,
        )
        second = self.store.list_proposals(
            self.tenant,
            self.workspace,
            namespace=namespace,
            before_created_at=first[-1].created_at,
            before_proposal_id=first[-1].id,
            limit=2,
        )

        self.assertEqual(2, len(first))
        self.assertEqual(1, len(second))
        self.assertFalse({row.id for row in first} & {row.id for row in second})

    def test_conversation_curation_audit_failure_rolls_back_proposal(self) -> None:
        turn = ConversationTurn(
            tenant_id=self.tenant,
            workspace_id=self.workspace,
            thread_id=uuid4(),
            namespace="postgres-curation-audit-failure",
            retention_policy=ConversationRetentionPolicy.RAW_AND_CURATED,
            messages=(ConversationMessage(role="user", content="curation audit probe"),),
        )
        self.store.append_turn(turn, "curation-audit-turn")
        proposals = MemoryProposalService(self.store, RetentionService(self.store))
        curator = ConversationCurator(
            self.store,
            RetentionService(self.store),
            proposals=proposals,
        )
        audit = AuditEvent(
            tenant_id=self.tenant,
            workspace_id=self.workspace,
            action="conversation.curate.propose",
            actor="integration",
            actor_type="system",
            resource_type="memory_proposal",
        )
        with patch.object(
            self.store, "_insert_audit_event", side_effect=RuntimeError("audit down")
        ):
            with self.assertRaisesRegex(RuntimeError, "audit down"):
                curator.curate_turn(
                    CurateConversationTurnCommand(tenant_id=self.tenant, turn_id=turn.id),
                    audit_event=audit,
                )
        self.assertEqual(
            (),
            self.store.list_proposals(
                self.tenant,
                self.workspace,
                namespace="postgres-curation-audit-failure",
            ),
        )

    def test_conversation_append_audit_failure_rolls_back_transcript(self) -> None:
        service = ConversationService(self.store)
        thread_id = uuid4()
        audit = AuditEvent(
            tenant_id=self.tenant,
            workspace_id=self.workspace,
            action="conversation.turn.append",
            actor="integration",
            actor_type="system",
            resource_type="conversation_turn",
        )
        with patch.object(
            self.store, "_insert_audit_event", side_effect=RuntimeError("audit down")
        ):
            with self.assertRaisesRegex(RuntimeError, "audit down"):
                service.append_turn(
                    AppendConversationTurnCommand(
                        tenant_id=self.tenant,
                        workspace_id=self.workspace,
                        thread_id=thread_id,
                        namespace="postgres-turn-audit-failure",
                        messages=(ConversationMessage(role="user", content="audit probe"),),
                    ),
                    audit_event=audit,
                )
        self.assertEqual(
            (),
            self.store.list_turns(
                self.tenant,
                self.workspace,
                namespace="postgres-turn-audit-failure",
            ),
        )

    def test_graph_edge_audit_failure_rolls_back_edge(self) -> None:
        source = self._item("graph audit source")
        target = self._item("graph audit target")
        self.store.retain(source, self._event(source))
        self.store.retain(target, self._event(target))
        edge = MemoryEdge(
            tenant_id=self.tenant,
            workspace_id=self.workspace,
            src_id=source.id,
            dst_id=target.id,
            edge_type=MemoryEdgeType.SUPPORTS,
        )
        audit = AuditEvent(
            tenant_id=self.tenant,
            workspace_id=self.workspace,
            action="graph.edge.create",
            actor="integration",
            actor_type="system",
            resource_type="memory_edge",
            resource_id=str(edge.id),
        )
        with patch.object(
            self.store, "_insert_audit_event", side_effect=RuntimeError("audit down")
        ):
            with self.assertRaisesRegex(RuntimeError, "audit down"):
                self.store.save_edge(edge, audit_event=audit)
        self.assertEqual(
            (),
            self.store.list_neighbors(self.tenant, self.workspace, source.id),
        )

    def test_graph_neighbor_listing_uses_stable_keyset_cursor(self) -> None:
        source = self._item("graph page source")
        self.store.retain(source, self._event(source))
        for index in range(3):
            target = self._item(f"graph page target {index}")
            self.store.retain(target, self._event(target))
            self.store.save_edge(
                MemoryEdge(
                    tenant_id=self.tenant,
                    workspace_id=self.workspace,
                    src_id=source.id,
                    dst_id=target.id,
                    edge_type=MemoryEdgeType.SUPPORTS,
                )
            )

        first = self.store.list_neighbors(
            self.tenant,
            self.workspace,
            source.id,
            limit=2,
        )
        second = self.store.list_neighbors(
            self.tenant,
            self.workspace,
            source.id,
            after_created_at=first[-1].created_at,
            after_edge_id=first[-1].id,
            limit=2,
        )

        self.assertEqual(2, len(first))
        self.assertEqual(1, len(second))
        self.assertFalse({row.id for row in first} & {row.id for row in second})

    def test_proposal_accept_audit_failure_rolls_back_memory_and_status(self) -> None:
        service = MemoryProposalService(self.store, RetentionService(self.store))
        submitted = service.submit(
            SubmitMemoryProposalCommand(
                tenant_id=self.tenant,
                workspace_id=self.workspace,
                namespace="postgres-audit-failure",
                requester="integration-test",
                target=MemoryProposalTarget.FACT,
                proposal="Audit failure must not create durable memory.",
                evidence="Failure injection evidence.",
            )
        )
        audit = AuditEvent(
            tenant_id=self.tenant,
            workspace_id=self.workspace,
            action="proposal.accept",
            actor="integration",
            actor_type="system",
            resource_type="memory_proposal",
            resource_id=str(submitted.proposal.id),
        )
        with patch.object(
            self.store, "_insert_audit_event", side_effect=RuntimeError("audit down")
        ):
            with self.assertRaisesRegex(RuntimeError, "audit down"):
                service.accept(
                    ReviewMemoryProposalCommand(
                        tenant_id=self.tenant,
                        proposal_id=submitted.proposal.id,
                        reviewer="integration-test",
                    ),
                    audit_event=audit,
                )
        proposal = self.store.get_proposal(self.tenant, submitted.proposal.id)
        self.assertIsNotNone(proposal)
        assert proposal is not None
        self.assertEqual(MemoryProposalStatus.OPEN, proposal.status)
        self.assertNotIn("accepted_memory_id", proposal.metadata)

    def test_proposal_reject_audit_failure_rolls_back_review_status(self) -> None:
        service = MemoryProposalService(self.store, RetentionService(self.store))
        submitted = service.submit(
            SubmitMemoryProposalCommand(
                tenant_id=self.tenant,
                workspace_id=self.workspace,
                namespace="postgres-reject-audit-failure",
                requester="integration-test",
                target=MemoryProposalTarget.FACT,
                proposal="Reject audit failure must preserve the open proposal.",
            )
        )
        audit = AuditEvent(
            tenant_id=self.tenant,
            workspace_id=self.workspace,
            action="proposal.reject",
            actor="integration",
            actor_type="system",
            resource_type="memory_proposal",
            resource_id=str(submitted.proposal.id),
        )
        with patch.object(
            self.store, "_insert_audit_event", side_effect=RuntimeError("audit down")
        ):
            with self.assertRaisesRegex(RuntimeError, "audit down"):
                service.reject(
                    ReviewMemoryProposalCommand(
                        tenant_id=self.tenant,
                        proposal_id=submitted.proposal.id,
                        reviewer="integration-test",
                    ),
                    audit_event=audit,
                )
        proposal = self.store.get_proposal(self.tenant, submitted.proposal.id)
        self.assertIsNotNone(proposal)
        assert proposal is not None
        self.assertEqual(MemoryProposalStatus.OPEN, proposal.status)

    def test_curated_only_raw_content_can_be_purged_without_losing_turn_identity(
        self,
    ) -> None:
        turn = ConversationTurn(
            tenant_id=self.tenant,
            workspace_id=self.workspace,
            thread_id=uuid4(),
            retention_policy=ConversationRetentionPolicy.CURATED_ONLY,
            messages=(
                ConversationMessage(role="user", content="temporary raw transcript"),
            ),
        )
        stored, created = self.store.append_turn(turn, "curated-only-postgres")

        purged = self.store.purge_turn_content(self.tenant, turn.id)
        loaded = self.store.get_turn(self.tenant, turn.id)

        self.assertTrue(created)
        self.assertEqual(turn.id, stored.id)
        self.assertTrue(purged)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(PURGED_CONVERSATION_CONTENT, loaded.messages[0].content)
        self.assertEqual(
            "purged_after_curation",
            loaded.metadata["retention"]["raw_content"],
        )

    def test_provisioned_agent_and_thread_satisfy_memory_foreign_keys(self) -> None:
        agent_id = uuid4()
        thread_id = uuid4()
        agent, thread = self.store.provision_agent_thread(
            AgentIdentity(
                id=agent_id,
                tenant_id=self.tenant,
                workspace_id=self.workspace,
                name="Hermes integration",
                role="hermes",
                config={"namespace": "hermes/integration"},
            ),
            thread_id=thread_id,
        )
        item = replace(
            self._item("Provisioned identities retain correctly"),
            agent_id=agent.id,
            thread_id=thread_id,
            scope=MemoryScope.THREAD,
        )

        stored, created = self.store.retain(item, self._event(item))

        self.assertTrue(created)
        self.assertEqual(agent_id, stored.agent_id)
        self.assertIsNotNone(thread)
        self.assertEqual(thread_id, thread.id if thread else None)

    def test_concurrent_first_checkpoint_is_compare_and_swap_safe(self) -> None:
        agent_id = uuid4()
        thread_id = uuid4()
        self.store.provision_agent_thread(
            AgentIdentity(
                id=agent_id,
                tenant_id=self.tenant,
                workspace_id=self.workspace,
                name="Checkpoint agent",
                role="test",
            ),
            thread_id=thread_id,
        )
        checkpoints = tuple(
            Checkpoint(
                tenant_id=self.tenant,
                workspace_id=self.workspace,
                thread_id=thread_id,
                revision=1,
                state={"writer": writer},
            )
            for writer in ("a", "b")
        )
        checkpoint_store = PostgresCheckpointStore(self.store)

        def attempt(checkpoint: Checkpoint) -> str:
            try:
                checkpoint_store.save_if_head(checkpoint, 0)
            except StaleRevisionError:
                return "stale"
            return "saved"

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(executor.map(attempt, checkpoints))

        self.assertEqual(["saved", "stale"], sorted(results))

    def test_checkpoint_audit_failure_rolls_back_checkpoint_revision(self) -> None:
        agent_id = uuid4()
        thread_id = uuid4()
        self.store.provision_agent_thread(
            AgentIdentity(
                id=agent_id,
                tenant_id=self.tenant,
                workspace_id=self.workspace,
                name="Checkpoint audit agent",
                role="test",
            ),
            thread_id=thread_id,
        )
        checkpoint = Checkpoint(
            tenant_id=self.tenant,
            workspace_id=self.workspace,
            thread_id=thread_id,
            revision=1,
            state={"checkpoint": "must roll back"},
        )
        audit = AuditEvent(
            tenant_id=self.tenant,
            workspace_id=self.workspace,
            action="checkpoint.save",
            actor="integration",
            actor_type="system",
            resource_type="checkpoint",
        )
        checkpoint_store = PostgresCheckpointStore(self.store)

        with patch.object(
            self.store,
            "_insert_audit_event",
            side_effect=RuntimeError("audit unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "audit unavailable"):
                checkpoint_store.save_if_head(checkpoint, 0, audit_event=audit)

        self.assertIsNone(checkpoint_store.get_head(self.tenant, thread_id))

    def test_checkpoint_compaction_audit_failure_preserves_history(self) -> None:
        agent_id = uuid4()
        thread_id = uuid4()
        self.store.provision_agent_thread(
            AgentIdentity(
                id=agent_id,
                tenant_id=self.tenant,
                workspace_id=self.workspace,
                name="Checkpoint compaction audit agent",
                role="test",
            ),
            thread_id=thread_id,
        )
        checkpoint_store = PostgresCheckpointStore(self.store)
        for revision in (1, 2):
            checkpoint_store.save_if_head(
                Checkpoint(
                    tenant_id=self.tenant,
                    workspace_id=self.workspace,
                    thread_id=thread_id,
                    revision=revision,
                    state={"revision": revision},
                ),
                revision - 1,
            )
        audit = AuditEvent(
            tenant_id=self.tenant,
            workspace_id=self.workspace,
            action="checkpoint.compact",
            actor="integration",
            actor_type="system",
            resource_type="checkpoint_thread",
        )

        with patch.object(
            self.store,
            "_insert_audit_event",
            side_effect=RuntimeError("audit unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "audit unavailable"):
                checkpoint_store.compact(
                    self.tenant,
                    thread_id,
                    keep_last=1,
                    audit_event=audit,
                )

        self.assertIsNotNone(checkpoint_store.get_revision(self.tenant, thread_id, 1))
        self.assertIsNotNone(checkpoint_store.get_revision(self.tenant, thread_id, 2))

    def test_conflict_override_atomically_controls_canonical_recall(self) -> None:
        selected = self._item("Release Alpha is July 15.")
        newer = self._item("Release Alpha is July 16.")
        self.store.retain(selected, self._event(selected))
        self.store.retain(newer, self._event(newer))
        conflicts = ConflictService(
            self.store,
            PostgresConflictReviewRepository(self.store),
        )
        case = conflicts.list_cases(self.tenant, self.workspace)[0]

        decision = conflicts.decide(
            self.tenant,
            self.workspace,
            case.id,
            status=ConflictReviewStatus.OVERRIDDEN,
            winner_value="july 15",
            reason="verified release plan",
        )
        retry = conflicts.decide(
            self.tenant,
            self.workspace,
            case.id,
            status=ConflictReviewStatus.OVERRIDDEN,
            winner_value="july 15",
            reason="idempotent retry",
        )

        recalled = self.store.search(
            RecallQuery(
                tenant_id=self.tenant,
                workspace_id=self.workspace,
                text="Release Alpha July",
            )
        )
        self.assertEqual(selected.id, decision.applied_memory_id)
        self.assertEqual(decision.applied_memory_id, retry.applied_memory_id)
        self.assertEqual((selected.id,), tuple(row.item.id for row in recalled))
        with self.store._connection() as connection:
            self.store._set_tenant(connection, self.tenant)
            resolution_events = connection.execute(
                """
                select count(*) as count
                from outbox_events
                where payload ->> 'reason' = 'conflict-resolution'
                """
            ).fetchone()["count"]
        self.assertEqual(1, resolution_events)

        competing = ConflictReviewDecision(
            tenant_id=self.tenant,
            workspace_id=self.workspace,
            case_id=case.id,
            status=ConflictReviewStatus.OVERRIDDEN,
            winner_value="july 16",
            applied_memory_id=newer.id,
        )
        selected_tombstone = selected.supersede(
            selected.text,
            status=MemoryStatus.ARCHIVED,
        )
        with self.assertRaisesRegex(ValueError, "already applied and immutable"):
            PostgresConflictReviewRepository(self.store).apply_resolution(
                competing,
                ((selected_tombstone, self._event(selected_tombstone), 1),),
            )
        self.assertTrue(self.store.is_recallable_head(self.tenant, selected.id))

    def test_conflict_resolution_rolls_back_all_writes_on_stale_parent(self) -> None:
        first = self._item("Rollback conflict one")
        second = self._item("Rollback conflict two")
        self.store.retain(first, self._event(first))
        self.store.retain(second, self._event(second))
        first_tombstone = first.supersede(first.text, status=MemoryStatus.ARCHIVED)
        second_tombstone = second.supersede(second.text, status=MemoryStatus.ARCHIVED)
        decision = ConflictReviewDecision(
            tenant_id=self.tenant,
            workspace_id=self.workspace,
            case_id=uuid4(),
            status=ConflictReviewStatus.ACCEPTED,
            winner_value="one",
            applied_memory_id=first.id,
        )
        reviews = PostgresConflictReviewRepository(self.store)

        with self.assertRaises(MemoryRevisionConflictError):
            reviews.apply_resolution(
                decision,
                (
                    (first_tombstone, self._event(first_tombstone), 1),
                    (second_tombstone, self._event(second_tombstone), 99),
                ),
            )

        self.assertIsNone(self.store.get(self.tenant, first_tombstone.id))
        self.assertIsNone(self.store.get(self.tenant, second_tombstone.id))
        self.assertEqual((), reviews.list_for_workspace(self.tenant, self.workspace))

    def test_conflict_resolution_audit_failure_rolls_back_writes_and_review(self) -> None:
        item = self._item("Conflict audit rollback")
        self.store.retain(item, self._event(item))
        tombstone = item.supersede(item.text, status=MemoryStatus.ARCHIVED)
        decision = ConflictReviewDecision(
            tenant_id=self.tenant,
            workspace_id=self.workspace,
            case_id=uuid4(),
            status=ConflictReviewStatus.ACCEPTED,
            winner_value="rollback",
            applied_memory_id=tombstone.id,
        )
        audit = AuditEvent(
            tenant_id=self.tenant,
            workspace_id=self.workspace,
            action="conflict.decide",
            actor="integration",
            actor_type="system",
            resource_type="conflict_case",
            resource_id=str(decision.case_id),
        )
        reviews = PostgresConflictReviewRepository(self.store)
        with patch.object(
            self.store, "_insert_audit_event", side_effect=RuntimeError("audit down")
        ):
            with self.assertRaisesRegex(RuntimeError, "audit down"):
                reviews.apply_resolution(
                    decision,
                    ((tombstone, self._event(tombstone), item.revision),),
                    audit_event=audit,
                )
        self.assertIsNone(self.store.get(self.tenant, tombstone.id))
        self.assertTrue(self.store.is_recallable_head(self.tenant, item.id))
        self.assertEqual((), reviews.list_for_workspace(self.tenant, self.workspace))

    def test_supersede_if_current_is_cas_and_idempotent(self) -> None:
        item = self._item("Alpha release is July 15")
        self.store.retain(item, self._event(item))
        replacement = item.supersede("Alpha release is July 16")
        event = self._event(replacement)

        stored, created = self.store.supersede_if_current(
            replacement,
            event,
            expected_revision=1,
            idempotency_key="supersede-alpha",
        )
        retry, retry_created = self.store.supersede_if_current(
            replacement,
            event,
            expected_revision=1,
            idempotency_key="supersede-alpha",
        )
        stale = item.supersede("Alpha release is July 17")

        self.assertTrue(created)
        self.assertFalse(retry_created)
        self.assertEqual(stored.id, retry.id)
        self.assertEqual(2, stored.revision)
        self.assertEqual(item.id, stored.supersedes_id)
        recalled = self.store.search(
            RecallQuery(
                tenant_id=self.tenant,
                workspace_id=self.workspace,
                text="Alpha release July",
            )
        )
        self.assertEqual((stored.id,), tuple(row.item.id for row in recalled))
        self.assertFalse(self.store.is_recallable_head(self.tenant, item.id))
        self.assertTrue(self.store.is_recallable_head(self.tenant, stored.id))
        with self.assertRaises(MemoryRevisionConflictError) as raised:
            self.store.supersede_if_current(
                stale,
                self._event(stale),
                expected_revision=1,
            )
        self.assertEqual(1, raised.exception.expected)
        self.assertEqual(2, raised.exception.actual)

    def test_outbox_failure_rolls_back_memory_and_provenance(self) -> None:
        first = self._item("first")
        duplicate_event_id = self._event(first).id
        self.store.retain(first, self._event(first, event_id=duplicate_event_id))
        second = self._item("must roll back")

        with self.assertRaises(PostgresError):
            self.store.retain(second, self._event(second, event_id=duplicate_event_id))

        self.assertIsNone(self.store.get(self.tenant, second.id))

    def test_audit_failure_rolls_back_memory_and_outbox(self) -> None:
        item = self._item("audit transaction rollback")
        audit = AuditEvent(
            tenant_id=self.tenant,
            workspace_id=self.workspace,
            action="memory.retain",
            actor="integration",
            actor_type="system",
            resource_type="memory_item",
            resource_id=str(item.id),
        )
        with patch.object(
            self.store, "_insert_audit_event", side_effect=RuntimeError("audit down")
        ):
            with self.assertRaisesRegex(RuntimeError, "audit down"):
                self.store.retain(item, self._event(item), audit_event=audit)

        self.assertIsNone(self.store.get(self.tenant, item.id))
        with self.store._connection() as connection:
            self.store._set_tenant(connection, self.tenant)
            count = connection.execute(
                "select count(*) as count from outbox_events where correlation_id = %s",
                (item.id,),
            ).fetchone()["count"]
        self.assertEqual(0, count)

    def test_observation_audit_failure_rolls_back_observation_and_evidence(self) -> None:
        item = self._item("observation audit rollback evidence")
        self.store.retain(item, self._event(item))
        observation = Observation(
            tenant_id=self.tenant,
            workspace_id=self.workspace,
            summary="derived observation must roll back with audit",
            evidence_ids=(item.id,),
        )
        audit = AuditEvent(
            tenant_id=self.tenant,
            workspace_id=self.workspace,
            action="reflection.observation.create",
            actor="maintenance",
            actor_type="system",
            resource_type="observation",
            resource_id=str(observation.id),
        )
        with patch.object(
            self.store, "_insert_audit_event", side_effect=RuntimeError("audit down")
        ):
            with self.assertRaisesRegex(RuntimeError, "audit down"):
                self.store.save(observation, audit_event=audit)

        self.assertNotIn(
            observation.id,
            {row.id for row in self.store.list_observations(self.tenant, self.workspace)},
        )
        with self.store._connection() as connection:
            self.store._set_tenant(connection, self.tenant)
            count = connection.execute(
                "select count(*) as count from observation_evidence where observation_id = %s",
                (observation.id,),
            ).fetchone()["count"]
        self.assertEqual(0, count)

    def test_supersede_audit_failure_rolls_back_replacement_and_outbox(self) -> None:
        parent = self._item("audit supersede parent")
        self.store.retain(parent, self._event(parent))
        replacement = parent.supersede("audit supersede replacement")
        audit = AuditEvent(
            tenant_id=self.tenant,
            workspace_id=self.workspace,
            action="memory.supersede",
            actor="integration",
            actor_type="system",
            resource_type="memory_item",
            resource_id=str(replacement.id),
        )
        with patch.object(
            self.store, "_insert_audit_event", side_effect=RuntimeError("audit down")
        ):
            with self.assertRaisesRegex(RuntimeError, "audit down"):
                self.store.supersede_if_current(
                    replacement,
                    self._event(replacement),
                    expected_revision=parent.revision,
                    audit_event=audit,
                )

        self.assertIsNone(self.store.get(self.tenant, replacement.id))
        self.assertTrue(self.store.is_recallable_head(self.tenant, parent.id))
        with self.store._connection() as connection:
            self.store._set_tenant(connection, self.tenant)
            count = connection.execute(
                "select count(*) as count from outbox_events where correlation_id = %s",
                (replacement.id,),
            ).fetchone()["count"]
        self.assertEqual(0, count)

    def test_rls_hides_another_tenants_item(self) -> None:
        item = self._item()
        self.store.retain(item, self._event(item))

        self.assertIsNone(self.store.get(uuid4(), item.id))

    def test_layer_filtered_listing_preserves_provenance(self) -> None:
        semantic = self._item()
        self.store.retain(semantic, self._event(semantic))
        core = replace(self._item("core policy"), layer=MemoryLayer.CORE)
        self.store.retain(core, self._event(core))

        rows = self.store.list_for_workspace(
            self.tenant, self.workspace, layers=(MemoryLayer.CORE,)
        )

        self.assertEqual((core.id,), tuple(row.id for row in rows))
        self.assertEqual("test://postgres", rows[0].provenance.origin_uri)

    def test_postgres_is_a_lexical_candidate_source(self) -> None:
        expected = self._item("Agents remember PostgreSQL facts")
        self.store.retain(expected, self._event(expected))

        rows = self.store.search(
            RecallQuery(
                tenant_id=self.tenant,
                workspace_id=self.workspace,
                text="PostgreSQL facts",
            )
        )

        self.assertEqual(expected.id, rows[0].item.id)
        self.assertEqual("postgres_lexical", rows[0].source)

    def test_private_recall_and_thread_ownership_are_agent_isolated(self) -> None:
        agent_a = uuid4()
        agent_b = uuid4()
        thread_a = uuid4()
        thread_b = uuid4()
        for agent_id, thread_id, name in (
            (agent_a, thread_a, "Private agent A"),
            (agent_b, thread_b, "Private agent B"),
        ):
            self.store.provision_agent_thread(
                AgentIdentity(
                    id=agent_id,
                    tenant_id=self.tenant,
                    workspace_id=self.workspace,
                    name=name,
                    role="integration",
                ),
                thread_id=thread_id,
            )

        private_a = replace(
            self._item("shared private marker agent alpha"),
            agent_id=agent_a,
            scope=MemoryScope.PRIVATE,
        )
        private_b = replace(
            self._item("shared private marker agent beta"),
            agent_id=agent_b,
            scope=MemoryScope.PRIVATE,
        )
        self.store.retain(private_a, self._event(private_a))
        self.store.retain(private_b, self._event(private_b))

        recalled = self.store.search(
            RecallQuery(
                tenant_id=self.tenant,
                workspace_id=self.workspace,
                agent_id=agent_a,
                text="shared private marker agent",
                top_k=10,
            )
        )

        self.assertEqual((private_a.id,), tuple(row.item.id for row in recalled))
        self.assertTrue(
            self.store.thread_belongs_to_agent(
                self.tenant, self.workspace, agent_a, thread_a
            )
        )
        self.assertFalse(
            self.store.thread_belongs_to_agent(
                self.tenant, self.workspace, agent_a, thread_b
            )
        )

    def test_outbox_lease_prevents_concurrent_delivery_and_acknowledges(self) -> None:
        item = self._item("leased event")
        event = self._event(item)
        self.store.retain(item, event)

        claimed = self.store.claim_outbox(
            self.tenant, "relay-a", limit=10, lease_seconds=30
        )
        competing = self.store.claim_outbox(
            self.tenant, "relay-b", limit=10, lease_seconds=30
        )

        self.assertEqual((event.id,), tuple(row.event.id for row in claimed))
        self.assertEqual((), competing)
        self.assertFalse(
            self.store.mark_outbox_published(self.tenant, event.id, "relay-b")
        )
        self.assertTrue(
            self.store.mark_outbox_published(self.tenant, event.id, "relay-a")
        )
        self.assertEqual(
            (),
            self.store.claim_outbox(
                self.tenant, "relay-b", limit=10, lease_seconds=30
            ),
        )

    def test_exhausted_outbox_event_is_dead_lettered(self) -> None:
        item = self._item("poison event")
        event = self._event(item)
        self.store.retain(item, event)
        self.store.claim_outbox(self.tenant, "relay-a", limit=1, lease_seconds=30)

        released = self.store.release_outbox(
            self.tenant,
            event.id,
            "relay-a",
            error="poison",
            max_attempts=1,
            retry_delay_seconds=5,
        )

        self.assertTrue(released)
        self.assertEqual(
            (),
            self.store.claim_outbox(
                self.tenant, "relay-b", limit=10, lease_seconds=30
            ),
        )
        with self.store._connection() as connection:
            self.store._set_tenant(connection, self.tenant)
            row = connection.execute(
                """
                select last_error, dead_lettered_at
                from outbox_events where id = %s
                """,
                (event.id,),
            ).fetchone()
        self.assertEqual("poison", row["last_error"])
        self.assertIsNotNone(row["dead_lettered_at"])

    def test_consumer_processing_is_leased_and_completed_once(self) -> None:
        event_id = uuid4()

        first = self.store.claim_event_processing(
            self.tenant,
            event_id,
            "embed-v1",
            "worker-a",
            lease_seconds=30,
        )
        busy = self.store.claim_event_processing(
            self.tenant,
            event_id,
            "embed-v1",
            "worker-b",
            lease_seconds=30,
        )
        completed = self.store.complete_event_processing(
            self.tenant, event_id, "embed-v1", "worker-a"
        )
        duplicate = self.store.claim_event_processing(
            self.tenant,
            event_id,
            "embed-v1",
            "worker-b",
            lease_seconds=30,
        )

        self.assertEqual(ConsumerClaim.ACQUIRED, first)
        self.assertEqual(ConsumerClaim.BUSY, busy)
        self.assertTrue(completed)
        self.assertEqual(ConsumerClaim.COMPLETED, duplicate)

    def test_workspace_embedding_freshness_counts_active_heads_by_delivery_state(self) -> None:
        complete_item = self._item("completed embedding")
        complete_event = self._event(complete_item)
        self.store.retain(complete_item, complete_event)
        self.store.claim_outbox(self.tenant, "relay-complete", limit=1, lease_seconds=30)
        self.assertTrue(
            self.store.mark_outbox_published(self.tenant, complete_event.id, "relay-complete")
        )
        self.assertEqual(
            ConsumerClaim.ACQUIRED,
            self.store.claim_event_processing(
                self.tenant, complete_event.id, "embed-v1", "worker-complete", lease_seconds=30
            ),
        )
        self.assertTrue(
            self.store.complete_event_processing(
                self.tenant, complete_event.id, "embed-v1", "worker-complete"
            )
        )

        pending_item = self._item("pending embedding")
        self.store.retain(pending_item, self._event(pending_item))

        dead_item = self._item("dead letter embedding")
        dead_event = self._event(dead_item)
        self.store.retain(dead_item, dead_event)
        self.store.claim_outbox(self.tenant, "relay-dead", limit=10, lease_seconds=30)
        self.assertTrue(
            self.store.release_outbox(
                self.tenant,
                dead_event.id,
                "relay-dead",
                error="test dead letter",
                max_attempts=1,
                retry_delay_seconds=1,
            )
        )

        freshness = self.store.workspace_embedding_freshness(self.tenant, self.workspace)

        self.assertEqual(3, freshness.active_memory_count)
        self.assertTrue(freshness.stale)
        self.assertEqual(2, freshness.stale_memory_count)
        self.assertEqual(1, freshness.unpublished_memory_count)
        self.assertEqual(0, freshness.processing_memory_count)
        self.assertEqual(1, freshness.dead_letter_memory_count)
        self.assertEqual(0, freshness.missing_delivery_memory_count)


if __name__ == "__main__":
    unittest.main()
