"""Live conversation ledger → curation → recall release gate.

This checks that raw transcript capture is installed without dumping raw turns
directly into recall, and that explicit curation can turn a captured turn into
recallable memory with conversation provenance.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

from memory_plane.build_info import require_status_build_identity
from memory_plane.config.secrets import read_secret_env

REPORT_FORMAT = "obelisk-conversation-pipeline-v1"
DEFAULT_TENANT = UUID("00000000-0000-0000-0000-000000000001")
DEFAULT_WORKSPACE = UUID("00000000-0000-0000-0000-000000000002")
DEFAULT_THREAD = UUID("00000000-0000-0000-0000-000000000042")
DEFAULT_AGENT = UUID("00000000-0000-0000-0000-000000000043")


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
class PipelineConfig:
    """Runtime options for the live conversation pipeline check."""

    base_url: str = "http://127.0.0.1:6798"
    api_key: str | None = None
    tenant_id: UUID = DEFAULT_TENANT
    workspace_id: UUID = DEFAULT_WORKSPACE
    thread_id: UUID = DEFAULT_THREAD
    agent_id: UUID = DEFAULT_AGENT
    namespace: str = "release-conversation"
    timeout_seconds: int = 30
    run_id: str = field(default_factory=lambda: uuid4().hex[:12])


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One conversation pipeline check."""

    name: str
    ok: bool
    detail: str


@dataclass(frozen=True, slots=True)
class PipelineReport:
    """Machine-readable conversation pipeline release evidence."""

    format: str
    ok: bool
    generated_at: str
    build: dict[str, str]
    base_url: str
    tenant_id: str
    workspace_id: str
    thread_id: str
    namespace: str
    run_id: str
    turn_id: str | None
    memory_id: str | None
    checks: list[CheckResult]


def run_eval(client: JsonClient, config: PipelineConfig) -> PipelineReport:
    """Run raw capture, explicit curation and recall checks."""
    build, build_check = _capture_build_identity(client)
    checks: list[CheckResult] = [build_check]
    marker = f"conversation-pipeline-{config.run_id}"
    turn_id: str | None = None
    proposal_id: str | None = None
    memory_id: str | None = None

    try:
        provisioned = client.request(
            "POST",
            "/v1/identities/provision",
            {
                "tenant_id": str(config.tenant_id),
                "workspace_id": str(config.workspace_id),
                "agent_id": str(config.agent_id),
                "agent_name": "Obelisk conversation release evaluator",
                "agent_role": "release-eval",
                "agent_config": {"namespace": config.namespace},
                "thread_id": str(config.thread_id),
            },
        )
        checks.append(
            CheckResult(
                "agent-thread-provisioned",
                str(provisioned.get("thread", {}).get("id")) == str(config.thread_id),
                f"agent_id={config.agent_id} thread_id={config.thread_id}",
            )
        )
    except Exception as exc:  # noqa: BLE001
        checks.append(
            CheckResult("agent-thread-provisioned", False, f"{type(exc).__name__}: {exc}")
        )
        return _report(config, checks, build, turn_id, memory_id)

    try:
        turn = client.request(
            "POST",
            "/v1/conversations/turns",
            {
                "tenant_id": str(config.tenant_id),
                "workspace_id": str(config.workspace_id),
                "thread_id": str(config.thread_id),
                "agent_id": str(config.agent_id),
                "namespace": config.namespace,
                "source_kind": "release-eval",
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            f"Запомни проверку {marker}: интерфейс памяти должен "
                            "оставаться на русском."
                        ),
                    },
                    {
                        "role": "assistant",
                        "content": "Принял, сохраню это через conversation pipeline.",
                    },
                ],
                "metadata": {"run_id": config.run_id},
                "idempotency_key": f"conversation-pipeline-turn:{config.run_id}",
            },
            expect_status=201,
        )
        turn_id = str(turn["id"])
        checks.append(
            CheckResult(
                "raw-turn-stored",
                bool(turn.get("created")) and turn.get("retention_policy") == "raw_and_curated",
                f"turn_id={turn_id} created={turn.get('created')}",
            )
        )
    except Exception as exc:  # noqa: BLE001 - release report captures failures.
        checks.append(CheckResult("raw-turn-stored", False, f"{type(exc).__name__}: {exc}"))
        return _report(config, checks, build, turn_id, memory_id)

    try:
        listed = client.request(
            "GET",
            f"/v1/conversations/turns?namespace={config.namespace}&limit=20",
        )
        checks.append(
            CheckResult(
                "raw-turn-listed",
                any(str(item.get("id")) == turn_id for item in listed.get("turns", [])),
                f"count={listed.get('count')}",
            )
        )
    except Exception as exc:  # noqa: BLE001
        checks.append(CheckResult("raw-turn-listed", False, f"{type(exc).__name__}: {exc}"))

    try:
        pre_recall = client.request(
            "POST",
            "/v1/memory/recall",
            {
                "tenant_id": str(config.tenant_id),
                "workspace_id": str(config.workspace_id),
                "thread_id": str(config.thread_id),
                "query": marker,
                "top_k": 20,
            },
        )
        pre_ids = [str(item.get("id")) for item in pre_recall.get("results", [])]
        checks.append(
            CheckResult(
                "raw-turn-not-recalled",
                marker not in json.dumps(pre_recall, ensure_ascii=False),
                f"pre_recall_results={len(pre_ids)}",
            )
        )
    except Exception as exc:  # noqa: BLE001
        checks.append(CheckResult("raw-turn-not-recalled", False, f"{type(exc).__name__}: {exc}"))

    try:
        curated = client.request(
            "POST",
            f"/v1/conversations/turns/{turn_id}/curate",
            {
                "tenant_id": str(config.tenant_id),
                "labels": ["release-eval", "conversation-pipeline"],
                "idempotency_key": f"conversation-pipeline-curate:{config.run_id}",
            },
            expect_status=201,
        )
        proposal_id = str(curated["id"])
        checks.append(
            CheckResult(
                "curation-created-proposal",
                bool(curated.get("created"))
                and bool(proposal_id)
                and curated.get("status") == "open"
                and curated.get("metadata", {}).get("claim_status") == "unverified",
                f"proposal_id={proposal_id} status={curated.get('status')!r}",
            )
        )
    except Exception as exc:  # noqa: BLE001
        checks.append(
            CheckResult("curation-created-proposal", False, f"{type(exc).__name__}: {exc}")
        )
        return _report(config, checks, build, turn_id, memory_id)

    try:
        recalled = client.request(
            "POST",
            "/v1/memory/recall",
            {
                "tenant_id": str(config.tenant_id),
                "workspace_id": str(config.workspace_id),
                "thread_id": str(config.thread_id),
                "query": marker,
                "top_k": 20,
            },
        )
        recalled_ids = [str(item.get("id")) for item in recalled.get("results", [])]
        checks.append(
            CheckResult(
                "unaccepted-proposal-not-recalled",
                marker not in json.dumps(recalled, ensure_ascii=False),
                f"results={len(recalled_ids)} proposal_id={proposal_id}",
            )
        )
    except Exception as exc:  # noqa: BLE001
        checks.append(
            CheckResult("unaccepted-proposal-not-recalled", False, f"{type(exc).__name__}: {exc}")
        )

    try:
        accepted = client.request(
            "POST",
            f"/v1/memory/proposals/{proposal_id}/accept",
            {
                "tenant_id": str(config.tenant_id),
                "reviewer": "release-eval-operator",
                "reason": "explicit release-eval acceptance",
                "idempotency_key": f"conversation-pipeline-accept:{config.run_id}",
            },
            expect_status=201,
        )
        memory = accepted.get("memory") if isinstance(accepted, dict) else None
        memory_id = str(memory.get("id")) if isinstance(memory, dict) else None
        checks.append(
            CheckResult(
                "operator-accepted-proposal-created-memory",
                bool(memory_id) and accepted.get("proposal", {}).get("status") == "accepted",
                f"memory_id={memory_id}",
            )
        )
    except Exception as exc:  # noqa: BLE001
        checks.append(
            CheckResult(
                "operator-accepted-proposal-created-memory",
                False,
                f"{type(exc).__name__}: {exc}",
            )
        )
        return _report(config, checks, build, turn_id, memory_id)

    try:
        recalled = client.request(
            "POST",
            "/v1/memory/recall",
            {
                "tenant_id": str(config.tenant_id),
                "workspace_id": str(config.workspace_id),
                "thread_id": str(config.thread_id),
                "query": marker,
                "top_k": 20,
            },
        )
        recalled_ids = [str(item.get("id")) for item in recalled.get("results", [])]
        checks.append(
            CheckResult(
                "accepted-memory-recalled",
                memory_id in recalled_ids,
                f"results={len(recalled_ids)} memory_id={memory_id}",
            )
        )
    except Exception as exc:  # noqa: BLE001
        checks.append(
            CheckResult("accepted-memory-recalled", False, f"{type(exc).__name__}: {exc}")
        )

    return _report(config, checks, build, turn_id, memory_id)


def write_report(report: PipelineReport, path: Path) -> None:
    """Write the conversation pipeline report to JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(report), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _report(
    config: PipelineConfig,
    checks: list[CheckResult],
    build: dict[str, str],
    turn_id: str | None,
    memory_id: str | None,
) -> PipelineReport:
    return PipelineReport(
        format=REPORT_FORMAT,
        ok=all(check.ok for check in checks),
        generated_at=datetime.now(UTC).isoformat(),
        build=build,
        base_url=config.base_url,
        tenant_id=str(config.tenant_id),
        workspace_id=str(config.workspace_id),
        thread_id=str(config.thread_id),
        namespace=config.namespace,
        run_id=config.run_id,
        turn_id=turn_id,
        memory_id=memory_id,
        checks=checks,
    )


def _capture_build_identity(client: JsonClient) -> tuple[dict[str, str], CheckResult]:
    try:
        status = client.request("GET", "/v1/system/status")
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:6798")
    parser.add_argument("--api-key", default=read_secret_env("UAM_API_KEY"))
    parser.add_argument("--tenant-id", type=UUID, default=DEFAULT_TENANT)
    parser.add_argument("--workspace-id", type=UUID, default=DEFAULT_WORKSPACE)
    parser.add_argument("--thread-id", type=UUID, default=DEFAULT_THREAD)
    parser.add_argument("--agent-id", type=UUID, default=DEFAULT_AGENT)
    parser.add_argument("--namespace", default="release-conversation")
    parser.add_argument("--json-report", type=Path, default=Path("ops/conversation-pipeline.json"))
    parser.add_argument("--timeout-seconds", type=int, default=30)
    args = parser.parse_args()

    config = PipelineConfig(
        base_url=args.base_url,
        api_key=args.api_key,
        tenant_id=args.tenant_id,
        workspace_id=args.workspace_id,
        thread_id=args.thread_id,
        agent_id=args.agent_id,
        namespace=args.namespace,
        timeout_seconds=args.timeout_seconds,
    )
    report = run_eval(
        ApiClient(
            base_url=config.base_url,
            api_key=config.api_key,
            timeout_seconds=config.timeout_seconds,
        ),
        config,
    )
    write_report(report, args.json_report)
    for check in report.checks:
        status = "PASS" if check.ok else "FAIL"
        print(f"{status} {check.name}: {check.detail}")
    print(f"conversation_pipeline={'PASS' if report.ok else 'FAIL'}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
