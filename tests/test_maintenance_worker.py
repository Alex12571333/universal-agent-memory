from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

from memory_plane.contracts.events import IntegrationEvent
from memory_plane.workers import maintenance_main


def test_maintenance_handler_reflects_only_requested_jobs(monkeypatch) -> None:
    reflected: list[tuple[object, object]] = []
    captured: dict[str, object] = {}
    tenant_id, workspace_id = uuid4(), uuid4()

    class FakeWorker:
        def __init__(self, _url, consumer, **kwargs):
            captured["consumer"] = consumer
            captured["kwargs"] = kwargs

        async def connect(self):
            raise RuntimeError("stop after wiring")

        async def close(self):
            return None

    container = SimpleNamespace(
        reflection=SimpleNamespace(
            reflect=lambda tenant, workspace: reflected.append((tenant, workspace))
        ),
        store=SimpleNamespace(close=lambda: None),
    )
    monkeypatch.setenv("UAM_SERVER_ID", str(uuid4()))
    monkeypatch.setenv("UAM_DATABASE_URL", "postgresql://example")
    monkeypatch.setattr(
        maintenance_main, "build_postgres_container", lambda *_args, **_kwargs: container
    )
    monkeypatch.setattr(maintenance_main, "NatsPullWorker", FakeWorker)

    try:
        asyncio.run(maintenance_main.run())
    except RuntimeError as error:
        assert str(error) == "stop after wiring"

    handler = captured["consumer"]._handler
    asyncio.run(
        handler(
            IntegrationEvent(
                name="memory.retained.v1",
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                payload={"jobs": ["embed"]},
            )
        )
    )
    assert reflected == []
    asyncio.run(
        handler(
            IntegrationEvent(
                name="memory.retained.v1",
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                payload={"jobs": ["reflect"]},
            )
        )
    )
    assert reflected == [(tenant_id, workspace_id)]
    assert captured["kwargs"]["durable"] == "MAINTENANCE_REFLECT_WORKER"
