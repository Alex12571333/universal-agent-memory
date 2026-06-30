from __future__ import annotations

from collections.abc import Mapping

import pytest
from uam_client import InvalidRequestError, MemoryClient, RetainRequest, RetryPolicy
from uam_client.client import HttpResponse


class FakeTransport:
    def __init__(self, responses: list[HttpResponse]) -> None:
        self.responses = responses
        self.bodies: list[dict | None] = []
        self.headers: list[Mapping[str, str]] = []

    def send(
        self,
        method: str,
        url: str,
        body: dict | None,
        headers: Mapping[str, str],
        timeout: float,
    ) -> HttpResponse:
        self.bodies.append(body)
        self.headers.append(headers)
        return self.responses.pop(0)


def test_retain_reuses_generated_idempotency_key_across_retry() -> None:
    transport = FakeTransport(
        [
            HttpResponse(503, {"detail": "busy"}, {}),
            HttpResponse(
                201,
                {"id": "memory-1", "created": True, "queued_event_ids": ["event-1"]},
                {},
            ),
        ]
    )
    client = MemoryClient(
        transport=transport,
        retry=RetryPolicy(max_retries=1, base_delay_seconds=0),
        sleep=lambda seconds: None,
    )

    result = client.retain(RetainRequest(text="Remember this"))

    assert result.id == "memory-1"
    assert transport.bodies[0]["idempotency_key"]
    assert transport.bodies[0]["idempotency_key"] == transport.bodies[1]["idempotency_key"]


def test_validation_error_is_typed_and_not_retried() -> None:
    transport = FakeTransport([HttpResponse(422, {"detail": "invalid text"}, {})])

    with pytest.raises(InvalidRequestError) as caught:
        MemoryClient(transport=transport).retain(RetainRequest(text=""))

    assert caught.value.status == 422
    assert len(transport.bodies) == 1


def test_retry_after_header_controls_delay() -> None:
    delays: list[float] = []
    transport = FakeTransport(
        [
            HttpResponse(429, {"detail": "slow down"}, {"Retry-After": "2"}),
            HttpResponse(
                201,
                {"id": "memory-1", "created": True, "queued_event_ids": []},
                {},
            ),
        ]
    )

    MemoryClient(
        transport=transport,
        retry=RetryPolicy(max_retries=1),
        sleep=delays.append,
    ).retain(RetainRequest(text="Retry safely", idempotency_key="stable"))

    assert delays == [2.0]


def test_api_key_is_sent_as_bearer_token() -> None:
    transport = FakeTransport([HttpResponse(200, {"status": "ok"}, {})])

    MemoryClient(api_key="secret", transport=transport).health()

    assert transport.headers[0]["Authorization"] == "Bearer secret"
