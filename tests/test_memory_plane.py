from __future__ import annotations

import unittest
from uuid import uuid4

from memory_plane.bootstrap import build_in_memory_container
from memory_plane.contracts.dto import (
    Candidate,
    ContextRecipe,
    IngestDocumentCommand,
    RecallQuery,
    RetainCommand,
    SupersedeMemoryCommand,
)
from memory_plane.contracts.events import IntegrationEvent
from memory_plane.domain.conflict import ConflictReviewStatus
from memory_plane.domain.graph import MemoryEdgeType
from memory_plane.domain.models import (
    MemoryLayer,
    MemoryRevisionConflictError,
    MemoryScope,
    MemoryStatus,
    Provenance,
)
from memory_plane.services.retrieval import RetrievalService
from memory_plane.workers.handlers import RetainedEventRouter


class _FailingCandidateSource:
    name = "optional_vector"

    def search(self, _query: RecallQuery) -> tuple[Candidate, ...]:
        raise ConnectionError("vector dependency unavailable")


class _RecoveringCandidateSource:
    name = "recovering_vector"

    def __init__(self) -> None:
        self.calls = 0

    def search(self, _query: RecallQuery) -> tuple[Candidate, ...]:
        self.calls += 1
        if self.calls == 1:
            raise ConnectionError("temporary vector outage")
        return ()


class MemoryPlaneTest(unittest.TestCase):
    def setUp(self) -> None:
        self.container = build_in_memory_container()
        self.tenant = uuid4()
        self.workspace = uuid4()
        self.agent = uuid4()

    def retain(
        self,
        text: str,
        *,
        layer: MemoryLayer = MemoryLayer.SEMANTIC,
        key: str | None = None,
        tenant=None,
        workspace=None,
        status: MemoryStatus = MemoryStatus.ACTIVE,
    ):
        return self.container.retention.retain(
            RetainCommand(
                tenant_id=tenant or self.tenant,
                workspace_id=workspace or self.workspace,
                agent_id=self.agent,
                layer=layer,
                scope=MemoryScope.WORKSPACE,
                kind="fact",
                text=text,
                provenance=Provenance(source_kind="test"),
                status=status,
                idempotency_key=key,
            )
        )

    def test_retain_is_idempotent_and_emits_once(self) -> None:
        first = self.retain("Alpha release is July 15", key="turn-1")
        second = self.retain("different retry body", key="turn-1")

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.item.id, second.item.id)
        self.assertEqual(1, len(self.container.store.events))

    def test_idempotency_key_is_isolated_by_workspace(self) -> None:
        first = self.retain("Workspace one", key="same-client-key")
        second = self.retain("Workspace two", key="same-client-key", workspace=uuid4())

        self.assertTrue(first.created)
        self.assertTrue(second.created)
        self.assertNotEqual(first.item.id, second.item.id)

    def test_supersede_memory_uses_optimistic_revision(self) -> None:
        first = self.retain("Alpha release is July 15")

        updated = self.container.retention.supersede(
            SupersedeMemoryCommand(
                tenant_id=self.tenant,
                item_id=first.item.id,
                replacement_text="Alpha release is July 16",
                expected_revision=1,
            )
        )

        self.assertTrue(updated.created)
        self.assertEqual(2, updated.item.revision)
        self.assertEqual(first.item.id, updated.item.supersedes_id)
        self.assertEqual(2, len(self.container.store.events))
        self.assertEqual("memory.retained.v1", self.container.store.events[-1].name)

        recalled = self.container.retrieval.recall(
            RecallQuery(
                tenant_id=self.tenant,
                workspace_id=self.workspace,
                text="Alpha release July",
            )
        )
        self.assertEqual((updated.item.id,), tuple(row.item.id for row in recalled.candidates))
        self.assertFalse(self.container.store.is_recallable_head(self.tenant, first.item.id))
        self.assertTrue(self.container.store.is_recallable_head(self.tenant, updated.item.id))

    def test_supersede_rejects_stale_revision(self) -> None:
        first = self.retain("Alpha release is July 15")
        self.container.retention.supersede(
            SupersedeMemoryCommand(
                tenant_id=self.tenant,
                item_id=first.item.id,
                replacement_text="Alpha release is July 16",
                expected_revision=1,
            )
        )

        with self.assertRaises(MemoryRevisionConflictError) as raised:
            self.container.retention.supersede(
                SupersedeMemoryCommand(
                    tenant_id=self.tenant,
                    item_id=first.item.id,
                    replacement_text="Alpha release is July 17",
                    expected_revision=1,
                )
            )

        self.assertEqual(1, raised.exception.expected)
        self.assertEqual(2, raised.exception.actual)

    def test_supersede_retry_with_idempotency_key_returns_existing_revision(self) -> None:
        first = self.retain("Alpha release is July 15")
        command = SupersedeMemoryCommand(
            tenant_id=self.tenant,
            item_id=first.item.id,
            replacement_text="Alpha release is July 16",
            expected_revision=1,
            idempotency_key="supersede-alpha",
        )

        created = self.container.retention.supersede(command)
        retry = self.container.retention.supersede(command)

        self.assertTrue(created.created)
        self.assertFalse(retry.created)
        self.assertEqual(created.item.id, retry.item.id)
        self.assertEqual(2, len(self.container.store.events))

    def test_recall_enforces_tenant_and_ranks_lexical_match(self) -> None:
        expected = self.retain("Ivan owns the Alpha release")
        self.retain("Unrelated private tenant fact", tenant=uuid4())

        result = self.container.retrieval.recall(
            RecallQuery(
                tenant_id=self.tenant,
                workspace_id=self.workspace,
                text="Who owns Alpha release?",
            )
        )

        self.assertEqual(expected.item.id, result.candidates[0].item.id)
        self.assertTrue(all(row.item.tenant_id == self.tenant for row in result.candidates))

    def test_recall_falls_back_when_optional_candidate_source_fails(self) -> None:
        expected = self.retain("PostgreSQL fallback survives vector outage")
        retrieval = RetrievalService((self.container.store, _FailingCandidateSource()))

        result = retrieval.recall(
            RecallQuery(
                tenant_id=self.tenant,
                workspace_id=self.workspace,
                text="vector outage fallback",
            )
        )

        self.assertEqual((expected.item.id,), tuple(row.item.id for row in result.candidates))
        self.assertEqual((self.container.store.name,), result.sources_used)
        self.assertEqual(
            "degraded",
            retrieval.source_health()["optional_vector"]["status"],
        )

    def test_recall_propagates_required_canonical_source_failure(self) -> None:
        retrieval = RetrievalService(
            (_FailingCandidateSource(),),
            required_sources=frozenset({"optional_vector"}),
        )

        with self.assertRaises(ConnectionError):
            retrieval.recall(
                RecallQuery(
                    tenant_id=self.tenant,
                    workspace_id=self.workspace,
                    text="required source",
                )
            )

        self.assertEqual("failed", retrieval.source_health()["optional_vector"]["status"])

    def test_optional_source_health_recovers_after_success(self) -> None:
        source = _RecoveringCandidateSource()
        retrieval = RetrievalService((self.container.store, source))
        query = RecallQuery(
            tenant_id=self.tenant,
            workspace_id=self.workspace,
            text="dependency recovery",
        )

        retrieval.recall(query)
        retrieval.recall(query)

        state = retrieval.source_health()[source.name]
        self.assertEqual("healthy", state["status"])
        self.assertEqual(1, state["failures"])
        self.assertIsNone(state["error_type"])

    def test_recall_excludes_rejected_and_archived_memory(self) -> None:
        self.retain("Alpha secret rejected", status=MemoryStatus.REJECTED)
        self.retain("Alpha old archived", status=MemoryStatus.ARCHIVED)
        active = self.retain("Alpha active visible", status=MemoryStatus.ACTIVE)

        result = self.container.retrieval.recall(
            RecallQuery(
                tenant_id=self.tenant,
                workspace_id=self.workspace,
                text="Alpha",
            )
        )

        self.assertEqual((active.item.id,), tuple(row.item.id for row in result.candidates))

    def test_recall_demotes_disputed_memory_below_active_memory(self) -> None:
        disputed = self.retain("Alpha deployment uses blue host", status=MemoryStatus.DISPUTED)
        active = self.retain("Alpha deployment uses green host", status=MemoryStatus.ACTIVE)

        result = self.container.retrieval.recall(
            RecallQuery(
                tenant_id=self.tenant,
                workspace_id=self.workspace,
                text="Alpha deployment host",
            )
        )

        self.assertEqual(active.item.id, result.candidates[0].item.id)
        by_id = {row.item.id: row for row in result.candidates}
        self.assertLess(by_id[disputed.item.id].final_score, by_id[active.item.id].final_score)

    def test_pinned_memory_must_be_core(self) -> None:
        with self.assertRaises(ValueError):
            self.retain("Pinned semantic is invalid", status=MemoryStatus.PINNED)

    def test_graph_links_and_lists_neighbors(self) -> None:
        source = self.retain("Alpha release date is disputed")
        target = self.retain("Alpha release date is July 16")

        edge = self.container.graph.link(
            tenant_id=self.tenant,
            workspace_id=self.workspace,
            src_id=source.item.id,
            dst_id=target.item.id,
            edge_type=MemoryEdgeType.CONTRADICTS,
            weight=0.8,
        )
        neighbors = self.container.graph.neighbors(
            tenant_id=self.tenant,
            workspace_id=self.workspace,
            item_id=source.item.id,
        )

        self.assertEqual(MemoryEdgeType.CONTRADICTS, edge.edge_type)
        self.assertEqual((edge.id,), tuple(row.id for row in neighbors))

    def test_graph_rejects_cross_workspace_edges(self) -> None:
        source = self.retain("Alpha source")
        other_workspace = uuid4()
        target = self.container.retention.retain(
            RetainCommand(
                tenant_id=self.tenant,
                workspace_id=other_workspace,
                agent_id=self.agent,
                layer=MemoryLayer.SEMANTIC,
                scope=MemoryScope.WORKSPACE,
                kind="fact",
                text="Other workspace target",
                provenance=Provenance(source_kind="test"),
            )
        )

        with self.assertRaises(ValueError):
            self.container.graph.link(
                tenant_id=self.tenant,
                workspace_id=self.workspace,
                src_id=source.item.id,
                dst_id=target.item.id,
                edge_type=MemoryEdgeType.SUPPORTS,
            )

    def test_recall_hides_thread_memory_without_matching_thread(self) -> None:
        thread = uuid4()
        self.container.retention.retain(
            RetainCommand(
                tenant_id=self.tenant,
                workspace_id=self.workspace,
                thread_id=thread,
                layer=MemoryLayer.WORKING,
                scope=MemoryScope.THREAD,
                kind="note",
                text="Thread-only launch code",
                provenance=Provenance(source_kind="test"),
            )
        )

        without_thread = self.container.retrieval.recall(
            RecallQuery(
                tenant_id=self.tenant,
                workspace_id=self.workspace,
                text="launch code",
            )
        )
        with_thread = self.container.retrieval.recall(
            RecallQuery(
                tenant_id=self.tenant,
                workspace_id=self.workspace,
                thread_id=thread,
                text="launch code",
            )
        )

        self.assertEqual((), without_thread.candidates)
        self.assertEqual(1, len(with_thread.candidates))

    def test_context_compiler_honors_budget_and_layer_priority(self) -> None:
        self.retain("Always obey workspace policy", layer=MemoryLayer.CORE)
        self.retain("Alpha release fact")
        recall = self.container.retrieval.recall(
            RecallQuery(
                tenant_id=self.tenant,
                workspace_id=self.workspace,
                text="Alpha workspace policy",
            )
        )
        package = self.container.context.compile(
            recall,
            ContextRecipe(
                operation="planner",
                budget_tokens=128,
                layer_order=(MemoryLayer.SEMANTIC,),
            ),
        )

        self.assertLessEqual(package.used_tokens, package.budget_tokens)
        self.assertEqual("core", package.sections[0].name)
        self.assertIn("workspace policy", package.render_markdown())

    def test_reflection_preserves_two_evidence_items(self) -> None:
        self.retain("Release Alpha is July 15.")
        self.retain(" release  alpha  is july 15! ")

        observations = self.container.reflection.reflect(self.tenant, self.workspace)

        self.assertEqual(1, len(observations))
        self.assertEqual(2, len(observations[0].evidence_ids))

    def test_reflection_marks_older_conflicting_time_fact_stale(self) -> None:
        self.retain("Release Alpha is July 15.")
        latest = self.retain("Release Alpha is July 16.")

        observations = self.container.reflection.reflect(self.tenant, self.workspace)

        self.assertEqual(2, len(observations))
        stale = [row for row in observations if row.stale]
        current = [row for row in observations if not row.stale]
        self.assertEqual(["Release Alpha is July 15."], [row.summary for row in stale])
        self.assertEqual([latest.item.text], [row.summary for row in current])
        self.assertLess(stale[0].confidence, 0.7)

    def test_reflection_detects_entity_owner_conflict(self) -> None:
        old = self.retain("Ivan owns Alpha release.")
        new = self.retain("Alex owns Alpha release.")

        observations = self.container.reflection.reflect(self.tenant, self.workspace)

        self.assertEqual(2, len(observations))
        by_summary = {row.summary: row for row in observations}
        self.assertTrue(by_summary[old.item.text].stale)
        self.assertFalse(by_summary[new.item.text].stale)
        self.assertEqual((old.item.id,), by_summary[old.item.text].evidence_ids)
        self.assertEqual((new.item.id,), by_summary[new.item.text].evidence_ids)

    def test_conflict_inbox_lists_candidates_and_suggests_active_winner(self) -> None:
        old = self.retain("Release Alpha is July 15.")
        new = self.retain("Release Alpha is July 16.")

        cases = self.container.conflicts.list_cases(self.tenant, self.workspace)

        self.assertEqual(1, len(cases))
        case = cases[0]
        self.assertEqual("release alpha", case.subject)
        self.assertEqual("state", case.predicate)
        self.assertEqual("july 16", case.suggested_winner_value)
        by_value = {candidate.value: candidate for candidate in case.candidates}
        self.assertEqual("stale", by_value["july 15"].status)
        self.assertEqual("active", by_value["july 16"].status)
        self.assertEqual((old.item.id,), by_value["july 15"].evidence_ids)
        self.assertEqual((new.item.id,), by_value["july 16"].evidence_ids)

    def test_conflict_review_decision_is_persisted_and_filters_resolved(self) -> None:
        old = self.retain("Ivan owns Alpha release.")
        newest = self.retain("Alex owns Alpha release.")
        case = self.container.conflicts.list_cases(self.tenant, self.workspace)[0]

        decision = self.container.conflicts.decide(
            self.tenant,
            self.workspace,
            case.id,
            status=ConflictReviewStatus.ACCEPTED,
            winner_value=case.suggested_winner_value,
            reason="newer explicit correction",
        )

        self.assertEqual(ConflictReviewStatus.ACCEPTED, decision.status)
        self.assertEqual(newest.item.id, decision.applied_memory_id)
        recalled = self.container.retrieval.recall(
            RecallQuery(
                tenant_id=self.tenant,
                workspace_id=self.workspace,
                text="owner Alpha release",
            )
        )
        self.assertEqual((newest.item.id,), tuple(row.item.id for row in recalled.candidates))
        self.assertFalse(self.container.store.is_recallable_head(self.tenant, old.item.id))
        self.assertEqual((), self.container.conflicts.list_cases(self.tenant, self.workspace))
        with_resolved = self.container.conflicts.list_cases(
            self.tenant,
            self.workspace,
            include_resolved=True,
        )
        self.assertEqual("accepted", with_resolved[0].review_status)

        event_count = len(self.container.store.events)
        retry = self.container.conflicts.decide(
            self.tenant,
            self.workspace,
            case.id,
            status=ConflictReviewStatus.ACCEPTED,
            winner_value=case.suggested_winner_value,
            reason="idempotent retry",
        )
        self.assertEqual(decision.applied_memory_id, retry.applied_memory_id)
        self.assertEqual(event_count, len(self.container.store.events))

    def test_conflict_override_archives_newer_loser_and_keeps_selected_winner(self) -> None:
        selected = self.retain("Release Alpha is July 15.")
        newer = self.retain("Release Alpha is July 16.")
        case = self.container.conflicts.list_cases(self.tenant, self.workspace)[0]

        decision = self.container.conflicts.decide(
            self.tenant,
            self.workspace,
            case.id,
            status=ConflictReviewStatus.OVERRIDDEN,
            winner_value="july 15",
            reason="Operator verified the signed release plan.",
        )

        recalled = self.container.retrieval.recall(
            RecallQuery(
                tenant_id=self.tenant,
                workspace_id=self.workspace,
                text="Release Alpha July",
            )
        )
        self.assertEqual(selected.item.id, decision.applied_memory_id)
        self.assertEqual((selected.item.id,), tuple(row.item.id for row in recalled.candidates))
        self.assertFalse(self.container.store.is_recallable_head(self.tenant, newer.item.id))

    def test_reflection_is_idempotent_across_repeated_runs(self) -> None:
        self.retain("Release Alpha is July 15.")
        self.retain(" release  alpha  is july 15! ")

        first = self.container.reflection.reflect(self.tenant, self.workspace)
        second = self.container.reflection.reflect(self.tenant, self.workspace)

        self.assertEqual(1, len(first))
        self.assertEqual((), second)

    def test_worker_router_dispatches_registered_jobs(self) -> None:
        calls: list[str] = []
        router = RetainedEventRouter({"embed": lambda event: calls.append(event.name)})
        event = IntegrationEvent(
            name="memory.retained.v1",
            tenant_id=self.tenant,
            workspace_id=self.workspace,
            payload={"jobs": ["embed", "unknown"]},
        )

        completed = router.handle(event)

        self.assertEqual(("embed",), completed)
        self.assertEqual(["memory.retained.v1"], calls)

    def test_ingestion_is_stably_chunked_and_idempotent(self) -> None:
        command = IngestDocumentCommand(
            tenant_id=self.tenant,
            workspace_id=self.workspace,
            text=("Alpha requirements. " * 80) + "\n\n" + ("Release checklist. " * 80),
            origin_uri="file:///alpha.md",
            chunk_size_chars=300,
            chunk_overlap_chars=30,
        )
        first = self.container.ingestion.ingest_text(command)
        second = self.container.ingestion.ingest_text(command)

        self.assertGreater(first.created_count, 1)
        self.assertEqual(0, second.created_count)
        self.assertEqual(first.memory_ids, second.memory_ids)


if __name__ == "__main__":
    unittest.main()
