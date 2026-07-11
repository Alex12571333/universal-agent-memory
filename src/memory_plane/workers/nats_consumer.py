"""JetStream pull worker wired to durable consumer idempotency."""

from __future__ import annotations

import json
from base64 import b64encode
from dataclasses import replace
from datetime import datetime
from hashlib import sha256
from typing import Any
from uuid import UUID

from memory_plane.contracts.events import IntegrationEvent
from memory_plane.services.consumer import IdempotentEventConsumer

DEFAULT_DLQ_MAX_BYTES = 134_217_728
DEFAULT_DLQ_MAX_AGE_SECONDS = 1_209_600


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
        max_deliveries: int = 8,
        retry_base_seconds: int = 2,
        retry_max_seconds: int = 60,
        dead_letter_stream: str = "MEMORY_DLQ",
        dead_letter_subject: str = "memory.dead_letters.embedding",
        dead_letter_max_bytes: int = DEFAULT_DLQ_MAX_BYTES,
        dead_letter_max_age_seconds: int = DEFAULT_DLQ_MAX_AGE_SECONDS,
    ) -> None:
        if not durable.strip():
            raise ValueError("durable consumer name must not be empty")
        if max_deliveries < 1:
            raise ValueError("max_deliveries must be positive")
        if retry_base_seconds < 1 or retry_max_seconds < retry_base_seconds:
            raise ValueError("invalid retry delay bounds")
        if dead_letter_max_bytes < 1 or dead_letter_max_age_seconds < 1:
            raise ValueError("NATS dead-letter limits must be positive")
        self._url = url
        self._consumer = consumer
        self._durable = durable
        self._subject = subject
        self._stream = stream
        self._max_deliveries = max_deliveries
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds
        self._dead_letter_stream = dead_letter_stream
        self._dead_letter_subject = dead_letter_subject
        self._dead_letter_max_bytes = dead_letter_max_bytes
        self._dead_letter_max_age_seconds = dead_letter_max_age_seconds
        self._client: Any = None
        self._subscription: Any = None
        self._jetstream: Any = None

    async def connect(self) -> None:
        """Create or resume a durable pull consumer."""
        try:
            import nats
            from nats.js.errors import NotFoundError
        except ImportError as error:
            raise RuntimeError('NATS support is not installed; use ".[nats]"') from error
        self._client = await nats.connect(self._url)
        self._jetstream = self._client.jetstream()
        try:
            existing = await self._jetstream.stream_info(self._dead_letter_stream)
            if (
                existing.config.max_bytes != self._dead_letter_max_bytes
                or existing.config.max_age != self._dead_letter_max_age_seconds
            ):
                await self._jetstream.update_stream(
                    config=replace(
                        existing.config,
                        max_bytes=self._dead_letter_max_bytes,
                        max_age=self._dead_letter_max_age_seconds,
                    )
                )
        except NotFoundError:
            await self._jetstream.add_stream(
                name=self._dead_letter_stream,
                subjects=["memory.dead_letters.>"],
                storage="file",
                max_bytes=self._dead_letter_max_bytes,
                max_age=self._dead_letter_max_age_seconds,
            )
        self._subscription = await self._jetstream.pull_subscribe(
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
                    await self._retry_or_dead_letter(message, "consumer_busy")
                    continue
                await message.ack_sync()
                acknowledged += 1
            except Exception as error:  # noqa: BLE001 - transport must retain failures for replay.
                await self._retry_or_dead_letter(message, f"{type(error).__name__}: {error}")
        return acknowledged

    async def _retry_or_dead_letter(self, message: Any, error: str) -> None:
        """Bound poison-message delivery and atomically preserve a replay record first."""
        attempts = _delivery_attempts(message)
        if attempts >= self._max_deliveries:
            try:
                await self._publish_dead_letter(message, attempts, error)
            except Exception:  # noqa: BLE001 - never terminally drop without a DLQ copy.
                await message.nak(delay=self._retry_max_seconds)
                return
            await message.term()
            return
        delay = min(self._retry_max_seconds, self._retry_base_seconds * (2 ** (attempts - 1)))
        await message.nak(delay=delay)

    async def _publish_dead_letter(self, message: Any, attempts: int, error: str) -> None:
        if self._jetstream is None:
            raise RuntimeError("NATS JetStream is not connected")
        raw = bytes(message.data)
        body = json.dumps(
            {
                "source_stream": self._stream,
                "source_subject": getattr(message, "subject", ""),
                "consumer": self._durable,
                "deliveries": attempts,
                "error": error[:2000],
                "event_base64": b64encode(raw).decode(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        message_id = sha256(raw + str(attempts).encode()).hexdigest()
        await self._jetstream.publish(
            self._dead_letter_subject,
            body,
            stream=self._dead_letter_stream,
            headers={"Nats-Msg-Id": message_id},
        )

    async def close(self) -> None:
        """Drain the NATS connection after current acknowledgements."""
        if self._client is not None:
            await self._client.drain()
            self._client = None
            self._subscription = None
            self._jetstream = None

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


def _delivery_attempts(message: Any) -> int:
    metadata = getattr(message, "metadata", None)
    delivered = getattr(metadata, "num_delivered", 1)
    return max(1, int(delivered))
