"""Reindex restored canonical memory into a clean Qdrant and prove dense recall.

Run this only against the PostgreSQL instance created by ``restore_drill.py``
and a new, empty Qdrant collection.  It deliberately never reads the source
vector index, so a passing report proves the restored ledger was sufficient to
rebuild semantic retrieval.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from memory_plane.bootstrap import build_postgres_container
from memory_plane.config.database import read_database_dsn
from memory_plane.contracts.dto import RecallQuery
from memory_plane.domain.models import MemoryStatus

REPORT_FORMAT = "obelisk-restored-reindex-probe-v1"


def run_probe(
    *,
    dsn: str,
    tenant_id: UUID,
    workspace_id: UUID,
    qdrant_url: str,
    collection: str,
    dimension: int,
) -> dict[str, object]:
    """Rebuild one restored workspace and require a dense retrieval result."""
    container = build_postgres_container(
        dsn,
        server_id=tenant_id,
        project_id=workspace_id,
        qdrant_url=qdrant_url,
        qdrant_collection=collection,
        qdrant_dim=dimension,
        require_qdrant=True,
    )
    items = tuple(
        item
        for item in container.store.list_for_workspace(tenant_id, workspace_id)
        if item.status not in {MemoryStatus.ARCHIVED, MemoryStatus.REJECTED}
    )
    if not items:
        raise RuntimeError("restored workspace has no active memory to reindex and probe")

    indexed = container.embedding.reindex_all(tenant_id, workspace_id)
    verified = container.embedding.indexed_workspace_count(tenant_id, workspace_id)
    if indexed != verified:
        raise RuntimeError(
            f"restored reindex count mismatch: indexed={indexed}, verified={verified}"
        )

    probe = items[0]
    query = " ".join(probe.text.split()[:32])
    recall = container.retrieval.recall(
        RecallQuery(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            text=query,
            top_k=5,
        )
    )
    candidate = next((row for row in recall.candidates if row.item.id == probe.id), None)
    dense_used = "qdrant_hybrid" in recall.sources_used
    semantic_ok = bool(candidate and candidate.semantic > 0 and dense_used)
    checks = [
        {"name": "restored-reindex", "ok": indexed == verified},
        {"name": "semantic-recall", "ok": semantic_ok},
    ]
    return {
        "format": REPORT_FORMAT,
        "ok": all(bool(check["ok"]) for check in checks),
        "completed_at": datetime.now(UTC).isoformat(),
        "tenant_id": str(tenant_id),
        "workspace_id": str(workspace_id),
        "collection": collection,
        "embedding_model": container.embedding.model_name,
        "embedding_dimension": dimension,
        "indexed_points": indexed,
        "verified_points": verified,
        "semantic_sources": list(recall.sources_used),
        "semantic_candidate_found": candidate is not None,
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", default=os.getenv("UAM_SERVER_ID"))
    parser.add_argument("--workspace-id", default=os.getenv("UAM_PROJECT_ID"))
    parser.add_argument("--qdrant-url", default=os.getenv("UAM_QDRANT_URL"))
    parser.add_argument("--collection", required=True)
    parser.add_argument(
        "--dimension", type=int, default=int(os.getenv("UAM_EMBEDDING_DIM", "1536"))
    )
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    dsn = read_database_dsn()
    if not dsn or not args.tenant_id or not args.workspace_id or not args.qdrant_url:
        parser.error("database, tenant, workspace and Qdrant configuration are required")

    report = run_probe(
        dsn=dsn,
        tenant_id=UUID(args.tenant_id),
        workspace_id=UUID(args.workspace_id),
        qdrant_url=args.qdrant_url,
        collection=args.collection,
        dimension=args.dimension,
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
