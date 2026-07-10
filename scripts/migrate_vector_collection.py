"""Build and verify a new Qdrant collection before an embedding model switch."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from memory_plane.bootstrap import build_postgres_container
from memory_plane.config.database import read_database_dsn

REPORT_FORMAT = "obelisk-vector-collection-migration-v1"


def migrate_collection(
    *,
    dsn: str,
    tenant_id: UUID,
    workspace_id: UUID,
    qdrant_url: str,
    target_collection: str,
    dimension: int,
) -> dict[str, Any]:
    """Populate a new collection and verify its scoped point count."""
    current_collection = os.getenv("UAM_QDRANT_COLLECTION", "memory_items")
    if target_collection == current_collection:
        raise ValueError("target collection must differ from the active collection")
    if not target_collection.strip():
        raise ValueError("target collection must not be empty")
    container = build_postgres_container(
        dsn,
        server_id=tenant_id,
        project_id=workspace_id,
        qdrant_url=qdrant_url,
        qdrant_dim=dimension,
        qdrant_collection=target_collection,
        require_qdrant=True,
    )
    indexed = container.embedding.reindex_all(tenant_id, workspace_id)
    verified = container.embedding.indexed_workspace_count(tenant_id, workspace_id)
    if verified != indexed:
        raise RuntimeError(
            f"target collection verification failed: indexed={indexed}, verified={verified}"
        )
    return {
        "format": REPORT_FORMAT,
        "ok": True,
        "completed_at": datetime.now(UTC).isoformat(),
        "tenant_id": str(tenant_id),
        "workspace_id": str(workspace_id),
        "source_collection": current_collection,
        "target_collection": target_collection,
        "embedding_model": container.embedding.model_name,
        "embedding_dimension": dimension,
        "indexed_points": indexed,
        "verified_points": verified,
        "activation": f"UAM_QDRANT_COLLECTION={target_collection}",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-collection", required=True)
    parser.add_argument("--tenant-id", default=os.getenv("UAM_SERVER_ID"))
    parser.add_argument("--workspace-id", default=os.getenv("UAM_PROJECT_ID"))
    parser.add_argument("--qdrant-url", default=os.getenv("UAM_QDRANT_URL"))
    parser.add_argument(
        "--dimension",
        type=int,
        default=int(os.getenv("UAM_EMBEDDING_DIM", "1536")),
    )
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    dsn = read_database_dsn()
    if not dsn or not args.tenant_id or not args.workspace_id or not args.qdrant_url:
        parser.error("database, tenant, workspace and Qdrant configuration are required")
    report = migrate_collection(
        dsn=dsn,
        tenant_id=UUID(args.tenant_id),
        workspace_id=UUID(args.workspace_id),
        qdrant_url=args.qdrant_url,
        target_collection=args.target_collection,
        dimension=args.dimension,
    )
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
