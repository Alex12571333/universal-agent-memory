"""NATS JetStream transport for durable outbox publication."""

from __future__ import annotations

import json
from typing import Any

from memory_plane.contracts.events import IntegrationEvent


class NatsJetStreamSink:
    """Publish versioned events after JetStream persistence acknowledgement."""

    def __init__(
        self,
        url: str,
        *,
        stream: str = "MEMORY_EVENTS",
        subject_prefix: str = "memory.events",
    ) -> None:
        if not url.strip():
            raise ValueError("NATS URL must not be empty")
        self._url = url
        self._stream = stream
        self._subject_prefix = subject_prefix.rstrip(".")
        self._client: Any = None
        self._jetstream: Any = None

    async def connect(self) -> None:
        """Connect and idempotently ensure the file-backed event stream."""
        try:
            import nats
            from nats.js.errors import NotFoundError
        except ImportError as error:
            raise RuntimeError('NATS support is not installed; use ".[nats]"') from error

        self._client = await nats.connect(self._url)
        self._jetstream = self._client.jetstream()
        try:
            await self._jetstream.stream_info(self._stream)
        except NotFoundError:
            await self._jetstream.add_stream(
                name=self._stream,
                subjects=[f"{self._subject_prefix}.>"],
                storage="file",
            )

    async def send(self, event: IntegrationEvent) -> None:
        """Publish with event ID deduplication and wait for the server ack."""
        if self._jetstream is None:
            raise RuntimeError("NATS sink is not connected")
        body = json.dumps(
            {
                "id": str(event.id),
                "name": event.name,
                "tenant_id": str(event.tenant_id),
                "workspace_id": str(event.workspace_id),
                "correlation_id": (
                    None if event.correlation_id is None else str(event.correlation_id)
                ),
                "occurred_at": event.occurred_at.isoformat(),
                "payload": event.payload,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        subject = f"{self._subject_prefix}.{event.name}"
        await self._jetstream.publish(
            subject,
            body,
            stream=self._stream,
            headers={"Nats-Msg-Id": str(event.id)},
        )

    async def close(self) -> None:
        """Drain the client so acknowledged publications leave the socket."""
        if self._client is not None:
            await self._client.drain()
            self._client = None
            self._jetstream = None
