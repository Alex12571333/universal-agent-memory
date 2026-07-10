from __future__ import annotations

import os
import unittest
from uuid import UUID

from qdrant_client.http.exceptions import ResponseHandlingException

from memory_plane.bootstrap import build_postgres_container
from memory_plane.contracts.dto import RecallQuery, RetainCommand
from memory_plane.domain.models import MemoryLayer, MemoryScope, Provenance

DATABASE_URL = os.getenv("UAM_TEST_DATABASE_URL")
UNREACHABLE_QDRANT = "http://127.0.0.1:1"
TENANT_ID = UUID("00000000-0000-0000-0000-000000000001")
WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000002")


@unittest.skipUnless(DATABASE_URL, "set UAM_TEST_DATABASE_URL to run PostgreSQL tests")
class FailSoftRuntimeIntegrationTest(unittest.TestCase):
    def test_api_container_starts_and_recalls_when_qdrant_is_unreachable(self) -> None:
        container = build_postgres_container(
            DATABASE_URL or "",
            server_id=TENANT_ID,
            project_id=WORKSPACE_ID,
            qdrant_url=UNREACHABLE_QDRANT,
        )
        retained = container.retention.retain(
            RetainCommand(
                tenant_id=TENANT_ID,
                workspace_id=WORKSPACE_ID,
                layer=MemoryLayer.SEMANTIC,
                scope=MemoryScope.WORKSPACE,
                kind="fact",
                text="canonical fallback remains available",
                provenance=Provenance(source_kind="integration-test"),
            )
        )

        recalled = container.retrieval.recall(
            RecallQuery(
                tenant_id=TENANT_ID,
                workspace_id=WORKSPACE_ID,
                text="canonical fallback available",
            )
        )

        self.assertIn(retained.item.id, tuple(row.item.id for row in recalled.candidates))
        self.assertEqual(("postgres_lexical",), recalled.sources_used)
        self.assertEqual(
            "degraded",
            container.retrieval.source_health()["qdrant_hybrid"]["status"],
        )

    def test_embedding_worker_container_requires_qdrant(self) -> None:
        with self.assertRaises(ResponseHandlingException):
            build_postgres_container(
                DATABASE_URL or "",
                server_id=TENANT_ID,
                project_id=WORKSPACE_ID,
                qdrant_url=UNREACHABLE_QDRANT,
                require_qdrant=True,
            )
