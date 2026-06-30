from __future__ import annotations

import os
import unittest
from dataclasses import replace
from uuid import uuid4

from memory_plane.adapters.postgres import PostgresMemoryLedger
from memory_plane.contracts.events import IntegrationEvent
from memory_plane.domain.models import MemoryItem, MemoryLayer, MemoryScope, Provenance

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


if __name__ == "__main__":
    unittest.main()
