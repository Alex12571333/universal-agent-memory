"""Live OpenClaw/Hermes soak checks for a running Obelisk Memory server.

The script exercises the production integration contract expected from native
agent plugins:

* each agent can retain durable memory;
* each agent can recall its own workspace memory;
* parallel agent writes remain idempotent;
* recall does not leak memories across workspaces.

It intentionally uses only the Python standard library so it can be copied to a
server or agent host and run as release evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

DEFAULT_TENANT = UUID("00000000-0000-0000-0000-000000000001")
OPENCLAW_WORKSPACE = UUID("00000000-0000-0000-0000-000000000014")
HERMES_WORKSPACE = UUID("00000000-0000-0000-0000-000000000015")
OPENCLAW_AGENT = UUID("00000000-0000-0000-0000-0000000000c1")
HERMES_AGENT = UUID("00000000-0000-0000-0000-0000000000e5")

_SOURCE_COMMIT_PATTERN = re.compile(r"[0-9a-fA-F]{40}")
_IMAGE_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-fA-F]{64}")
_PLACEHOLDER_BUILD_VALUES = frozenset(
    {"", "none", "null", "unknown", "unset", "not-set", "replace-me"}
)


def read_secret_env(name: str) -> str | None:
    """Read a direct or file-mounted secret without importing the server package.

    The evaluator is intentionally copied to agent hosts for release evidence;
    it must therefore remain executable with a stock Python installation.
    """
    value = os.getenv(name)
    if value:
        return value
    file_name = os.getenv(f"{name}_FILE")
    if not file_name:
        return None
    secret = Path(file_name).read_text(encoding="utf-8").strip()
    return secret or None


def require_status_build_identity(payload: object) -> dict[str, str]:
    """Validate the runtime identity embedded in a system-status response."""
    if not isinstance(payload, Mapping):
        raise ValueError("system status is missing or is not an object")
    build = payload.get("build")
    if not isinstance(build, Mapping):
        raise ValueError("build identity is missing or is not an object")
    fields = ("version", "source_commit", "image_digest", "deployment_id", "build_time")
    identity = {field: str(build.get(field, "")).strip() for field in fields}
    missing = [
        field
        for field, value in identity.items()
        if value.casefold() in _PLACEHOLDER_BUILD_VALUES
    ]
    if missing:
        raise ValueError(f"build identity has missing/placeholder fields: {', '.join(missing)}")
    if _SOURCE_COMMIT_PATTERN.fullmatch(identity["source_commit"]) is None:
        raise ValueError("build identity source_commit must be a 40-character git SHA")
    if _IMAGE_DIGEST_PATTERN.fullmatch(identity["image_digest"]) is None:
        raise ValueError("build identity image_digest must be sha256:<64 hex characters>")
    try:
        build_time = datetime.fromisoformat(identity["build_time"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("build identity build_time must be an ISO-8601 timestamp") from exc
    if build_time.tzinfo is None:
        raise ValueError("build identity build_time must include a timezone")
    status_version = str(payload.get("version", "")).strip()
    if status_version != identity["version"]:
        raise ValueError(
            "system status version does not match build identity "
            f"({status_version!r} != {identity['version']!r})"
        )
    return identity


class JsonClient(Protocol):
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
class AgentSpec:
    name: str
    workspace_id: UUID
    agent_id: UUID
    labels: tuple[str, ...]
    query: str


@dataclass(frozen=True, slots=True)
class SoakConfig:
    base_url: str = "http://127.0.0.1:6798"
    api_key: str | None = None
    tenant_id: UUID = DEFAULT_TENANT
    rounds: int = 3
    parallel: int = 2
    timeout_seconds: int = 30
    run_id: str = field(default_factory=lambda: uuid4().hex[:12])


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    ok: bool
    duration_ms: int
    detail: str = ""


@dataclass(frozen=True, slots=True)
class SoakReport:
    format: str
    ok: bool
    generated_at: str
    build: dict[str, str]
    base_url: str
    tenant_id: str
    run_id: str
    rounds: int
    parallel: int
    checks: tuple[CheckResult, ...]


AGENTS = (
    AgentSpec(
        name="openclaw",
        workspace_id=OPENCLAW_WORKSPACE,
        agent_id=OPENCLAW_AGENT,
        labels=("agent-soak", "openclaw"),
        query="OpenClaw lifecycle memory marker recall before run",
    ),
    AgentSpec(
        name="hermes",
        workspace_id=HERMES_WORKSPACE,
        agent_id=HERMES_AGENT,
        labels=("agent-soak", "hermes"),
        query="Hermes lifecycle memory marker prefetch before turn",
    ),
)


def run_soak(config: SoakConfig, client: JsonClient | None = None) -> SoakReport:
    api = client or ApiClient(config.base_url, config.api_key, config.timeout_seconds)
    checks: list[CheckResult] = []

    checks.append(_check("health", lambda: _check_health(api)))
    build, build_check = _capture_build_identity(api)
    checks.append(build_check)
    if config.api_key:
        checks.append(_check("auth-required", lambda: _check_auth_required(api)))

    max_workers = max(1, config.parallel)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [
            pool.submit(_run_agent_round, api, config, agent, round_index)
            for round_index in range(config.rounds)
            for agent in AGENTS
        ]
        for future in as_completed(futures):
            checks.extend(future.result())

    checks.append(
        _check(
            "cross-workspace-leakage",
            lambda: _check_cross_workspace_leakage(api, config),
        )
    )
    ok = all(check.ok for check in checks)
    return SoakReport(
        format="obelisk-agent-soak-v1",
        ok=ok,
        generated_at=datetime.now(UTC).isoformat(),
        build=build,
        base_url=config.base_url,
        tenant_id=str(config.tenant_id),
        run_id=config.run_id,
        rounds=config.rounds,
        parallel=config.parallel,
        checks=tuple(checks),
    )


def _run_agent_round(
    api: JsonClient,
    config: SoakConfig,
    agent: AgentSpec,
    round_index: int,
) -> list[CheckResult]:
    marker = _marker(config.run_id, agent.name, round_index)
    text = (
        f"{marker}: {agent.name} native lifecycle plugin retained a durable "
        "memory after a tool/model loop."
    )
    idempotency_key = f"agent-soak:{config.run_id}:{agent.name}:{round_index}"
    results = [
        _check(
            f"{agent.name}:retain:{round_index}",
            lambda: _retain_agent_memory(api, config, agent, text, idempotency_key),
        ),
        _check(
            f"{agent.name}:idempotent-retry:{round_index}",
            lambda: _retain_agent_memory(api, config, agent, text, idempotency_key),
        ),
        _check(
            f"{agent.name}:recall:{round_index}",
            lambda: _expect_recall_contains(api, config, agent, marker),
        ),
    ]
    return results


def _check_health(api: JsonClient) -> None:
    health = api.request("GET", "/health", auth=False)
    _expect(isinstance(health, dict), "health returned non-object response")
    _expect(health.get("status") == "ok", f"health status is {health.get('status')!r}")


def _capture_build_identity(api: JsonClient) -> tuple[dict[str, str], CheckResult]:
    started = time.perf_counter()
    try:
        status = api.request("GET", "/v1/system/status")
        identity = require_status_build_identity(status)
    except Exception as exc:  # noqa: BLE001 - evidence report captures the failure.
        return {}, CheckResult(
            name="build-identity",
            ok=False,
            duration_ms=_elapsed_ms(started),
            detail=f"{type(exc).__name__}: {exc}",
        )
    return identity, CheckResult(
        name="build-identity",
        ok=True,
        duration_ms=_elapsed_ms(started),
        detail=(
            f"version={identity['version']} source_commit={identity['source_commit']} "
            f"image_digest={identity['image_digest']} deployment_id={identity['deployment_id']}"
        ),
    )


def _check_auth_required(api: JsonClient) -> None:
    api.request(
        "POST",
        "/v1/memory/retain",
        {},
        expect_status=401,
        auth=False,
    )


def _retain_agent_memory(
    api: JsonClient,
    config: SoakConfig,
    agent: AgentSpec,
    text: str,
    idempotency_key: str,
) -> dict[str, Any]:
    data = api.request(
        "POST",
        "/v1/memory/retain",
        {
            "tenant_id": str(config.tenant_id),
            "workspace_id": str(agent.workspace_id),
            "agent_id": str(agent.agent_id),
            "layer": "episodic",
            "scope": "workspace",
            "kind": "agent_soak_marker",
            "text": text,
            "labels": list(agent.labels),
            "source_kind": f"{agent.name}-native-plugin",
            "idempotency_key": idempotency_key,
            "confidence": 0.91,
            "importance": 0.72,
        },
        expect_status=201,
    )
    _expect(isinstance(data, dict), "retain returned non-object response")
    _expect("id" in data, "retain response missing id")
    return data


def _expect_recall_contains(
    api: JsonClient,
    config: SoakConfig,
    agent: AgentSpec,
    marker: str,
) -> None:
    recall = api.request(
        "POST",
        "/v1/memory/recall",
        {
            "tenant_id": str(config.tenant_id),
            "workspace_id": str(agent.workspace_id),
            "agent_id": str(agent.agent_id),
            "query": f"{agent.query}: {marker}",
            "operation": f"{agent.name}_soak_recall",
            "top_k": 8,
            "context_budget_tokens": 1200,
            "labels": list(agent.labels),
        },
    )
    texts = _recall_texts(recall)
    _expect(any(marker in text for text in texts), f"recall did not return marker {marker}")


def _check_cross_workspace_leakage(api: JsonClient, config: SoakConfig) -> None:
    for round_index in range(config.rounds):
        openclaw_marker = _marker(config.run_id, "openclaw", round_index)
        hermes_marker = _marker(config.run_id, "hermes", round_index)
        _expect_recall_excludes(api, config, AGENTS[1], openclaw_marker)
        _expect_recall_excludes(api, config, AGENTS[0], hermes_marker)


def _expect_recall_excludes(
    api: JsonClient,
    config: SoakConfig,
    agent: AgentSpec,
    foreign_marker: str,
) -> None:
    recall = api.request(
        "POST",
        "/v1/memory/recall",
        {
            "tenant_id": str(config.tenant_id),
            "workspace_id": str(agent.workspace_id),
            "agent_id": str(agent.agent_id),
            "query": f"find marker {foreign_marker}",
            "operation": f"{agent.name}_soak_leakage_probe",
            "top_k": 12,
            "context_budget_tokens": 2000,
            "labels": list(agent.labels),
        },
    )
    texts = _recall_texts(recall)
    _expect(
        not any(foreign_marker in text for text in texts),
        f"{agent.name} recall leaked foreign marker {foreign_marker}",
    )


def _recall_texts(recall: Any) -> list[str]:
    if not isinstance(recall, dict):
        raise AssertionError("recall returned non-object response")
    results = recall.get("results", [])
    context = recall.get("context", {})
    texts = [
        str(row.get("text", ""))
        for row in results
        if isinstance(row, dict)
    ]
    if isinstance(context, dict):
        texts.append(str(context.get("markdown", "")))
    return texts


def _marker(run_id: str, agent_name: str, round_index: int) -> str:
    return f"SOAK-{run_id}-{agent_name.upper()}-{round_index:03d}"


def _check(name: str, fn: Any) -> CheckResult:
    started = time.perf_counter()
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - eval report should capture every failure.
        return CheckResult(
            name=name,
            ok=False,
            duration_ms=_elapsed_ms(started),
            detail=f"{type(exc).__name__}: {exc}",
        )
    return CheckResult(name=name, ok=True, duration_ms=_elapsed_ms(started))


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def write_report(report: SoakReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(report), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _print_report(report: SoakReport) -> None:
    print(json.dumps(asdict(report), ensure_ascii=False, indent=2))


def _parse_uuid(value: str) -> UUID:
    return UUID(value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.getenv("UAM_BASE_URL", "http://127.0.0.1:6798"))
    parser.add_argument("--api-key", default=read_secret_env("UAM_API_KEY"))
    parser.add_argument("--tenant-id", type=_parse_uuid, default=DEFAULT_TENANT)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--parallel", type=int, default=2)
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument("--run-id", default=os.getenv("UAM_AGENT_SOAK_RUN_ID") or uuid4().hex[:12])
    parser.add_argument("--json-report", type=Path)
    parser.add_argument("--print-curl", action="store_true")
    args = parser.parse_args()

    if args.print_curl:
        query = urlencode({"tenant_id": str(args.tenant_id)})
        print(f"curl -fsS {args.base_url.rstrip()}/health")
        print(
            "curl -fsS "
            f"{args.base_url.rstrip()}/v1/workspaces/{OPENCLAW_WORKSPACE}/memories?{query}"
        )
        return 0

    config = SoakConfig(
        base_url=args.base_url,
        api_key=args.api_key,
        tenant_id=args.tenant_id,
        rounds=args.rounds,
        parallel=args.parallel,
        timeout_seconds=args.timeout_seconds,
        run_id=args.run_id,
    )
    report = run_soak(config)
    if args.json_report:
        write_report(report, args.json_report)
    _print_report(report)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
