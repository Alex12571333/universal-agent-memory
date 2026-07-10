from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


real_embedding_eval = _load_script("real_embedding_eval")


def test_real_embedding_eval_accepts_root_or_v1_base_url() -> None:
    assert (
        real_embedding_eval._embedding_url("https://api.example.com")
        == "https://api.example.com/v1/embeddings"
    )
    assert (
        real_embedding_eval._embedding_url("https://api.example.com/v1")
        == "https://api.example.com/v1/embeddings"
    )


def _fake_embedding(_base_url: str, _model: str, text: str, _api_key: str | None) -> list[float]:
    vectors = {
        "storage-postgres": [1.0, 0.0, 0.0, 0.0, 0.0],
        "embedding-openai-compatible": [0.0, 1.0, 0.0, 0.0, 0.0],
        "current-openai-compatible-embeddings": [0.0, 0.0, 1.0, 0.0, 0.0],
        "openclaw-plugin": [0.0, 0.0, 0.0, 1.0, 0.0],
        "hermes-plugin": [0.0, 0.0, 0.0, 0.0, 1.0],
        "obsolete-fake-embeddings": [-1.0, -1.0, -1.0, -1.0, -1.0],
    }
    for doc in real_embedding_eval.DOCS:
        if text == doc.text:
            return vectors[doc.doc_id]
    if "где хранится" in text:
        return vectors["storage-postgres"]
    if "какую embedding модель" in text:
        return vectors["embedding-openai-compatible"]
    if "openclaw" in text.lower():
        return vectors["openclaw-plugin"]
    if "hermes" in text.lower():
        return vectors["hermes-plugin"]
    if "semantic recall" in text:
        return vectors["current-openai-compatible-embeddings"]
    raise AssertionError(text)


def test_real_embedding_eval_passes_semantic_contract(monkeypatch) -> None:
    monkeypatch.setattr(real_embedding_eval, "post_embedding", _fake_embedding)

    report = real_embedding_eval.run_eval(
        provider="openai-compatible",
        base_url="http://embedding-gateway/v1",
        model="test-embed",
        api_key=None,
        expected_dimension=5,
    )

    assert report.format == "obelisk-embedding-eval-v1"
    assert report.ok is True
    assert {check.name for check in report.checks} == {
        "endpoint-reachable",
        "dimension",
        "semantic:storage routing",
        "semantic:production embedding model",
        "semantic:openclaw integration",
        "semantic:hermes integration",
        "semantic:freshness preference",
    }


def test_real_embedding_eval_fails_dimension_mismatch(monkeypatch) -> None:
    monkeypatch.setattr(real_embedding_eval, "post_embedding", _fake_embedding)

    report = real_embedding_eval.run_eval(
        provider="openai-compatible",
        base_url="http://embedding-gateway/v1",
        model="test-embed",
        api_key=None,
        expected_dimension=3072,
    )

    assert report.ok is False
    dimension = next(check for check in report.checks if check.name == "dimension")
    assert dimension.ok is False
    assert "actual=5" in dimension.detail


def test_real_embedding_eval_fails_wrong_semantic_top(monkeypatch) -> None:
    def bad_embedding(
        base_url: str,
        model: str,
        text: str,
        api_key: str | None,
    ) -> list[float]:
        if "какую embedding модель" in text:
            return [1.0, 0.0, 0.0, 0.0, 0.0]
        return _fake_embedding(base_url, model, text, api_key)

    monkeypatch.setattr(real_embedding_eval, "post_embedding", bad_embedding)

    report = real_embedding_eval.run_eval(
        provider="openai-compatible",
        base_url="http://embedding-gateway/v1",
        model="test-embed",
        api_key=None,
        expected_dimension=5,
    )

    assert report.ok is False
    failed = [check for check in report.checks if not check.ok]
    assert failed[0].name == "semantic:production embedding model"
    assert "top=storage-postgres" in failed[0].detail


def test_real_embedding_eval_writes_json_report(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(real_embedding_eval, "post_embedding", _fake_embedding)
    report = real_embedding_eval.run_eval(
        provider="openai-compatible",
        base_url="http://embedding-gateway/v1",
        model="test-embed",
        api_key=None,
        expected_dimension=5,
    )
    path = tmp_path / "ops" / "embedding.json"

    real_embedding_eval.write_report(report, path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["format"] == "obelisk-embedding-eval-v1"
    assert payload["ok"] is True
    assert payload["provider"] == "openai-compatible"
