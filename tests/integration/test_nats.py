from __future__ import annotations

import os
import unittest
from uuid import uuid4

from memory_plane.adapters.nats import NatsJetStreamSink
from memory_plane.adapters.postgres import PostgresMemoryLedger
from memory_plane.contracts.events import IntegrationEvent
from memory_plane.domain.models import MemoryItem, MemoryLayer, MemoryScope, Provenance
from memory_plane.services.consumer import IdempotentEventConsumer
from memory_plane.services.outbox import OutboxRelay
from memory_plane.workers.nats_consumer import NatsPullWorker

NATS_URL = os.getenv("UAM_TEST_NATS_URL")
DATABASE_URL = os.getenv("UAM_TEST_DATABASE_URL")


@unittest.skipUnless(NATS_URL, "set UAM_TEST_NATS_URL to run NATS tests")
class NatsJetStreamSinkTest(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_event_id_is_stored_once(self) -> None:
        import nats

        suffix = uuid4().hex.upper()
        stream = f"TEST_{suffix}"
        prefix = f"test.memory.{suffix.lower()}"
        sink = NatsJetStreamSink(NATS_URL or "", stream=stream, subject_prefix=prefix)
        event = IntegrationEvent(
            name="memory.retained.v1",
            tenant_id=uuid4(),
            workspace_id=uuid4(),
            payload={"memory_id": str(uuid4())},
        )
        await sink.connect()
        try:
            await sink.send(event)
            await sink.send(event)
            client = await nats.connect(NATS_URL)
            try:
                info = await client.jetstream().stream_info(stream)
                self.assertEqual(1, info.state.messages)
            finally:
                await client.drain()
        finally:
            await sink.close()


@unittest.skipUnless(
    NATS_URL and DATABASE_URL,
    "set UAM_TEST_NATS_URL and UAM_TEST_DATABASE_URL for relay tests",
)
class OutboxToNatsTest(unittest.IsolatedAsyncioTestCase):
    async def test_committed_event_is_relayed_and_acknowledged(self) -> None:
        tenant_id = uuid4()
        workspace_id = uuid4()
        store = PostgresMemoryLedger(DATABASE_URL or "")
        store.ensure_standalone_scope(
            tenant_id,
            workspace_id,
            server_name=f"relay-{tenant_id}",
            project_name="relay",
        )
        item = MemoryItem(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            layer=MemoryLayer.SEMANTIC,
            scope=MemoryScope.WORKSPACE,
            kind="fact",
            text="Relay this memory",
            provenance=Provenance(source_kind="test"),
        )
        event = IntegrationEvent(
            name="memory.retained.v1",
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            correlation_id=item.id,
            payload={"memory_id": str(item.id), "jobs": ["embed"]},
        )
        store.retain(item, event)
        suffix = uuid4().hex.upper()
        stream = f"RELAY_{suffix}"
        sink = NatsJetStreamSink(
            NATS_URL or "",
            stream=stream,
            subject_prefix=f"test.relay.{suffix.lower()}",
        )
        handled: list = []

        async def handler(message: IntegrationEvent) -> None:
            handled.append(message.id)

        await sink.connect()
        try:
            result = await OutboxRelay(
                store,
                sink,
                tenant_id=tenant_id,
                worker_id="integration-relay",
            ).run_once()
            self.assertEqual((1, 1, 0), (result.claimed, result.published, result.failed))
            self.assertEqual(
                (),
                store.claim_outbox(
                    tenant_id,
                    "second-relay",
                    limit=10,
                    lease_seconds=30,
                ),
            )
            worker = NatsPullWorker(
                NATS_URL or "",
                IdempotentEventConsumer(
                    store,
                    handler,
                    consumer=f"test-{suffix}",
                    worker_id="integration-consumer",
                ),
                durable=f"TEST_{suffix}",
                subject=f"test.relay.{suffix.lower()}.>",
                stream=stream,
            )
            await worker.connect()
            try:
                self.assertEqual(1, await worker.run_once())
                self.assertEqual([event.id], handled)
            finally:
                await worker.close()
        finally:
            await sink.close()
