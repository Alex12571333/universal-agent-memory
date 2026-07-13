from __future__ import annotations

import unittest
from types import SimpleNamespace

from memory_plane.workers.nats_consumer import NatsPullWorker


class Message:
    def __init__(self, attempts: int) -> None:
        self.metadata = SimpleNamespace(num_delivered=attempts)
        self.data = b'{"id":"event"}'
        self.subject = "memory.events.memory.retained.v1"
        self.delays: list[int] = []
        self.terminated = False

    async def nak(self, *, delay: int) -> None:
        self.delays.append(delay)

    async def term(self) -> None:
        self.terminated = True


class NatsPullWorkerTest(unittest.IsolatedAsyncioTestCase):
    async def test_empty_pull_with_asyncio_timeout_is_not_a_worker_failure(self) -> None:
        worker = NatsPullWorker("nats://example", consumer=object(), durable="test")
        worker._subscription = AsyncioTimeoutSubscription()

        self.assertEqual(0, await worker.run_once())

    async def test_uses_capped_backoff_before_terminal_delivery(self) -> None:
        worker = NatsPullWorker(
            "nats://example",
            consumer=object(),
            durable="test",
            max_deliveries=4,
            retry_base_seconds=2,
            retry_max_seconds=5,
        )
        retry = Message(attempts=3)
        poison = Message(attempts=4)

        await worker._retry_or_dead_letter(retry, "failed")
        worker._jetstream = DeadLetterJetStream()
        await worker._retry_or_dead_letter(poison, "failed")

        self.assertEqual([5], retry.delays)
        self.assertFalse(retry.terminated)
        self.assertTrue(poison.terminated)
        self.assertEqual([], poison.delays)
        self.assertEqual(1, len(worker._jetstream.published))


class DeadLetterJetStream:
    def __init__(self) -> None:
        self.published: list[tuple] = []

    async def publish(self, *args, **kwargs) -> None:
        self.published.append((args, kwargs))


class AsyncioTimeoutSubscription:
    async def fetch(self, *args, **kwargs) -> list[object]:
        raise TimeoutError
