"""Dependency-free synchronous HTTP client."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Protocol
from uuid import uuid4

from uam_client.errors import (
    AuthenticationError,
    ConflictError,
    InvalidRequestError,
    MemoryServerError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    ServiceUnavailableError,
)
from uam_client.models import (
    CompiledContext,
    IdentityProvisionRequest,
    IdentityProvisionResponse,
    IngestTextRequest,
    IngestTextResponse,
    MemoryResult,
    RecallRequest,
    RecallResponse,
    RetainRequest,
    RetainResponse,
    RetryPolicy,
)


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: dict[str, Any]
    headers: Mapping[str, str]


class Transport(Protocol):
    def send(
        self,
        method: str,
        url: str,
        body: dict[str, Any] | None,
        headers: Mapping[str, str],
        timeout: float,
    ) -> HttpResponse: ...


class UrllibTransport:
    def send(
        self,
        method: str,
        url: str,
        body: dict[str, Any] | None,
        headers: Mapping[str, str],
        timeout: float,
    ) -> HttpResponse:
        payload = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            url, data=payload, headers=dict(headers), method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return HttpResponse(
                    status=response.status,
                    body=_decode(response.read()),
                    headers=dict(response.headers.items()),
                )
        except urllib.error.HTTPError as error:
            return HttpResponse(
                status=error.code,
                body=_decode(error.read()),
                headers=dict(error.headers.items()),
            )


class MemoryClient:
    def __init__(
        self,
        base_url: str = "http://localhost:8080",
        *,
        api_key: str | None = None,
        timeout: float = 10.0,
        retry: RetryPolicy | None = None,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._retry = retry or RetryPolicy()
        self._transport = transport or UrllibTransport()
        self._sleep = sleep

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health", None)

    def retain(self, request: RetainRequest) -> RetainResponse:
        if request.idempotency_key is None:
            request = replace(request, idempotency_key=str(uuid4()))
        value = self._request("POST", "/v1/memory/retain", request.to_dict())
        return RetainResponse(
            id=value["id"],
            created=value["created"],
            queued_event_ids=tuple(value["queued_event_ids"]),
        )

    def recall(self, request: RecallRequest) -> RecallResponse:
        value = self._request("POST", "/v1/memory/recall", request.to_dict())
        context = value["context"]
        return RecallResponse(
            results=tuple(MemoryResult(**row) for row in value["results"]),
            sources_used=tuple(value["sources_used"]),
            context=CompiledContext(
                **{**context, "trace_ids": tuple(context["trace_ids"])}
            ),
        )

    def ingest_text(self, request: IngestTextRequest) -> IngestTextResponse:
        value = self._request("POST", "/v1/ingest/text", request.to_dict())
        return IngestTextResponse(
            document_checksum=value["document_checksum"],
            memory_ids=tuple(value["memory_ids"]),
            created_count=value["created_count"],
        )

    def provision_identity(
        self,
        request: IdentityProvisionRequest,
    ) -> IdentityProvisionResponse:
        """Provision stable IDs with an operator-scoped client."""
        value = self._request("POST", "/v1/identities/provision", request.to_dict())
        return IdentityProvisionResponse(agent=value["agent"], thread=value["thread"])

    def _request(
        self, method: str, path: str, body: dict[str, Any] | None
    ) -> dict[str, Any]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        for attempt in range(self._retry.max_retries + 1):
            try:
                response = self._transport.send(
                    method,
                    f"{self._base_url}{path}",
                    body,
                    headers,
                    self._timeout,
                )
            except OSError as error:
                if attempt == self._retry.max_retries:
                    raise ServiceUnavailableError(str(error)) from error
                self._sleep(self._delay(attempt, None))
                continue
            if 200 <= response.status < 300:
                return response.body
            if (
                response.status in self._retry.retry_statuses
                and attempt < self._retry.max_retries
            ):
                self._sleep(self._delay(attempt, response.headers.get("Retry-After")))
                continue
            _raise_http_error(response)
        raise AssertionError("retry loop must return or raise")

    def _delay(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass
        return float(self._retry.base_delay_seconds * (2**attempt))


def _decode(payload: bytes) -> dict[str, Any]:
    if not payload:
        return {}
    value = json.loads(payload)
    return value if isinstance(value, dict) else {"value": value}


def _raise_http_error(response: HttpResponse) -> None:
    detail = response.body.get("detail", f"HTTP {response.status}")
    message = detail if isinstance(detail, str) else json.dumps(detail)
    errors: dict[int, type[MemoryServerError]] = {
        400: InvalidRequestError,
        401: AuthenticationError,
        403: PermissionDeniedError,
        404: NotFoundError,
        409: ConflictError,
        422: InvalidRequestError,
        429: RateLimitError,
    }
    error_type = errors.get(response.status)
    if error_type is None:
        error_type = ServiceUnavailableError if response.status >= 500 else MemoryServerError
    raise error_type(message, status=response.status)
