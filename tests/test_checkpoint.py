"""Unit tests for working-memory checkpoint CAS flow."""

from __future__ import annotations

from uuid import uuid4

import pytest

from memory_plane.adapters.in_memory import InMemoryCheckpointStore
from memory_plane.domain.checkpoint import Checkpoint, StaleRevisionError
from memory_plane.services.checkpoint import CheckpointService

TENANT = uuid4()
WORKSPACE = uuid4()


def _make_service() -> tuple[CheckpointService, InMemoryCheckpointStore]:
    store = InMemoryCheckpointStore()
    return CheckpointService(store), store


class TestCheckpointDomain:
    """Domain-level invariants."""

    def test_valid_checkpoint(self) -> None:
        cp = Checkpoint(
            tenant_id=TENANT,
            workspace_id=WORKSPACE,
            thread_id=uuid4(),
            revision=1,
            state={"plan": "step-1"},
        )
        assert cp.revision == 1

    def test_reject_zero_revision(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            Checkpoint(
                tenant_id=TENANT,
                workspace_id=WORKSPACE,
                thread_id=uuid4(),
                revision=0,
                state={},
            )

    def test_reject_negative_revision(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            Checkpoint(
                tenant_id=TENANT,
                workspace_id=WORKSPACE,
                thread_id=uuid4(),
                revision=-1,
                state={},
            )


class TestCheckpointService:
    """Service-level CAS guarantees."""

    def test_save_and_restore(self) -> None:
        svc, _ = _make_service()
        thread = uuid4()
        cp = svc.save(
            tenant_id=TENANT,
            workspace_id=WORKSPACE,
            thread_id=thread,
            state={"plan": "do-thing"},
        )
        assert cp.revision == 1
        assert cp.state == {"plan": "do-thing"}

        restored = svc.restore(tenant_id=TENANT, thread_id=thread)
        assert restored is not None
        assert restored.state == {"plan": "do-thing"}

    def test_auto_increment_revision(self) -> None:
        svc, _ = _make_service()
        thread = uuid4()
        cp1 = svc.save(
            tenant_id=TENANT,
            workspace_id=WORKSPACE,
            thread_id=thread,
            state={"step": 1},
        )
        assert cp1.revision == 1

        cp2 = svc.save(
            tenant_id=TENANT,
            workspace_id=WORKSPACE,
            thread_id=thread,
            state={"step": 2},
        )
        assert cp2.revision == 2

    def test_cas_update_success(self) -> None:
        svc, _ = _make_service()
        thread = uuid4()
        svc.save(
            tenant_id=TENANT,
            workspace_id=WORKSPACE,
            thread_id=thread,
            state={"v": 1},
        )
        cp2 = svc.update(
            tenant_id=TENANT,
            workspace_id=WORKSPACE,
            thread_id=thread,
            state={"v": 2},
            expected_revision=1,
        )
        assert cp2.revision == 2
        assert cp2.state == {"v": 2}

    def test_cas_stale_revision(self) -> None:
        svc, _ = _make_service()
        thread = uuid4()
        svc.save(
            tenant_id=TENANT,
            workspace_id=WORKSPACE,
            thread_id=thread,
            state={"v": 1},
        )
        with pytest.raises(StaleRevisionError) as exc_info:
            svc.update(
                tenant_id=TENANT,
                workspace_id=WORKSPACE,
                thread_id=thread,
                state={"v": "conflict"},
                expected_revision=99,
            )
        assert exc_info.value.expected == 99
        assert exc_info.value.actual == 1

    def test_concurrent_update_one_wins(self) -> None:
        svc, _ = _make_service()
        thread = uuid4()
        svc.save(
            tenant_id=TENANT,
            workspace_id=WORKSPACE,
            thread_id=thread,
            state={"v": 1},
        )
        # First update succeeds
        svc.update(
            tenant_id=TENANT,
            workspace_id=WORKSPACE,
            thread_id=thread,
            state={"v": "winner"},
            expected_revision=1,
        )
        # Second update with same expected revision fails
        with pytest.raises(StaleRevisionError):
            svc.update(
                tenant_id=TENANT,
                workspace_id=WORKSPACE,
                thread_id=thread,
                state={"v": "loser"},
                expected_revision=1,
            )

    def test_restore_specific_revision(self) -> None:
        svc, _ = _make_service()
        thread = uuid4()
        svc.save(
            tenant_id=TENANT,
            workspace_id=WORKSPACE,
            thread_id=thread,
            state={"version": "old"},
        )
        svc.save(
            tenant_id=TENANT,
            workspace_id=WORKSPACE,
            thread_id=thread,
            state={"version": "new"},
        )
        old = svc.restore_revision(tenant_id=TENANT, thread_id=thread, revision=1)
        assert old is not None
        assert old.state == {"version": "old"}

        head = svc.restore(tenant_id=TENANT, thread_id=thread)
        assert head is not None
        assert head.state == {"version": "new"}

    def test_compaction(self) -> None:
        svc, _ = _make_service()
        thread = uuid4()
        for i in range(1, 6):
            svc.save(
                tenant_id=TENANT,
                workspace_id=WORKSPACE,
                thread_id=thread,
                state={"step": i},
            )
        deleted = svc.compact(tenant_id=TENANT, thread_id=thread, keep_last=2)
        assert deleted == 3

        # Oldest revisions are gone
        assert svc.restore_revision(tenant_id=TENANT, thread_id=thread, revision=1) is None
        assert svc.restore_revision(tenant_id=TENANT, thread_id=thread, revision=3) is None
        # Newest survive
        assert svc.restore_revision(tenant_id=TENANT, thread_id=thread, revision=4) is not None
        assert svc.restore_revision(tenant_id=TENANT, thread_id=thread, revision=5) is not None

    def test_workspace_isolation(self) -> None:
        svc, _ = _make_service()
        ws_a, ws_b = uuid4(), uuid4()
        thread = uuid4()
        svc.save(
            tenant_id=TENANT,
            workspace_id=ws_a,
            thread_id=thread,
            state={"ws": "a"},
        )

        heads_a = svc.list_for_workspace(TENANT, ws_a)
        heads_b = svc.list_for_workspace(TENANT, ws_b)
        assert len(heads_a) == 1
        assert len(heads_b) == 0

    def test_thread_isolation(self) -> None:
        svc, _ = _make_service()
        thread_a, thread_b = uuid4(), uuid4()
        svc.save(
            tenant_id=TENANT,
            workspace_id=WORKSPACE,
            thread_id=thread_a,
            state={"t": "a"},
        )
        assert svc.restore(tenant_id=TENANT, thread_id=thread_a) is not None
        assert svc.restore(tenant_id=TENANT, thread_id=thread_b) is None

    def test_empty_restore_returns_none(self) -> None:
        svc, _ = _make_service()
        assert svc.restore(tenant_id=TENANT, thread_id=uuid4()) is None

    def test_update_nonexistent_raises(self) -> None:
        svc, _ = _make_service()
        with pytest.raises(StaleRevisionError) as exc_info:
            svc.update(
                tenant_id=TENANT,
                workspace_id=WORKSPACE,
                thread_id=uuid4(),
                state={"v": 1},
                expected_revision=1,
            )
        assert exc_info.value.actual is None
