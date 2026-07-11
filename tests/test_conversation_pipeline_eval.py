from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
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


conversation_pipeline_eval = _load_script("conversation_pipeline_eval")
BUILD_IDENTITY = {
    "version": "0.1.0",
    "source_commit": "a" * 40,
    "image_digest": "sha256:" + "b" * 64,
    "deployment_id": "conversation-pipeline-test",
    "build_time": "2026-07-10T00:00:00+00:00",
}


class FakeConversationClient:
    def __init__(self, *, leak_raw: bool = False, include_build_identity: bool = True) -> None:
        self.leak_raw = leak_raw
        self.turn_id = "00000000-0000-0000-0000-000000000111"
        self.memory_id = "00000000-0000-0000-0000-000000000222"
        self.accepted = False
        self.marker = ""
        self.build_identity = BUILD_IDENTITY if include_build_identity else None

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        expect_status: int = 200,
        auth: bool = True,
    ) -> Any:
        del expect_status, auth
        if method == "GET" and path == "/v1/system/status":
            return {"status": "ok", "version": "0.1.0", "build": self.build_identity}
        if method == "POST" and path == "/v1/conversations/turns":
            assert body is not None
            self.marker = str(body["messages"][0]["content"]).split("проверку ")[1].split(":")[0]
            return {
                "id": self.turn_id,
                "created": True,
                "retention_policy": "raw_and_curated",
            }
        if method == "GET" and path.startswith("/v1/conversations/turns"):
            return {"count": 1, "turns": [{"id": self.turn_id}]}
        if method == "POST" and path == f"/v1/conversations/turns/{self.turn_id}/curate":
            return {
                "id": self.memory_id,
                "created": True,
                "status": "open",
                "metadata": {"claim_status": "unverified"},
            }
        if method == "POST" and path == f"/v1/memory/proposals/{self.memory_id}/accept":
            self.accepted = True
            return {
                "proposal": {"status": "accepted"},
                "memory": {"id": self.memory_id, "created": True},
            }
        if method == "POST" and path == "/v1/memory/recall":
            if self.accepted:
                return {"results": [{"id": self.memory_id, "text": self.marker}]}
            if self.leak_raw:
                return {"results": [{"id": self.turn_id, "text": self.marker}]}
            return {"results": []}
        raise AssertionError((method, path, body))


def _config() -> object:
    return conversation_pipeline_eval.PipelineConfig(
        base_url="http://memory.example",
        api_key="secret",
        tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
        workspace_id=UUID("00000000-0000-0000-0000-000000000002"),
        run_id="abc123",
    )


def test_conversation_pipeline_eval_passes_full_pipeline() -> None:
    report = conversation_pipeline_eval.run_eval(FakeConversationClient(), _config())

    assert report.format == "obelisk-conversation-pipeline-v1"
    assert report.ok is True
    assert report.generated_at
    assert report.build == BUILD_IDENTITY
    assert {check.name for check in report.checks} == {
        "build-identity",
        "raw-turn-stored",
        "raw-turn-listed",
        "raw-turn-not-recalled",
        "curation-created-proposal",
        "unaccepted-proposal-not-recalled",
        "operator-accepted-proposal-created-memory",
        "accepted-memory-recalled",
    }
    assert report.turn_id == "00000000-0000-0000-0000-000000000111"
    assert report.memory_id == "00000000-0000-0000-0000-000000000222"


def test_conversation_pipeline_eval_fails_when_raw_turn_leaks_into_recall() -> None:
    report = conversation_pipeline_eval.run_eval(
        FakeConversationClient(leak_raw=True),
        _config(),
    )

    assert report.ok is False
    raw_check = next(check for check in report.checks if check.name == "raw-turn-not-recalled")
    assert raw_check.ok is False


def test_conversation_pipeline_eval_fails_without_verified_build_identity() -> None:
    report = conversation_pipeline_eval.run_eval(
        FakeConversationClient(include_build_identity=False),
        _config(),
    )

    assert report.ok is False
    assert report.build == {}
    check = next(item for item in report.checks if item.name == "build-identity")
    assert check.ok is False


def test_conversation_pipeline_eval_writes_json_report(tmp_path: Path) -> None:
    report = conversation_pipeline_eval.run_eval(FakeConversationClient(), _config())
    path = tmp_path / "ops" / "conversation-pipeline.json"

    conversation_pipeline_eval.write_report(report, path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["format"] == "obelisk-conversation-pipeline-v1"
    assert payload["ok"] is True
    assert payload["generated_at"]
    assert payload["build"] == BUILD_IDENTITY
    assert payload["run_id"] == "abc123"
