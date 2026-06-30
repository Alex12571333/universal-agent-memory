from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from memory_plane.domain.models import MemoryItem, MemoryLayer, MemoryScope, Provenance


def item(**overrides):
    values = {
        "tenant_id": uuid4(),
        "workspace_id": uuid4(),
        "layer": MemoryLayer.SEMANTIC,
        "scope": MemoryScope.WORKSPACE,
        "kind": "fact",
        "text": "A fact",
        "provenance": Provenance(source_kind="test"),
    }
    values.update(overrides)
    return MemoryItem(**values)


class DomainTest(unittest.TestCase):
    def test_thread_scope_requires_thread(self) -> None:
        with self.assertRaises(ValueError):
            item(scope=MemoryScope.THREAD)

    def test_temporal_validity_is_half_open(self) -> None:
        start = datetime.now(UTC)
        memory = item(valid_from=start, valid_to=start + timedelta(days=1))
        self.assertTrue(memory.is_valid_at(start))
        self.assertFalse(memory.is_valid_at(start + timedelta(days=1)))

    def test_supersede_creates_new_identity_and_revision(self) -> None:
        old = item()
        new = old.supersede("Corrected fact")
        self.assertNotEqual(old.id, new.id)
        self.assertEqual(old.id, new.supersedes_id)
        self.assertEqual(2, new.revision)
        self.assertEqual("A fact", old.text)


if __name__ == "__main__":
    unittest.main()
