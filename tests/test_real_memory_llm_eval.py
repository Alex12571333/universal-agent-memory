from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

from memory_plane.adapters.llm import MemoryLLMConfig

ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


real_memory_llm_eval = _load_script("real_memory_llm_eval")


class FakeMemoryLLMClient:
    def __init__(self, *, bad_proposal: bool = False) -> None:
        self.config = MemoryLLMConfig(
            model_name="provider-test",
            base_url="http://memory-llm.example/v1",
        )
        self.bad_proposal = bad_proposal
        self.calls: list[tuple[str, list[dict[str, str]], dict[str, Any]]] = []

    def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        self.calls.append(("chat", messages, kwargs))
        return "память"

    def chat_json(self, messages: list[dict[str, str]], **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("chat_json", messages, kwargs))
        return {
            "action": "retain",
            "proposal": (
                "production использует fake embeddings"
                if self.bad_proposal
                else "production использует OpenAI-compatible embeddings endpoint"
            ),
            "confidence": 0.91,
            "tags": ["embeddings", "openai-compatible"],
        }


def test_real_memory_llm_eval_passes_memory_contract() -> None:
    client = FakeMemoryLLMClient()

    report = real_memory_llm_eval.run_eval(client)

    assert report.ok is True
    assert report.format == "obelisk-memory-llm-eval-v1"
    assert {check.name for check in report.checks} == {
        "chat-completions",
        "json-memory-curation",
    }


def test_real_memory_llm_eval_fails_when_model_keeps_obsolete_claim() -> None:
    report = real_memory_llm_eval.run_eval(FakeMemoryLLMClient(bad_proposal=True))

    assert report.ok is False
    failed = [check for check in report.checks if not check.ok]
    assert len(failed) == 1
    assert failed[0].name == "json-memory-curation"
    assert "fake embeddings" in failed[0].detail


def test_real_memory_llm_eval_writes_json_report(tmp_path: Path) -> None:
    report = real_memory_llm_eval.run_eval(FakeMemoryLLMClient())
    path = tmp_path / "reports" / "memory-llm.json"

    real_memory_llm_eval.write_report(report, path)

    text = path.read_text(encoding="utf-8")
    assert '"format": "obelisk-memory-llm-eval-v1"' in text
    assert '"ok": true' in text
