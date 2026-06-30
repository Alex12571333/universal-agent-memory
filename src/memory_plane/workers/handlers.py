"""Router for independently deployable background processors."""

from __future__ import annotations

from collections.abc import Callable

from memory_plane.contracts.events import IntegrationEvent

JobHandler = Callable[[IntegrationEvent], None]


class RetainedEventRouter:
    """Dispatch requested derived jobs without coupling them to the write path."""

    def __init__(self, handlers: dict[str, JobHandler]) -> None:
        """Register independently testable handlers by job name."""
        self._handlers = handlers

    def handle(self, event: IntegrationEvent) -> tuple[str, ...]:
        """Dispatch a retained event and report jobs that had registered handlers."""
        if event.name != "memory.retained.v1":
            return ()
        completed: list[str] = []
        for job in event.payload.get("jobs", []):
            handler = self._handlers.get(str(job))
            if handler is None:
                continue
            handler(event)
            completed.append(str(job))
        return tuple(completed)
