"""Smoke-evaluate a real embedding endpoint on memory-retrieval scenarios.

This script intentionally avoids project internals so it can validate an
OpenAI-compatible embedding service before wiring it into UAM/Qdrant.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from typing import Any
from urllib.request import Request, urlopen


@dataclass(frozen=True, slots=True)
class Case:
    name: str
    query: str
    expected_id: str


@dataclass(frozen=True, slots=True)
class MemoryDoc:
    doc_id: str
    text: str


DOCS = (
    MemoryDoc(
        "storage-postgres",
        "Obelisk Memory хранит долговременную память в PostgreSQL ledger; "
        "Qdrant используется как векторный индекс для semantic recall.",
    ),
    MemoryDoc(
        "embedding-dgx-q8",
        "Для production embeddings на DGX Spark .10 используется Jina embeddings v4 "
        "text retrieval Q8_0 GGUF через llama.cpp wrapper на порту 8002.",
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
        "current-jina-embeddings",
        "Актуальная инструкция: использовать Jina embeddings v4 Q8_0 на DGX Spark .10 "
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
        "dgx embedding model",
        "какую embedding модель запускать на dgx spark .10?",
        "embedding-dgx-q8",
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
        "current-jina-embeddings",
    ),
)


def post_embedding(base_url: str, model: str, text: str, input_type: str) -> list[float]:
    payload = {
        "model": model,
        "input": text,
        "input_type": input_type,
    }
    request = Request(
        f"{base_url.rstrip('/')}/v1/embeddings",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=60) as response:  # noqa: S310
        data: dict[str, Any] = json.loads(response.read().decode("utf-8"))
    return [float(value) for value in data["data"][0]["embedding"]]


def cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    return dot / max(left_norm * right_norm, 1e-12)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://192.168.0.10:8002")
    parser.add_argument("--model", default="jina-embeddings-v4")
    args = parser.parse_args()

    doc_vectors = {
        doc.doc_id: post_embedding(args.base_url, args.model, doc.text, "document")
        for doc in DOCS
    }
    failures: list[str] = []
    print(f"endpoint={args.base_url} model={args.model} docs={len(DOCS)}")
    for case in CASES:
        query_vector = post_embedding(args.base_url, args.model, case.query, "query")
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
        status = "PASS" if ok else "FAIL"
        print(
            f"{status} {case.name}: expected={case.expected_id} "
            f"top={top_id} score={top_score:.4f} margin={margin:.4f}"
        )
        print("  top3=" + ", ".join(f"{doc_id}:{score:.4f}" for doc_id, score in ranked[:3]))
        if not ok:
            failures.append(case.name)
    if failures:
        print("failed_cases=" + ",".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
