"""NATS JetStream transport for durable outbox publication."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from memory_plane.contracts.events import IntegrationEvent

DEFAULT_STREAM_MAX_BYTES = 536_870_912
DEFAULT_STREAM_MAX_AGE_SECONDS = 604_800


class NatsJetStreamSink:
    """Publish versioned events after JetStream persistence acknowledgement."""

    def __init__(
        self,
        url: str,
        *,
        stream: str = "MEMORY_EVENTS",
        subject_prefix: str = "memory.events",
        max_bytes: int = DEFAULT_STREAM_MAX_BYTES,
        max_age_seconds: int = DEFAULT_STREAM_MAX_AGE_SECONDS,
    ) -> None:
        if not url.strip():
            raise ValueError("NATS URL must not be empty")
        if max_bytes < 1 or max_age_seconds < 1:
            raise ValueError("NATS stream limits must be positive")
        self._url = url
        self._stream = stream
        self._subject_prefix = subject_prefix.rstrip(".")
        self._max_bytes = max_bytes
        self._max_age_seconds = max_age_seconds
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
            existing = await self._jetstream.stream_info(self._stream)
            if (
                existing.config.max_bytes != self._max_bytes
                or existing.config.max_age != self._max_age_seconds
            ):
                await self._jetstream.update_stream(
                    config=replace(
                        existing.config,
                        max_bytes=self._max_bytes,
                        max_age=self._max_age_seconds,
                    )
                )
        except NotFoundError:
            await self._jetstream.add_stream(
                name=self._stream,
                subjects=[f"{self._subject_prefix}.>"],
                storage="file",
                max_bytes=self._max_bytes,
                max_age=self._max_age_seconds,
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
