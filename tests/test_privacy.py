from __future__ import annotations

from uuid import uuid4

import pytest

from memory_plane.adapters.in_memory import InMemoryMemoryStore
from memory_plane.contracts.dto import RetainCommand
from memory_plane.domain.models import MemoryLayer, MemoryScope, Provenance
from memory_plane.services.privacy import PrivacyAction, PrivacyGuard
from memory_plane.services.retention import RetentionService


def test_privacy_guard_redacts_common_secret_fixtures() -> None:
    guard = PrivacyGuard(action=PrivacyAction.REDACT)
    decision = guard.apply(
        "OpenAI key sk-abcdefghijklmnopqrstuvwxyz123456 and "
        "password=supersecret42 and SSN 123-45-6789"
    )

    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in decision.text
    assert "supersecret42" not in decision.text
    assert "123-45-6789" not in decision.text
    assert "[REDACTED:openai_api_key]" in decision.text
    assert decision.metadata["privacy"]["finding_count"] == 3
    assert decision.metadata["privacy"]["action"] == "redact"


def test_privacy_guard_reject_policy_raises() -> None:
    guard = PrivacyGuard(action=PrivacyAction.REJECT)

    with pytest.raises(ValueError, match="privacy guard rejected"):
        guard.apply("Bearer abcdefghijklmnopqrstuvwxyz123456")


def test_privacy_guard_metadata_only_keeps_no_raw_content() -> None:
    guard = PrivacyGuard(action=PrivacyAction.METADATA_ONLY)
    decision = guard.apply("token=abcdefghijklmnopqrstuvwxyz123456")

    assert decision.text == "[content withheld by privacy guard]"
    assert decision.metadata["privacy"]["finding_kinds"] == ["api_key_assignment"]


def test_retention_applies_privacy_audit_metadata() -> None:
    store = InMemoryMemoryStore()
    service = RetentionService(store, privacy=PrivacyGuard(action=PrivacyAction.REDACT))

    result = service.retain(
        RetainCommand(
            tenant_id=uuid4(),
            workspace_id=uuid4(),
            layer=MemoryLayer.SEMANTIC,
            scope=MemoryScope.WORKSPACE,
            kind="log",
            text="Tool output leaked AKIAABCDEFGHIJKLMNOP in logs.",
            provenance=Provenance(source_kind="test"),
        )
    )

    assert "AKIAABCDEFGHIJKLMNOP" not in result.item.text
    assert "[REDACTED:aws_access_key]" in result.item.text
    assert result.item.metadata["privacy"]["counts"] == {"aws_access_key": 1}
