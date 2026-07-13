from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


agent_soak_eval = _load_script("agent_soak_eval")
BUILD_IDENTITY = {
    "version": "0.1.0",
    "source_commit": "a" * 40,
    "image_digest": "sha256:" + "b" * 64,
    "deployment_id": "agent-soak-test",
    "build_time": "2026-07-10T00:00:00+00:00",
}


class FakeSoakApi:
    def __init__(
        self,
        *,
        leak_foreign_marker: bool = False,
        include_build_identity: bool = True,
    ) -> None:
        self._lock = Lock()
        self._items_by_key: dict[str, dict[str, Any]] = {}
        self._items: list[dict[str, Any]] = []
        self._leak_foreign_marker = leak_foreign_marker
        self._build_identity = BUILD_IDENTITY if include_build_identity else None

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        expect_status: int = 200,
        auth: bool = True,
    ) -> Any:
        if method == "GET" and path == "/health":
            assert expect_status == 200
            return {"status": "ok"}
        if method == "GET" and path == "/ready":
            return {"status": "ok", "version": "0.1.0", "build": self._build_identity}
        if method == "POST" and path == "/v1/memory/retain":
            return self._retain(body or {}, expect_status=expect_status)
        if method == "POST" and path == "/v1/memory/recall":
            return self._recall(body or {})
        raise AssertionError(f"unexpected request {method} {path}")

    def _retain(self, body: dict[str, Any], *, expect_status: int) -> dict[str, Any]:
        assert expect_status == 201
        key = str(body["idempotency_key"])
        with self._lock:
            if key in self._items_by_key:
                return self._items_by_key[key]
            item = {
                "id": f"mem-{len(self._items) + 1}",
                "revision": 1,
                "created": True,
                "tenant_id": str(body["tenant_id"]),
                "workspace_id": str(body["workspace_id"]),
                "agent_id": str(body["agent_id"]),
                "text": str(body["text"]),
                "labels": list(body.get("labels", [])),
            }
            self._items.append(item)
            self._items_by_key[key] = item
            return item

    def _recall(self, body: dict[str, Any]) -> dict[str, Any]:
        workspace = str(body["workspace_id"])
        query = str(body.get("query", ""))
        with self._lock:
            candidates = [
                item
                for item in self._items
                if item["workspace_id"] == workspace and _query_matches(query, item["text"])
            ]
            if self._leak_foreign_marker and "find marker" in query:
                marker = query.rsplit(" ", 1)[-1]
                candidates.extend(
                    item
                    for item in self._items
                    if marker in item["text"] and item["workspace_id"] != workspace
                )
        return {
            "results": [{"id": item["id"], "text": item["text"]} for item in candidates],
            "sources_used": ["fake"],
            "context": {
                "markdown": "\n".join(f"- {item['text']}" for item in candidates),
                "trace_ids": [],
            },
        }


def _query_matches(query: str, text: str) -> bool:
    return any(part.startswith("SOAK-") and part in text for part in query.split())


def test_agent_soak_eval_passes_parallel_agent_lifecycle() -> None:
    config = agent_soak_eval.SoakConfig(
        base_url="http://memory.example",
        tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
        rounds=2,
        parallel=4,
        run_id="unit",
    )

    report = agent_soak_eval.run_soak(config, FakeSoakApi())

    assert report.ok is True
    assert report.format == "obelisk-agent-soak-v1"
    assert report.generated_at
    assert report.build == BUILD_IDENTITY
    assert {check.name for check in report.checks} >= {
        "health",
        "build-identity",
        "openclaw:retain:0",
        "openclaw:idempotent-retry:0",
        "openclaw:recall:0",
        "hermes:retain:0",
        "hermes:idempotent-retry:0",
        "hermes:recall:0",
        "cross-workspace-leakage",
    }


def test_agent_soak_eval_fails_on_cross_workspace_leakage() -> None:
    config = agent_soak_eval.SoakConfig(
        base_url="http://memory.example",
        rounds=1,
        parallel=2,
        run_id="leak",
    )

    report = agent_soak_eval.run_soak(config, FakeSoakApi(leak_foreign_marker=True))

    assert report.ok is False
    leakage = next(check for check in report.checks if check.name == "cross-workspace-leakage")
    assert leakage.ok is False
    assert "leaked foreign marker" in leakage.detail


def test_agent_soak_eval_fails_without_verified_build_identity() -> None:
    config = agent_soak_eval.SoakConfig(rounds=1, parallel=1, run_id="no-build")

    report = agent_soak_eval.run_soak(config, FakeSoakApi(include_build_identity=False))

    assert report.ok is False
    assert report.build == {}
    check = next(item for item in report.checks if item.name == "build-identity")
    assert check.ok is False
    assert "build identity is missing" in check.detail


def test_agent_soak_eval_writes_json_report(tmp_path: Path) -> None:
    report = agent_soak_eval.SoakReport(
        format="obelisk-agent-soak-v1",
        ok=True,
        generated_at="2026-07-10T00:00:00+00:00",
        build=BUILD_IDENTITY,
        base_url="http://memory.example",
        tenant_id="00000000-0000-0000-0000-000000000001",
        run_id="write",
        rounds=1,
        parallel=1,
        checks=(
            agent_soak_eval.CheckResult(
                name="health",
                ok=True,
                duration_ms=1,
            ),
        ),
    )
    path = tmp_path / "reports" / "agent-soak.json"

    agent_soak_eval.write_report(report, path)

    text = path.read_text(encoding="utf-8")
    assert '"format": "obelisk-agent-soak-v1"' in text
    assert '"ok": true' in text
