"""Smoke-evaluate a real embedding endpoint on memory-retrieval scenarios.

This script intentionally avoids project internals so it can validate an
OpenAI-compatible embedding service before wiring it into UAM/Qdrant.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from memory_plane.config.secrets import read_secret_env


@dataclass(frozen=True, slots=True)
class Case:
    name: str
    query: str
    expected_id: str


@dataclass(frozen=True, slots=True)
class MemoryDoc:
    doc_id: str
    text: str


@dataclass(frozen=True, slots=True)
class EvalCheck:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True, slots=True)
class EvalReport:
    format: str
    ok: bool
    provider: str
    base_url: str
    model: str
    dimension: int
    checks: list[EvalCheck]


REPORT_FORMAT = "obelisk-embedding-eval-v1"


DOCS = (
    MemoryDoc(
        "storage-postgres",
        "Obelisk Memory хранит долговременную память в PostgreSQL ledger; "
        "Qdrant используется как векторный индекс для semantic recall.",
    ),
    MemoryDoc(
        "embedding-openai-compatible",
        "Для production embeddings используется OpenAI-compatible endpoint "
        "с моделью text-embedding-3-large и размерностью 3072.",
    ),
    MemoryDoc(
        "openclaw-plugin",
        "OpenClaw интегрируется глубоко через plugin runtime: перед запуском агента "
        "делается recall, после tool calls и завершения run сохраняются memories.",
    ),
    MemoryDoc(
        "hermes-plugin",
        "Hermes подключается через Python plugin hooks: prefetch памяти перед turn, "
        "sync_turn после ответа и session summary при завершении.",
    ),
    MemoryDoc(
        "obsolete-fake-embeddings",
        "Устаревшая инструкция: использовать deterministic fake embeddings в production.",
    ),
    MemoryDoc(
        "current-openai-compatible-embeddings",
        "Актуальная инструкция: использовать OpenAI-compatible embeddings endpoint "
        "для production semantic recall.",
    ),
)


CASES = (
    Case(
        "storage routing",
        "где хранится долговременная память и зачем нужен qdrant?",
        "storage-postgres",
    ),
    Case(
        "production embedding model",
        "какую embedding модель использовать в production?",
        "embedding-openai-compatible",
    ),
    Case(
        "openclaw integration",
        "как память должна подключаться к openclaw?",
        "openclaw-plugin",
    ),
    Case(
        "hermes integration",
        "как hermes будет синхронизировать память после ответа?",
        "hermes-plugin",
    ),
    Case(
        "freshness preference",
        "какие embeddings использовать в production semantic recall?",
        "current-openai-compatible-embeddings",
    ),
)


def post_embedding(base_url: str, model: str, text: str, api_key: str | None) -> list[float]:
    payload = {
        "model": model,
        "input": text,
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = Request(
        _embedding_url(base_url),
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urlopen(request, timeout=60) as response:  # noqa: S310
        data: dict[str, Any] = json.loads(response.read().decode("utf-8"))
    return [float(value) for value in data["data"][0]["embedding"]]


def _embedding_url(base_url: str) -> str:
    """Return an embeddings URL for roots with or without `/v1`."""
    normalized = base_url.rstrip("/")
    if normalized.endswith("/v1"):
        return f"{normalized}/embeddings"
    return f"{normalized}/v1/embeddings"


def cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    return dot / max(left_norm * right_norm, 1e-12)


def run_eval(
    *,
    provider: str,
    base_url: str,
    model: str,
    api_key: str | None,
    expected_dimension: int,
) -> EvalReport:
    """Run semantic embedding checks and return machine-readable evidence."""
    checks: list[EvalCheck] = []
    try:
        doc_vectors = {
            doc.doc_id: post_embedding(base_url, model, doc.text, api_key)
            for doc in DOCS
        }
        sample_vector = next(iter(doc_vectors.values()))
        checks.append(EvalCheck("endpoint-reachable", True, f"docs={len(DOCS)}"))
        checks.append(
            EvalCheck(
                "dimension",
                len(sample_vector) == expected_dimension,
                f"expected={expected_dimension} actual={len(sample_vector)}",
            )
        )
    except Exception as exc:  # noqa: BLE001 - release report captures provider failures.
        checks.append(EvalCheck("endpoint-reachable", False, f"{type(exc).__name__}: {exc}"))
        return EvalReport(
            format=REPORT_FORMAT,
            ok=False,
            provider=provider,
            base_url=base_url,
            model=model,
            dimension=expected_dimension,
            checks=checks,
        )

    for case in CASES:
        query_vector = post_embedding(base_url, model, case.query, api_key)
        ranked = sorted(
            (
                (doc.doc_id, cosine(query_vector, doc_vectors[doc.doc_id]))
                for doc in DOCS
            ),
            key=lambda item: item[1],
            reverse=True,
        )
        top_id, top_score = ranked[0]
        margin = top_score - ranked[1][1]
        ok = top_id == case.expected_id
        checks.append(
            EvalCheck(
                f"semantic:{case.name}",
                ok,
                (
                    f"expected={case.expected_id} top={top_id} "
                    f"score={top_score:.4f} margin={margin:.4f}"
                ),
            )
        )
    return EvalReport(
        format=REPORT_FORMAT,
        ok=all(check.ok for check in checks),
        provider=provider,
        base_url=base_url,
        model=model,
        dimension=expected_dimension,
        checks=checks,
    )


def write_report(report: EvalReport, path: Path) -> None:
    """Write an embedding eval report as stable JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(report), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", default="openai-compatible")
    parser.add_argument("--base-url", default="https://api.openai.com")
    parser.add_argument("--model", default="text-embedding-3-large")
    parser.add_argument("--dimension", type=int, default=3072)
    parser.add_argument("--json-report", type=Path)
    parser.add_argument(
        "--api-key",
        default=read_secret_env("UAM_EMBEDDING_API_KEY", "OPENAI_API_KEY"),
    )
    args = parser.parse_args()

    report = run_eval(
        provider=args.provider,
        base_url=args.base_url,
        model=args.model,
        api_key=args.api_key,
        expected_dimension=args.dimension,
    )
    if args.json_report:
        write_report(report, args.json_report)
    print(f"endpoint={report.base_url} model={report.model} docs={len(DOCS)}")
    for check in report.checks:
        status = "PASS" if check.ok else "FAIL"
        print(f"{status} {check.name}: {check.detail}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
