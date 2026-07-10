"""Run an operator UI/API walkthrough against a live Obelisk Memory server.

The script is intentionally HTTP-level rather than browser-driven so it can run
from release automation without extra dependencies. It verifies the operator
flows behind the UI: human-readable vault editing, conflict decisions, model
settings, reindexing, and metrics visibility.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

from memory_plane.config.secrets import read_secret_env

TENANT = UUID("00000000-0000-0000-0000-000000000001")
WORKSPACE = UUID("00000000-0000-0000-0000-000000000002")
REPORT_FORMAT = "obelisk-ui-walkthrough-v1"

FORBIDDEN_EDIT_TOKENS = (
    "embedding",
    "embeddings",
    "vector",
    "vectors",
    "provenance",
    "tenant_id",
    "workspace_id",
    "qdrant",
    "dense",
    "sparse",
)


class WalkthroughClient(Protocol):
    """Minimal client surface used by the walkthrough."""

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        expect_status: int = 200,
        auth: bool = True,
    ) -> Any:
        """Perform one API/UI request."""


@dataclass(frozen=True, slots=True)
class WalkthroughConfig:
    """Runtime options for a live UI walkthrough."""

    base_url: str = "http://127.0.0.1:6798"
    api_key: str | None = None
    tenant_id: UUID = TENANT
    workspace_id: UUID = WORKSPACE
    timeout_seconds: float = 20.0
    skip_model_probe: bool = False


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One UI walkthrough check result."""

    name: str
    ok: bool
    detail: str


@dataclass(frozen=True, slots=True)
class WalkthroughReport:
    """Machine-readable evidence for a release UI walkthrough."""

    format: str
    ok: bool
    generated_at: str
    base_url: str
    tenant_id: str
    workspace_id: str
    run_id: str
    checks: list[CheckResult]


@dataclass(frozen=True, slots=True)
class ApiClient:
    """Small stdlib HTTP client for JSON and text endpoints."""

    base_url: str
    api_key: str | None = None
    timeout_seconds: float = 20.0

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        expect_status: int = 200,
        auth: bool = True,
    ) -> Any:
        """Call the running server and return JSON when possible."""
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
            raw = exc.read().decode("utf-8")
        if status != expect_status:
            raise AssertionError(f"{method} {path}: expected {expect_status}, got {status}: {raw}")
        if not raw:
            return None
        stripped = raw.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            return json.loads(raw)
        return raw


def run_walkthrough(
    config: WalkthroughConfig,
    *,
    client: WalkthroughClient | None = None,
    run_id: str | None = None,
) -> WalkthroughReport:
    """Run all release UI checks and return a JSON-serializable report."""
    api = client or ApiClient(config.base_url, config.api_key, config.timeout_seconds)
    current_run_id = run_id or uuid4().hex[:12]
    state: dict[str, Any] = {}
    checks = [
        _run_check("ui-served", lambda: _check_ui(api)),
        _run_check(
            "retain-recall",
            lambda: _check_retain_recall(api, config, current_run_id, state),
        ),
        _run_check(
            "conflict-decision",
            lambda: _check_conflict_decision(api, config, current_run_id),
        ),
        _run_check(
            "vault-editable-text",
            lambda: _check_vault_editable_text(api, config, current_run_id, state),
        ),
        _run_check(
            "vault-archive",
            lambda: _check_vault_archive(api, config, state),
        ),
        _run_check(
            "model-settings-probe",
            lambda: _check_model_settings_probe(api, config),
        ),
        _run_check("reindex", lambda: _check_reindex(api, config)),
        _run_check("metrics-surface", lambda: _check_metrics(api)),
    ]
    return WalkthroughReport(
        format=REPORT_FORMAT,
        ok=all(check.ok for check in checks),
        generated_at=datetime.now(UTC).isoformat(),
        base_url=config.base_url.rstrip("/"),
        tenant_id=str(config.tenant_id),
        workspace_id=str(config.workspace_id),
        run_id=current_run_id,
        checks=checks,
    )


def write_report(report: WalkthroughReport, path: Path) -> None:
    """Write a walkthrough report to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(report), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def main() -> int:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:6798")
    parser.add_argument("--api-key", default=read_secret_env("UAM_API_KEY"))
    parser.add_argument("--tenant-id", type=UUID, default=TENANT)
    parser.add_argument("--workspace-id", type=UUID, default=WORKSPACE)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--skip-model-probe", action="store_true")
    parser.add_argument("--json-report", type=Path)
    args = parser.parse_args()

    report = run_walkthrough(
        WalkthroughConfig(
            base_url=args.base_url,
            api_key=args.api_key,
            tenant_id=args.tenant_id,
            workspace_id=args.workspace_id,
            timeout_seconds=args.timeout_seconds,
            skip_model_probe=args.skip_model_probe,
        )
    )
    if args.json_report:
        write_report(report, args.json_report)
    for check in report.checks:
        status = "PASS" if check.ok else "FAIL"
        print(f"{status} {check.name}: {check.detail}")
    print("ui_walkthrough=PASS" if report.ok else "ui_walkthrough=FAIL")
    return 0 if report.ok else 1


def _run_check(name: str, action: Callable[[], str]) -> CheckResult:
    try:
        detail = action()
    except Exception as exc:  # noqa: BLE001 - reports all walkthrough failures.
        return CheckResult(name, False, f"{type(exc).__name__}: {exc}")
    return CheckResult(name, True, str(detail))


def _check_ui(api: WalkthroughClient) -> str:
    ui = str(api.request("GET", "/ui", auth=False))
    if '<div id="root"' in ui and "/ui/assets/" in ui:
        return "React operator UI shell is served; API walkthrough validates live actions"
    required = [
        "Универсальная память агентов",
        "Редактируй обычный текст памяти",
        "Сохранить и пересчитать embedding",
        "Принять рекомендацию",
        "Скрыть как неактуальный",
        "decideConflict(",
        "/v1/settings/models",
    ]
    missing = [needle for needle in required if needle not in ui]
    if missing:
        raise AssertionError("UI missing required text/hooks: " + ", ".join(missing))
    return "Russian operator UI and conflict/vault/model hooks are present"


def _check_retain_recall(
    api: WalkthroughClient,
    config: WalkthroughConfig,
    run_id: str,
    state: dict[str, Any],
) -> str:
    marker = f"UI walkthrough editable marker {run_id}"
    retained = api.request(
        "POST",
        "/v1/memory/retain",
        {
            "tenant_id": str(config.tenant_id),
            "workspace_id": str(config.workspace_id),
            "layer": "semantic",
            "scope": "workspace",
            "kind": "fact",
            "text": marker,
            "labels": ["ui-walkthrough"],
            "idempotency_key": f"ui-walkthrough:{run_id}:retain",
        },
        expect_status=201,
    )
    recall = api.request(
        "POST",
        "/v1/memory/recall",
        {
            "tenant_id": str(config.tenant_id),
            "workspace_id": str(config.workspace_id),
            "query": marker,
            "top_k": 10,
            "context_budget_tokens": 800,
        },
    )
    texts = [str(row.get("text", "")) for row in recall.get("results", [])]
    if not any(marker in text for text in texts):
        raise AssertionError("retained marker was not recalled")
    state["marker"] = marker
    state["retained_id"] = retained.get("id")
    return "retained marker is recallable"


def _check_conflict_decision(
    api: WalkthroughClient,
    config: WalkthroughConfig,
    run_id: str,
) -> str:
    subject = f"Walkthrough{run_id}"
    _retain_for_conflict(api, config, f"{subject} releases on July 15", f"{run_id}:old")
    _retain_for_conflict(api, config, f"{subject} releases on July 16", f"{run_id}:new")
    query = urlencode({"tenant_id": str(config.tenant_id), "include_resolved": "true"})
    conflicts = api.request("GET", f"/v1/workspaces/{config.workspace_id}/conflicts?{query}")
    case = _find_conflict_case(conflicts.get("cases", []), "july 15", "july 16")
    api.request(
        "PUT",
        f"/v1/workspaces/{config.workspace_id}/conflicts/{case['id']}/decision",
        {
            "tenant_id": str(config.tenant_id),
            "status": "accepted",
            "winner_value": case.get("suggested_winner_value") or "july 16",
            "reason": "release walkthrough accepted latest evidence",
        },
    )
    resolved = api.request("GET", f"/v1/workspaces/{config.workspace_id}/conflicts?{query}")
    resolved_case = next(
        (row for row in resolved.get("cases", []) if row.get("id") == case.get("id")),
        None,
    )
    if resolved_case is None or resolved_case.get("review_status") != "accepted":
        raise AssertionError("conflict decision was not persisted as accepted")
    return "conflict decision persisted"


def _check_vault_editable_text(
    api: WalkthroughClient,
    config: WalkthroughConfig,
    run_id: str,
    state: dict[str, Any],
) -> str:
    marker = state.get("marker") or f"UI walkthrough editable marker {run_id}"
    query = urlencode({"tenant_id": str(config.tenant_id)})
    vault = api.request("GET", f"/v1/workspaces/{config.workspace_id}/vault?{query}")
    file = _find_vault_file(vault.get("files", []), marker)
    editable = str(file.get("editable_content") or "")
    if marker not in editable:
        raise AssertionError("editable_content does not contain the memory text")
    forbidden = [token for token in FORBIDDEN_EDIT_TOKENS if token in editable.lower()]
    if forbidden:
        raise AssertionError(
            "editable_content contains system/vector fields: " + ", ".join(forbidden)
        )
    state["vault_file"] = file
    return "vault editor exposes ordinary memory text only"


def _check_vault_archive(
    api: WalkthroughClient,
    config: WalkthroughConfig,
    state: dict[str, Any],
) -> str:
    file = state.get("vault_file")
    if not isinstance(file, dict):
        raise AssertionError("vault file from editable-text check is missing")
    result = api.request(
        "POST",
        f"/v1/workspaces/{config.workspace_id}/vault/archive",
        {
            "tenant_id": str(config.tenant_id),
            "file": file,
        },
    )
    changes = result.get("changes", [])
    if not changes or changes[0].get("action") != "archive":
        raise AssertionError(f"vault archive did not archive the selected memory: {result}")
    return "vault archive is non-destructive and API-backed"


def _check_model_settings_probe(api: WalkthroughClient, config: WalkthroughConfig) -> str:
    settings = api.request("GET", "/v1/settings/models")
    desired = settings.get("desired") or settings.get("runtime") or {}
    if config.skip_model_probe:
        return "model endpoint probe skipped by operator"
    body = {
        "provider": desired.get("provider"),
        "model_name": desired.get("model_name"),
        "dimension": desired.get("dimension"),
        "base_url": desired.get("base_url"),
        "api_key": "",
        "timeout_seconds": desired.get("timeout_seconds", 20),
    }
    result = api.request("POST", "/v1/settings/models/test", body)
    if result.get("ok") is not True:
        raise AssertionError(result.get("message") or "model settings probe failed")
    return "model settings endpoint probe succeeded"


def _check_reindex(api: WalkthroughClient, config: WalkthroughConfig) -> str:
    query = urlencode({"tenant_id": str(config.tenant_id)})
    result = api.request(
        "POST",
        f"/v1/workspaces/{config.workspace_id}/reindex?{query}",
        expect_status=202,
    )
    if "reindexed_count" not in result:
        raise AssertionError("reindex response missing reindexed_count")
    return f"reindexed {result['reindexed_count']} memories"


def _check_metrics(api: WalkthroughClient) -> str:
    metrics = str(api.request("GET", "/metrics"))
    required = ["uam_memory_items_total", "uam_embedding_operations_total"]
    missing = [needle for needle in required if needle not in metrics]
    if missing:
        raise AssertionError("metrics endpoint missing: " + ", ".join(missing))
    return "metrics endpoint exposes memory and embedding counters"


def _retain_for_conflict(
    api: WalkthroughClient,
    config: WalkthroughConfig,
    text: str,
    key_suffix: str,
) -> None:
    api.request(
        "POST",
        "/v1/memory/retain",
        {
            "tenant_id": str(config.tenant_id),
            "workspace_id": str(config.workspace_id),
            "layer": "semantic",
            "scope": "workspace",
            "kind": "fact",
            "text": text,
            "labels": ["ui-walkthrough", "conflict"],
            "idempotency_key": f"ui-walkthrough:conflict:{key_suffix}",
        },
        expect_status=201,
    )


def _find_conflict_case(
    cases: list[dict[str, Any]],
    first_value: str,
    second_value: str,
) -> dict[str, Any]:
    for case in cases:
        values = {
            str(candidate.get("value", "")).lower()
            for candidate in case.get("candidates", [])
            if isinstance(candidate, dict)
        }
        if first_value in values and second_value in values:
            return case
    raise AssertionError("expected July 15/July 16 conflict case not found")


def _find_vault_file(files: list[dict[str, Any]], text: str) -> dict[str, Any]:
    for file in files:
        if text in str(file.get("content", "")) or text in str(file.get("editable_content", "")):
            return file
    raise AssertionError("vault export did not include the retained marker memory")


if __name__ == "__main__":
    raise SystemExit(main())
