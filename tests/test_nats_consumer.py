from __future__ import annotations

import unittest
from types import SimpleNamespace

from memory_plane.workers.nats_consumer import NatsPullWorker


class Message:
    def __init__(self, attempts: int) -> None:
        self.metadata = SimpleNamespace(num_delivered=attempts)
        self.delays: list[int] = []
        self.terminated = False

    async def nak(self, *, delay: int) -> None:
        self.delays.append(delay)

    async def term(self) -> None:
        self.terminated = True


class NatsPullWorkerTest(unittest.IsolatedAsyncioTestCase):
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

        await worker._retry_or_term(retry)
        await worker._retry_or_term(poison)

        self.assertEqual([5], retry.delays)
        self.assertFalse(retry.terminated)
        self.assertTrue(poison.terminated)
        self.assertEqual([], poison.delays)
