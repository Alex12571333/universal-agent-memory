"""Production-readiness checks that run without external infrastructure.

This is intentionally broader than unit tests: it exercises concurrent retains,
CAS supersede races, tenant/workspace isolation, secret redaction, status policy,
vault import/export, and real embedding recall when an endpoint is supplied.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from uuid import UUID

from memory_plane.adapters.embeddings import (
    EmbeddingProviderConfig,
    FakeEmbeddingClient,
    build_embedding_client,
)
from memory_plane.adapters.in_memory import (
    InMemoryConflictReviewRepository,
    InMemoryMemoryStore,
    InMemoryObservationRepository,
)
from memory_plane.adapters.qdrant import QdrantCandidateSource
from memory_plane.contracts.dto import RecallQuery, RetainCommand, SupersedeMemoryCommand
from memory_plane.domain.models import MemoryLayer, MemoryScope, MemoryStatus, Provenance
from memory_plane.services.conflicts import ConflictService
from memory_plane.services.embedding import EmbeddingService
from memory_plane.services.reflection import ReflectionService
from memory_plane.services.retention import RetentionService
from memory_plane.services.retrieval import RetrievalService
from memory_plane.services.vault import VaultExporter, VaultImportSource

TENANT = UUID("00000000-0000-0000-0000-000000000101")
WORKSPACE = UUID("00000000-0000-0000-0000-000000000202")
FOREIGN_TENANT = UUID("00000000-0000-0000-0000-000000000303")
PROVENANCE = Provenance(source_kind="production-readiness-eval")


@dataclass(slots=True)
class Harness:
    store: InMemoryMemoryStore
    retention: RetentionService
    retrieval: RetrievalService
    embedding: EmbeddingService
    reflection: ReflectionService
    conflicts: ConflictService
    vault: VaultExporter


def build_harness(*, real_embeddings_url: str | None, model: str, dimension: int) -> Harness:
    store = InMemoryMemoryStore()
    observations = InMemoryObservationRepository(store)
    conflict_reviews = InMemoryConflictReviewRepository(store)
    client = (
        build_embedding_client(
            EmbeddingProviderConfig(
                provider="tei",
                model_name=model,
                dimension=dimension,
                base_url=real_embeddings_url,
                timeout_seconds=60,
            )
        )
        if real_embeddings_url
        else FakeEmbeddingClient(dimension=dimension)
    )
    vector_source = QdrantCandidateSource(
        url="memory://local",
        dense_dim=dimension,
        query_embedding_client=client,
    )
    vector_source._use_in_memory_backend()
    retention = RetentionService(store)
    return Harness(
        store=store,
        retention=retention,
        retrieval=RetrievalService((store, vector_source)),
        embedding=EmbeddingService(store, vector_source, client),
        reflection=ReflectionService(store, observations),
        conflicts=ConflictService(store, conflict_reviews),
        vault=VaultExporter(store, observations, retention),
    )


def retain(
    harness: Harness,
    text: str,
    *,
    tenant: UUID = TENANT,
    workspace: UUID = WORKSPACE,
    layer: MemoryLayer = MemoryLayer.SEMANTIC,
    labels: tuple[str, ...] = (),
    status: MemoryStatus = MemoryStatus.ACTIVE,
    key: str | None = None,
    importance: float = 0.5,
) -> UUID:
    result = harness.retention.retain(
        RetainCommand(
            tenant_id=tenant,
            workspace_id=workspace,
            layer=layer,
            scope=MemoryScope.WORKSPACE,
            kind="fact",
            text=text,
            provenance=PROVENANCE,
            labels=labels,
            status=status,
            idempotency_key=key,
            confidence=0.9,
            importance=importance,
        )
    )
    harness.embedding.process_memory_retained(tenant, result.item.id)
    return result.item.id


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def check_concurrent_idempotent_retains(harness: Harness) -> None:
    key = "concurrent:idempotent-retain"

    def write(index: int) -> UUID:
        return retain(
            harness,
            f"Concurrent idempotent memory body {index}",
            labels=("concurrency",),
            key=key,
        )

    with ThreadPoolExecutor(max_workers=16) as pool:
        ids = list(pool.map(write, range(32)))

    expect(len(set(ids)) == 1, "idempotent concurrent retain created duplicates")
    rows = harness.store.list_for_workspace(TENANT, WORKSPACE)
    matches = [row for row in rows if "concurrency" in row.labels]
    expect(len(matches) == 1, "concurrent idempotent retain stored more than one row")


def check_supersede_cas_race(harness: Harness) -> None:
    parent_id = retain(harness, "Alpha owner is Ivan.", labels=("cas",), key="cas:parent")

    def supersede(index: int) -> str:
        try:
            result = harness.retention.supersede(
                SupersedeMemoryCommand(
                    tenant_id=TENANT,
                    item_id=parent_id,
                    replacement_text=f"Alpha owner is Alex candidate {index}.",
                    expected_revision=1,
                    idempotency_key=f"cas:race:{index}",
                )
            )
            harness.embedding.process_memory_retained(TENANT, result.item.id)
            return "created"
        except Exception as exc:  # noqa: BLE001 - eval reports aggregate behavior.
            return type(exc).__name__

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(supersede, range(8)))

    expect(outcomes.count("created") == 1, f"expected one CAS winner, got {outcomes}")
    expect(
        outcomes.count("MemoryRevisionConflictError") == 7,
        f"expected stale writers to conflict, got {outcomes}",
    )


def check_isolation_and_policy(harness: Harness) -> None:
    retain(harness, "Tenant A knows the launch codename is Aurora.", labels=("isolation",))
    retain(
        harness,
        "Tenant B knows the launch codename is Borealis.",
        tenant=FOREIGN_TENANT,
        labels=("isolation",),
    )
    retain(
        harness,
        "Rejected memory must never appear in recall.",
        labels=("status",),
        status=MemoryStatus.REJECTED,
    )
    retain(
        harness,
        "Archived memory must never appear in recall.",
        labels=("status",),
        status=MemoryStatus.ARCHIVED,
    )

    recall = harness.retrieval.recall(
        RecallQuery(
            tenant_id=TENANT,
            workspace_id=WORKSPACE,
            text="launch codename memory appear",
            top_k=10,
        )
    )
    texts = {candidate.item.text for candidate in recall.candidates}
    expect(any("Aurora" in text for text in texts), "own-tenant memory missing")
    expect(not any("Borealis" in text for text in texts), "foreign tenant leaked")
    expect(not any("Rejected memory" in text for text in texts), "rejected memory leaked")
    expect(not any("Archived memory" in text for text in texts), "archived memory leaked")


def check_secret_redaction(harness: Harness) -> None:
    item_id = retain(
        harness,
        "Operator pasted API key sk-live-secret-token-1234567890 in a tool log.",
        labels=("privacy",),
    )
    item = harness.store.get(TENANT, item_id)
    expect(item is not None, "redaction test item missing")
    expect("sk-live-secret-token-1234567890" not in item.text, "secret was retained verbatim")
    expect("[REDACTED:openai_api_key]" in item.text, "secret redaction marker missing")


def check_conflicts_and_vault(harness: Harness) -> None:
    retain(harness, "Release Atlas is July 15.", labels=("conflict",))
    retain(harness, "Release Atlas is July 16.", labels=("conflict",))
    observations = harness.reflection.reflect(TENANT, WORKSPACE)
    expect(observations, "reflection did not produce observations")
    cases = harness.conflicts.list_cases(TENANT, WORKSPACE)
    expect(cases, "conflict inbox did not detect conflicting release dates")

    vault = harness.vault.export(TENANT, WORKSPACE)
    memory_notes = [
        file
        for file in vault.files
        if file.path.startswith("semantic/") or file.path.startswith("core/")
    ]
    expect(memory_notes, "vault export did not include memory notes")
    note = next(file for file in memory_notes if "Release Atlas is July 15." in file.content)
    edited = note.content.replace("Release Atlas is July 15.", "Release Atlas is July 17.")
    plan = harness.vault.plan_import(
        TENANT,
        WORKSPACE,
        (VaultImportSource(path=note.path, content=edited),),
    )
    expect(plan.dry_run, "vault import plan should be a dry run")
    expect(plan.supersede_count == 1, "vault import did not plan a safe supersede")


def check_real_semantic_recall(harness: Harness) -> None:
    retain(
        harness,
        "OpenClaw uses a deep plugin runtime: recall before run and retain after tools.",
        labels=("semantic-real", "openclaw"),
    )
    retain(
        harness,
        "Hermes uses Python plugin hooks: prefetch before turn and sync after reply.",
        labels=("semantic-real", "hermes"),
    )
    recall = harness.retrieval.recall(
        RecallQuery(
            tenant_id=TENANT,
            workspace_id=WORKSPACE,
            text="как openclaw подключает память перед запуском?",
            top_k=3,
        )
    )
    expect(recall.candidates, "semantic recall returned no candidates")
    expect(
        "openclaw" in recall.candidates[0].item.labels,
        f"semantic recall picked wrong top result: {recall.candidates[0].item.labels}",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--embedding-base-url")
    parser.add_argument("--embedding-model", default="jina-embeddings-v4")
    parser.add_argument("--embedding-dim", type=int, default=2048)
    args = parser.parse_args()

    harness = build_harness(
        real_embeddings_url=args.embedding_base_url,
        model=args.embedding_model,
        dimension=args.embedding_dim,
    )
    checks = (
        check_concurrent_idempotent_retains,
        check_supersede_cas_race,
        check_isolation_and_policy,
        check_secret_redaction,
        check_conflicts_and_vault,
        check_real_semantic_recall,
    )
    for check in checks:
        check(harness)
        print(f"PASS {check.__name__}")
    print("production_readiness_eval=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
