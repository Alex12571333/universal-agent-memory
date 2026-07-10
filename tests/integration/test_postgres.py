from __future__ import annotations

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import uuid4

from memory_plane.adapters.postgres import (
    PostgresCheckpointStore,
    PostgresConflictReviewRepository,
    PostgresMemoryLedger,
)
from memory_plane.contracts.dto import RecallQuery
from memory_plane.contracts.events import ConsumerClaim, IntegrationEvent
from memory_plane.domain.checkpoint import Checkpoint, StaleRevisionError
from memory_plane.domain.conflict import ConflictReviewDecision, ConflictReviewStatus
from memory_plane.domain.identity import AgentIdentity
from memory_plane.domain.models import (
    MemoryItem,
    MemoryLayer,
    MemoryRevisionConflictError,
    MemoryScope,
    MemoryStatus,
    Provenance,
)
from memory_plane.services.conflicts import ConflictService

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


if __name__ == "__main__":
    unittest.main()
