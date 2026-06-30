"""JetStream pull worker wired to durable consumer idempotency."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from memory_plane.contracts.events import IntegrationEvent
from memory_plane.services.consumer import IdempotentEventConsumer


class NatsPullWorker:
    """Fetch, decode and explicitly ack/nak JetStream messages."""

    def __init__(
        self,
        url: str,
        consumer: IdempotentEventConsumer,
        *,
        durable: str,
        subject: str = "memory.events.>",
        stream: str = "MEMORY_EVENTS",
    ) -> None:
        if not durable.strip():
            raise ValueError("durable consumer name must not be empty")
        self._url = url
        self._consumer = consumer
        self._durable = durable
        self._subject = subject
        self._stream = stream
        self._client: Any = None
        self._subscription: Any = None

    async def connect(self) -> None:
        """Create or resume a durable pull consumer."""
        try:
            import nats
        except ImportError as error:
            raise RuntimeError('NATS support is not installed; use ".[nats]"') from error
        self._client = await nats.connect(self._url)
        self._subscription = await self._client.jetstream().pull_subscribe(
            self._subject,
            durable=self._durable,
            stream=self._stream,
        )

    async def run_once(self, *, batch_size: int = 10, timeout: float = 1.0) -> int:
        """Process one bounded batch and return its acknowledged count."""
        if self._subscription is None:
            raise RuntimeError("NATS worker is not connected")
        from nats.errors import TimeoutError as NatsTimeoutError

        try:
            messages = await self._subscription.fetch(batch_size, timeout=timeout)
        except NatsTimeoutError:
            return 0
        acknowledged = 0
        for message in messages:
            try:
                event = self.decode(message.data)
                result = await self._consumer.handle(event)
                if result.busy or (not result.processed and not result.duplicate):
                    await message.nak(delay=1)
                    continue
                await message.ack_sync()
                acknowledged += 1
            except Exception:
                await message.nak()
        return acknowledged

    async def close(self) -> None:
        """Drain the NATS connection after current acknowledgements."""
        if self._client is not None:
            await self._client.drain()
            self._client = None
            self._subscription = None

    @staticmethod
    def decode(data: bytes) -> IntegrationEvent:
        """Decode the stable JSON event envelope emitted by the sink."""
        value = json.loads(data)
        return IntegrationEvent(
            id=UUID(value["id"]),
            name=value["name"],
            tenant_id=UUID(value["tenant_id"]),
            workspace_id=UUID(value["workspace_id"]),
            correlation_id=(
                None
                if value.get("correlation_id") is None
                else UUID(value["correlation_id"])
            ),
            occurred_at=datetime.fromisoformat(value["occurred_at"]),
            payload=value["payload"],
        )
