"""Concurrent load smoke gate for a running Obelisk Memory server.

This is not a full benchmark. It is a production-release guard that verifies the
server can handle parallel agent-style retain/recall traffic, preserve recall
correctness, and keep outbox/worker backlog within configured thresholds.
"""

from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

from memory_plane.build_info import require_status_build_identity
from memory_plane.config.secrets import read_secret_env

DEFAULT_TENANT = UUID("00000000-0000-0000-0000-000000000001")
DEFAULT_WORKSPACE = UUID("00000000-0000-0000-0000-000000000002")


class LoadClient(Protocol):
    """Small protocol for real HTTP and test clients."""

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        expect_status: int = 200,
        auth: bool = True,
    ) -> Any:
        """Return decoded JSON or text."""
        ...


@dataclass(frozen=True, slots=True)
class ApiClient:
    """Minimal stdlib HTTP client."""

    base_url: str
    api_key: str | None = None
    timeout_seconds: int = 30

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        expect_status: int = 200,
        auth: bool = True,
    ) -> Any:
        headers = {"Content-Type": "application/json"}
        if auth and self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(
            f"{self.base_url.rstrip('/')}{path}",
            data=None if body is None else json.dumps(body).encode("utf-8"),
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310
                status = response.status
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            status = exc.code
            raw = exc.read().decode("utf-8", errors="replace")
        if status != expect_status:
            raise AssertionError(f"{method} {path}: expected {expect_status}, got {status}: {raw}")
        if not raw:
            return None
        stripped = raw.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            return json.loads(stripped)
        return raw


@dataclass(frozen=True, slots=True)
class LoadConfig:
    """Runtime load smoke settings."""

    base_url: str = "http://127.0.0.1:6798"
    api_key: str | None = None
    tenant_id: UUID = DEFAULT_TENANT
    workspace_id: UUID = DEFAULT_WORKSPACE
    agents: int = 8
    operations_per_agent: int = 5
    timeout_seconds: int = 30
    max_error_rate: float = 0.0
    max_retain_p95_ms: int = 1000
    max_recall_p95_ms: int = 1500
    max_outbox_pending: int = 100
    max_outbox_lag_seconds: int = 300
    run_id: str = field(default_factory=lambda: uuid4().hex[:12])


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One load smoke check."""

    name: str
    ok: bool
    detail: str


@dataclass(frozen=True, slots=True)
class LoadReport:
    """Machine-readable load smoke report."""

    format: str
    ok: bool
    generated_at: str
    build: dict[str, str]
    base_url: str
    tenant_id: str
    workspace_id: str
    run_id: str
    agents: int
    operations_per_agent: int
    total_operations: int
    retain_p95_ms: int
    recall_p95_ms: int
    error_rate: float
    checks: tuple[CheckResult, ...]


@dataclass(frozen=True, slots=True)
class OperationResult:
    """One retain/recall operation result."""

    ok: bool
    retain_ms: int
    recall_ms: int
    detail: str = ""


def run_load_smoke(config: LoadConfig, client: LoadClient | None = None) -> LoadReport:
    """Run concurrent retain/recall checks and return a release evidence report."""
    api = client or ApiClient(config.base_url, config.api_key, config.timeout_seconds)
    checks: list[CheckResult] = []
    checks.append(_check("health", lambda: _check_health(api)))
    build, build_check = _capture_build_identity(api)
    checks.append(build_check)

    results = _run_parallel_operations(api, config)
    total = len(results)
    failures = [result for result in results if not result.ok]
    error_rate = len(failures) / max(1, total)
    retain_p95 = _p95([result.retain_ms for result in results if result.ok])
    recall_p95 = _p95([result.recall_ms for result in results if result.ok])
    checks.extend(
        [
            CheckResult(
                "concurrent-retain-recall",
                not failures,
                "all operations recalled their own marker"
                if not failures
                else f"{len(failures)} failed operations; first={failures[0].detail}",
            ),
            CheckResult(
                "error-rate",
                error_rate <= config.max_error_rate,
                f"{error_rate:.4f} <= {config.max_error_rate:.4f}",
            ),
            CheckResult(
                "retain-p95",
                retain_p95 <= config.max_retain_p95_ms,
                f"{retain_p95}ms <= {config.max_retain_p95_ms}ms",
            ),
            CheckResult(
                "recall-p95",
                recall_p95 <= config.max_recall_p95_ms,
                f"{recall_p95}ms <= {config.max_recall_p95_ms}ms",
            ),
        ]
    )
    checks.append(_check("metrics-backlog", lambda: _check_metrics(api, config)))

    ok = all(check.ok for check in checks)
    return LoadReport(
        format="obelisk-load-smoke-v1",
        ok=ok,
        generated_at=datetime.now(UTC).isoformat(),
        build=build,
        base_url=config.base_url,
        tenant_id=str(config.tenant_id),
        workspace_id=str(config.workspace_id),
        run_id=config.run_id,
        agents=config.agents,
        operations_per_agent=config.operations_per_agent,
        total_operations=total,
        retain_p95_ms=retain_p95,
        recall_p95_ms=recall_p95,
        error_rate=round(error_rate, 6),
        checks=tuple(checks),
    )


def _run_parallel_operations(api: LoadClient, config: LoadConfig) -> tuple[OperationResult, ...]:
    max_workers = max(1, config.agents)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [
            pool.submit(_run_operation, api, config, agent_index, op_index)
            for agent_index in range(config.agents)
            for op_index in range(config.operations_per_agent)
        ]
        return tuple(future.result() for future in as_completed(futures))


def _run_operation(
    api: LoadClient,
    config: LoadConfig,
    agent_index: int,
    op_index: int,
) -> OperationResult:
    marker = f"LOAD-{config.run_id}-{agent_index}-{op_index}"
    agent_id = uuid4()
    body = {
        "tenant_id": str(config.tenant_id),
        "workspace_id": str(config.workspace_id),
        "agent_id": str(agent_id),
        "layer": "semantic",
        "scope": "workspace",
        "kind": "load_smoke_marker",
        "text": f"{marker}: concurrent agent load smoke retained durable memory.",
        "labels": ["load-smoke", f"agent-{agent_index}"],
        "idempotency_key": f"load-smoke:{config.run_id}:{agent_index}:{op_index}",
    }
    try:
        started = time.perf_counter()
        api.request("POST", "/v1/memory/retain", body, expect_status=201)
        retain_ms = _elapsed_ms(started)
        started = time.perf_counter()
        recall = api.request(
            "POST",
            "/v1/memory/recall",
            {
                "tenant_id": str(config.tenant_id),
                "workspace_id": str(config.workspace_id),
                "query": f"find load smoke marker {marker}",
                "top_k": 5,
            },
        )
        recall_ms = _elapsed_ms(started)
        if marker not in json.dumps(recall, ensure_ascii=False):
            return OperationResult(False, retain_ms, recall_ms, f"marker not recalled: {marker}")
        return OperationResult(True, retain_ms, recall_ms)
    except Exception as exc:  # noqa: BLE001 - report every load-smoke failure.
        return OperationResult(False, 0, 0, f"{type(exc).__name__}: {exc}")


def _check_health(api: LoadClient) -> None:
    health = api.request("GET", "/health", auth=False)
    if not isinstance(health, dict) or health.get("status") != "ok":
        raise AssertionError(f"health failed: {health!r}")


def _capture_build_identity(api: LoadClient) -> tuple[dict[str, str], CheckResult]:
    try:
        status = api.request("GET", "/v1/system/status")
        identity = require_status_build_identity(status)
    except Exception as exc:  # noqa: BLE001 - evidence report captures the failure.
        return {}, CheckResult("build-identity", False, f"{type(exc).__name__}: {exc}")
    return identity, CheckResult(
        "build-identity",
        True,
        (
            f"version={identity['version']} source_commit={identity['source_commit']} "
            f"image_digest={identity['image_digest']} deployment_id={identity['deployment_id']}"
        ),
    )


def _check_metrics(api: LoadClient, config: LoadConfig) -> None:
    raw = api.request("GET", "/metrics")
    metrics = _parse_prometheus(str(raw))
    pending = metrics.get("uam_outbox_pending_total", 0.0)
    lag = metrics.get("uam_outbox_lag_seconds", 0.0)
    dead = metrics.get("uam_outbox_dead_letter_total", 0.0)
    if pending > config.max_outbox_pending:
        raise AssertionError(f"outbox pending {pending} > {config.max_outbox_pending}")
    if lag > config.max_outbox_lag_seconds:
        raise AssertionError(f"outbox lag {lag} > {config.max_outbox_lag_seconds}")
    if dead > 0:
        raise AssertionError(f"outbox dead letters {dead} > 0")


def _parse_prometheus(text: str) -> dict[str, float]:
    metrics: dict[str, float] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or " " not in line:
            continue
        name, value = line.split(None, 1)
        try:
            metrics[name] = float(value)
        except ValueError:
            continue
    return metrics


def _check(name: str, fn: Any) -> CheckResult:
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - release report should include every failure.
        return CheckResult(name=name, ok=False, detail=f"{type(exc).__name__}: {exc}")
    return CheckResult(name=name, ok=True, detail="ok")


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _p95(values: list[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * 0.95))))
    return ordered[index]


def write_report(report: LoadReport, path: Path) -> None:
    """Write a JSON report."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(report), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:6798")
    parser.add_argument("--api-key", default=read_secret_env("UAM_API_KEY"))
    parser.add_argument("--tenant-id", type=UUID, default=DEFAULT_TENANT)
    parser.add_argument("--workspace-id", type=UUID, default=DEFAULT_WORKSPACE)
    parser.add_argument("--agents", type=int, default=8)
    parser.add_argument("--operations-per-agent", type=int, default=5)
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument("--max-error-rate", type=float, default=0.0)
    parser.add_argument("--max-retain-p95-ms", type=int, default=1000)
    parser.add_argument("--max-recall-p95-ms", type=int, default=1500)
    parser.add_argument("--max-outbox-pending", type=int, default=100)
    parser.add_argument("--max-outbox-lag-seconds", type=int, default=300)
    parser.add_argument("--json-report", type=Path)
    parser.add_argument("--run-id", default=uuid4().hex[:12])
    args = parser.parse_args()
    if args.agents < 1 or args.operations_per_agent < 1:
        parser.error("--agents and --operations-per-agent must be positive")

    report = run_load_smoke(
        LoadConfig(
            base_url=args.base_url,
            api_key=args.api_key,
            tenant_id=args.tenant_id,
            workspace_id=args.workspace_id,
            agents=args.agents,
            operations_per_agent=args.operations_per_agent,
            timeout_seconds=args.timeout_seconds,
            max_error_rate=args.max_error_rate,
            max_retain_p95_ms=args.max_retain_p95_ms,
            max_recall_p95_ms=args.max_recall_p95_ms,
            max_outbox_pending=args.max_outbox_pending,
            max_outbox_lag_seconds=args.max_outbox_lag_seconds,
            run_id=args.run_id,
        )
    )
    if args.json_report:
        write_report(report, args.json_report)
    print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
