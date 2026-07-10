from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ui_walkthrough_eval = _load_script("ui_walkthrough_eval")


class FakeWalkthroughApi:
    def __init__(self, *, leak_vector_in_editable: bool = False) -> None:
        self._items: list[dict[str, Any]] = []
        self._leak_vector_in_editable = leak_vector_in_editable
        self._conflict_status = "unresolved"

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        expect_status: int = 200,
        auth: bool = True,
    ) -> Any:
        del auth
        if method == "GET" and path == "/ui":
            return """
            <h1>Универсальная память агентов</h1>
            <p>Редактируй обычный текст памяти</p>
            <button>Сохранить и пересчитать embedding</button>
            <button>Принять рекомендацию</button>
            <button>Скрыть как неактуальный</button>
            <script>decideConflict("/v1/settings/models")</script>
            """
        if method == "POST" and path == "/v1/memory/retain":
            assert expect_status == 201
            item = {
                "id": f"mem-{len(self._items) + 1}",
                "text": str((body or {})["text"]),
                "status": "active",
            }
            self._items.append(item)
            return item
        if method == "POST" and path == "/v1/memory/recall":
            query = str((body or {}).get("query", ""))
            return {
                "results": [
                    {"id": item["id"], "text": item["text"]}
                    for item in self._items
                    if query in item["text"]
                ]
            }
        if method == "GET" and "/conflicts?" in path:
            return {
                "count": 1,
                "cases": [
                    {
                        "id": "conflict-1",
                        "review_status": self._conflict_status,
                        "suggested_winner_value": "july 16",
                        "candidates": [
                            {"value": "july 15", "evidence_ids": ["mem-2"]},
                            {"value": "july 16", "evidence_ids": ["mem-3"]},
                        ],
                    }
                ],
            }
        if method == "PUT" and "/conflicts/conflict-1/decision" in path:
            self._conflict_status = str((body or {})["status"])
            return {"id": "conflict-1", "review_status": self._conflict_status}
        if method == "GET" and "/vault?" in path:
            files = []
            for item in self._items:
                editable = item["text"]
                if self._leak_vector_in_editable and "editable marker" in item["text"]:
                    editable = f'{item["text"]}\nembedding: [0.1, 0.2, 0.3, 0.4]'
                files.append(
                    {
                        "path": f"semantic/{item['id']}.md",
                        "content": (
                            "---\n"
                            'type: "memory"\n'
                            "revision: 1\n"
                            "---\n"
                            f"{item['text']}\n\n"
                            "## Provenance\nsource: test\n"
                        ),
                        "editable_content": editable,
                    }
                )
            return {"file_count": len(files), "files": files}
        if method == "POST" and "/vault/archive" in path:
            return {"changes": [{"action": "archive", "new_item_id": "mem-archived"}]}
        if method == "GET" and path == "/v1/settings/models":
            return {
                "desired": {
                    "provider": "fake",
                    "model_name": "fake-embeddings",
                    "dimension": 8,
                    "base_url": "",
                    "timeout_seconds": 5,
                }
            }
        if method == "POST" and path == "/v1/settings/models/test":
            return {"ok": True, "message": "endpoint returned expected vector dimension"}
        if method == "POST" and "/reindex?" in path:
            assert expect_status == 202
            return {"reindexed_count": len(self._items)}
        if method == "GET" and path == "/metrics":
            return "uam_memory_items_total 3\nuam_embedding_operations_total 1\n"
        raise AssertionError(f"unexpected request {method} {path}")


def test_ui_walkthrough_eval_passes_operator_flows() -> None:
    report = ui_walkthrough_eval.run_walkthrough(
        ui_walkthrough_eval.WalkthroughConfig(base_url="http://memory.example"),
        client=FakeWalkthroughApi(),
        run_id="unit",
    )

    assert report.ok is True
    assert report.format == "obelisk-ui-walkthrough-v1"
    assert {check.name for check in report.checks} >= {
        "ui-served",
        "retain-recall",
        "conflict-decision",
        "vault-editable-text",
        "vault-archive",
        "model-settings-probe",
        "reindex",
        "metrics-surface",
    }


def test_ui_walkthrough_eval_fails_when_vault_editor_exposes_vectors() -> None:
    report = ui_walkthrough_eval.run_walkthrough(
        ui_walkthrough_eval.WalkthroughConfig(base_url="http://memory.example"),
        client=FakeWalkthroughApi(leak_vector_in_editable=True),
        run_id="unit",
    )

    check = next(item for item in report.checks if item.name == "vault-editable-text")
    assert report.ok is False
    assert check.ok is False
    assert "system/vector fields" in check.detail


def test_ui_walkthrough_eval_writes_json_report(tmp_path: Path) -> None:
    report = ui_walkthrough_eval.WalkthroughReport(
        format="obelisk-ui-walkthrough-v1",
        ok=True,
        generated_at="2026-07-10T00:00:00+00:00",
        base_url="http://memory.example",
        tenant_id="00000000-0000-0000-0000-000000000001",
        workspace_id="00000000-0000-0000-0000-000000000002",
        run_id="write",
        checks=[ui_walkthrough_eval.CheckResult("ui-served", True, "ok")],
    )
    path = tmp_path / "ops" / "ui-walkthrough.json"

    ui_walkthrough_eval.write_report(report, path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["format"] == "obelisk-ui-walkthrough-v1"
    assert payload["ok"] is True
