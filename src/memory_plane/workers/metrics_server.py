"""Minimal private Prometheus endpoint for long-running worker processes."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from memory_plane.services.metrics import render_prometheus


class WorkerMetricsServer:
    """Expose process-local worker counters without adding an HTTP framework."""

    def __init__(self, collector: Callable[[], dict[str, float | int]]) -> None:
        self._collector = collector
        self._server: asyncio.AbstractServer | None = None

    async def start(self, host: str, port: int) -> None:
        """Start a private HTTP server for ``/metrics`` and ``/healthz``."""
        self._server = await asyncio.start_server(self._handle, host, port)

    async def close(self) -> None:
        """Stop accepting scrape requests."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            request_line = (await reader.readline()).decode("ascii", "replace").strip()
            while await reader.readline() not in {b"\r\n", b"\n", b""}:
                pass
            method, path, _ = request_line.split(" ", 2)
            response = self.response(method, path)
        except (ValueError, UnicodeError):
            response = _response(400, "Bad Request", "bad request\n")
        writer.write(response)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    def response(self, method: str, path: str) -> bytes:
        """Build a response; kept separate so endpoint behavior is unit-testable."""
        if method != "GET":
            return _response(405, "Method Not Allowed", "method not allowed\n")
        if path == "/healthz":
            return _response(200, "OK", "ok\n")
        if path == "/metrics":
            return _response(
                200,
                "OK",
                render_prometheus(self._collector()),
                content_type="text/plain; version=0.0.4; charset=utf-8",
            )
        return _response(404, "Not Found", "not found\n")


def _response(status: int, reason: str, body: str, *, content_type: str = "text/plain") -> bytes:
    encoded = body.encode()
    return (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(encoded)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode() + encoded
