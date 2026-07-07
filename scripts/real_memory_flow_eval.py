"""Run real-memory UAM scenarios with a live embedding provider.

The flow is intentionally small and deterministic:

1. retain realistic memory atoms;
2. process retained events through EmbeddingService;
3. recall via RetrievalService using the query-embedding-backed vector source.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from uuid import UUID

from memory_plane.adapters.embeddings import (
    EmbeddingProviderConfig,
    build_embedding_client,
)
from memory_plane.adapters.in_memory import InMemoryMemoryStore
from memory_plane.adapters.qdrant import QdrantCandidateSource
from memory_plane.contracts.dto import RecallQuery, RetainCommand
from memory_plane.domain.models import MemoryLayer, MemoryScope, Provenance
from memory_plane.services.embedding import EmbeddingService
from memory_plane.services.retention import RetentionService
from memory_plane.services.retrieval import RetrievalService

TENANT = UUID("00000000-0000-0000-0000-000000000101")
WORKSPACE = UUID("00000000-0000-0000-0000-000000000202")
PROVENANCE = Provenance(source_kind="real-memory-flow-eval")


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    query: str
    expected: tuple[str, ...]


MEMORIES = {
    "storage-postgres": "Долговременная память UAM хранится в PostgreSQL ledger; "
    "Qdrant нужен как semantic vector index для recall.",
    "embedding-dgx-q8": "На DGX Spark .10 production embeddings запускаются как "
    "Jina embeddings v4 text retrieval Q8_0 GGUF через llama.cpp wrapper.",
    "openclaw-plugin": "OpenClaw должен получать память через глубокий plugin runtime: "
    "before run делает recall, after tool call сохраняет полезные traces.",
    "hermes-plugin": "Hermes использует Python plugin hooks: prefetch перед turn, "
    "sync_turn после ответа и session summary в конце.",
    "obsolete-fake": "Устаревшая инструкция: использовать fake embeddings в production.",
    "current-jina-q8": "Актуальная инструкция: использовать Jina embeddings v4 Q8_0 "
    "на DGX Spark .10 для production semantic recall.",
}


SCENARIOS = (
    Scenario(
        "storage recall",
        "где хранить долговременную память и для чего qdrant?",
        ("storage-postgres",),
    ),
    Scenario(
        "dgx q8 recall",
        "какую embedding модель запускать на dgx spark .10?",
        ("embedding-dgx-q8", "current-jina-q8"),
    ),
    Scenario(
        "openclaw recall",
        "как openclaw должен подключать память перед запуском?",
        ("openclaw-plugin",),
    ),
    Scenario(
        "hermes recall",
        "как hermes синхронизирует память после ответа?",
        ("hermes-plugin",),
    ),
    Scenario(
        "freshness recall",
        "какие embeddings использовать в production semantic recall?",
        ("current-jina-q8",),
    ),
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://192.168.0.10:8002")
    parser.add_argument("--model", default="jina-embeddings-v4")
    parser.add_argument("--dimension", type=int, default=2048)
    args = parser.parse_args()

    client = build_embedding_client(
        EmbeddingProviderConfig(
            provider="tei",
            model_name=args.model,
            dimension=args.dimension,
            base_url=args.base_url,
            timeout_seconds=60,
        )
    )
    store = InMemoryMemoryStore()
    vector_source = QdrantCandidateSource(
        url="memory://local",
        dense_dim=args.dimension,
        query_embedding_client=client,
    )
    vector_source._use_in_memory_backend()
    retention = RetentionService(store)
    embeddings = EmbeddingService(store, vector_source, client)
    retrieval = RetrievalService((store, vector_source))

    ids_by_key: dict[str, UUID] = {}
    for key, text in MEMORIES.items():
        result = retention.retain(
            RetainCommand(
                tenant_id=TENANT,
                workspace_id=WORKSPACE,
                layer=MemoryLayer.SEMANTIC,
                scope=MemoryScope.WORKSPACE,
                kind="fact",
                text=text,
                provenance=PROVENANCE,
                labels=("real-eval", key),
                importance=0.8 if key.startswith("current") else 0.5,
                confidence=0.9,
                idempotency_key=f"real-eval:{key}",
            )
        )
        ids_by_key[key] = result.item.id
        embeddings.process_memory_retained(TENANT, result.item.id)

    failures: list[str] = []
    print(f"endpoint={args.base_url} model={client.model_name} dimension={client.dimension}")
    for scenario in SCENARIOS:
        recall = retrieval.recall(
            RecallQuery(
                tenant_id=TENANT,
                workspace_id=WORKSPACE,
                text=scenario.query,
                top_k=3,
            )
        )
        top = recall.candidates[0]
        expected_ids = {ids_by_key[key] for key in scenario.expected}
        ok = top.item.id in expected_ids
        status = "PASS" if ok else "FAIL"
        print(
            f"{status} {scenario.name}: expected={'|'.join(scenario.expected)} "
            f"top={top.item.labels[-1]} score={top.final_score:.4f} "
            f"semantic={top.semantic:.4f} source={top.source}"
        )
        for candidate in recall.candidates:
            print(
                f"  - {candidate.item.labels[-1]} final={candidate.final_score:.4f} "
                f"semantic={candidate.semantic:.4f} lexical={candidate.lexical:.4f}"
            )
        if not ok:
            failures.append(scenario.name)
    if failures:
        print("failed_cases=" + ",".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
