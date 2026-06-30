from __future__ import annotations

import unittest
from uuid import uuid4

from memory_plane.contracts.events import ClaimedEvent, ConsumerClaim, IntegrationEvent
from memory_plane.services.consumer import IdempotentEventConsumer
from memory_plane.services.outbox import OutboxRelay


class FakeOutboxRepository:
    def __init__(self, rows: tuple[ClaimedEvent, ...]) -> None:
        self.rows = rows
        self.published: list = []
        self.released: list[tuple] = []

    def claim_outbox(self, tenant_id, worker_id, *, limit, lease_seconds):
        return self.rows[:limit]

    def mark_outbox_published(self, tenant_id, event_id, worker_id):
        self.published.append(event_id)
        return True

    def release_outbox(
        self, tenant_id, event_id, worker_id, *, error, max_attempts
    ):
        self.released.append((event_id, error, max_attempts))
        return True


class FakeSink:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.events: list[IntegrationEvent] = []

    async def send(self, event: IntegrationEvent) -> None:
        if self.fail:
            raise RuntimeError("transport unavailable")
        self.events.append(event)


class FakeProcessedRepository:
    def __init__(self, claims: list[ConsumerClaim]) -> None:
        self.claims = claims
        self.completed = 0
        self.released = 0

    def claim_event_processing(self, *args, **kwargs):
        return self.claims.pop(0)

    def complete_event_processing(self, *args, **kwargs):
        self.completed += 1
        return True

    def release_event_processing(self, *args, **kwargs):
        self.released += 1
        return True


def event() -> IntegrationEvent:
    return IntegrationEvent(
        name="memory.retained.v1",
        tenant_id=uuid4(),
        workspace_id=uuid4(),
        payload={"memory_id": str(uuid4())},
    )


class OutboxRelayTest(unittest.IsolatedAsyncioTestCase):
    async def test_acknowledges_only_after_sink_success(self) -> None:
        pending = event()
        repository = FakeOutboxRepository((ClaimedEvent(pending, attempts=1),))
        sink = FakeSink()
        relay = OutboxRelay(
            repository,
            sink,
            tenant_id=pending.tenant_id,
            worker_id="relay-a",
        )

        result = await relay.run_once()

        self.assertEqual((1, 1, 0), (result.claimed, result.published, result.failed))
        self.assertEqual([pending], sink.events)
        self.assertEqual([pending.id], repository.published)
        self.assertEqual([], repository.released)

    async def test_transport_failure_releases_without_acknowledging(self) -> None:
        pending = event()
        repository = FakeOutboxRepository((ClaimedEvent(pending, attempts=3),))
        relay = OutboxRelay(
            repository,
            FakeSink(fail=True),
            tenant_id=pending.tenant_id,
            worker_id="relay-a",
            max_attempts=4,
        )

        result = await relay.run_once()

        self.assertEqual((1, 0, 1), (result.claimed, result.published, result.failed))
        self.assertEqual([], repository.published)
        self.assertIn("transport unavailable", repository.released[0][1])

    async def test_consumer_skips_completed_duplicate(self) -> None:
        repository = FakeProcessedRepository([ConsumerClaim.COMPLETED])
        calls: list = []

        async def handler(message: IntegrationEvent) -> None:
            calls.append(message.id)

        result = await IdempotentEventConsumer(
            repository,
            handler,
            consumer="embed-v1",
            worker_id="worker-a",
        ).handle(event())

        self.assertTrue(result.duplicate)
        self.assertFalse(result.processed)
        self.assertEqual([], calls)

    async def test_consumer_releases_failed_handler_for_redelivery(self) -> None:
        repository = FakeProcessedRepository([ConsumerClaim.ACQUIRED])

        async def handler(message: IntegrationEvent) -> None:
            raise ValueError("bad embedding")

        consumer = IdempotentEventConsumer(
            repository,
            handler,
            consumer="embed-v1",
            worker_id="worker-a",
        )

        with self.assertRaisesRegex(ValueError, "bad embedding"):
            await consumer.handle(event())

        self.assertEqual(1, repository.released)
        self.assertEqual(0, repository.completed)
