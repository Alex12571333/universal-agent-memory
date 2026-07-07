"""Pre-ingest secrets and high-risk PII guard."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class PrivacyAction(StrEnum):
    """Configured action when sensitive data is detected."""

    ALLOW = "allow"
    REDACT = "redact"
    REJECT = "reject"
    METADATA_ONLY = "metadata_only"


@dataclass(frozen=True, slots=True)
class PrivacyFinding:
    """One detector hit without retaining the raw matched secret."""

    kind: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class PrivacyDecision:
    """Sanitized text and audit metadata produced by the guard."""

    text: str
    findings: tuple[PrivacyFinding, ...]
    action: PrivacyAction
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _Detector:
    kind: str
    pattern: re.Pattern[str]
    validator: object | None = None


class PrivacyGuard:
    """Deterministic regex guard for secrets/high-risk PII."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        action: PrivacyAction = PrivacyAction.REDACT,
    ) -> None:
        """Create a guard with one policy action."""
        self._enabled = enabled
        self._action = action

    @classmethod
    def from_env(cls) -> PrivacyGuard:
        """Load guard config from `UAM_PRIVACY_*` env vars."""
        enabled = os.getenv("UAM_PRIVACY_ENABLED", "true").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        action = PrivacyAction(os.getenv("UAM_PRIVACY_ACTION", "redact").lower())
        return cls(enabled=enabled, action=action)

    def apply(self, text: str) -> PrivacyDecision:
        """Apply configured policy to text before canonical retention."""
        if not self._enabled:
            return PrivacyDecision(text=text, findings=(), action=PrivacyAction.ALLOW)
        findings = self.scan(text)
        if not findings:
            return PrivacyDecision(text=text, findings=(), action=PrivacyAction.ALLOW)
        if self._action == PrivacyAction.REJECT:
            kinds = ", ".join(sorted({finding.kind for finding in findings}))
            raise ValueError(f"privacy guard rejected sensitive content: {kinds}")
        if self._action == PrivacyAction.METADATA_ONLY:
            sanitized = "[content withheld by privacy guard]"
        elif self._action == PrivacyAction.REDACT:
            sanitized = self._redact(text, findings)
        else:
            sanitized = text
        return PrivacyDecision(
            text=sanitized,
            findings=findings,
            action=self._action,
            metadata=self._metadata(findings, self._action),
        )

    def scan(self, text: str) -> tuple[PrivacyFinding, ...]:
        """Return sorted non-overlapping findings."""
        findings: list[PrivacyFinding] = []
        for detector in _DETECTORS:
            for match in detector.pattern.finditer(text):
                if detector.validator is _luhn_valid and not _luhn_valid(match.group(0)):
                    continue
                findings.append(
                    PrivacyFinding(
                        kind=detector.kind,
                        start=match.start(),
                        end=match.end(),
                    )
                )
        findings.sort(key=lambda row: (row.start, -(row.end - row.start)))
        selected: list[PrivacyFinding] = []
        covered_until = -1
        for finding in findings:
            if finding.start < covered_until:
                continue
            selected.append(finding)
            covered_until = finding.end
        return tuple(selected)

    @staticmethod
    def _redact(text: str, findings: tuple[PrivacyFinding, ...]) -> str:
        sanitized = text
        for finding in sorted(findings, key=lambda row: row.start, reverse=True):
            sanitized = (
                sanitized[: finding.start]
                + f"[REDACTED:{finding.kind}]"
                + sanitized[finding.end :]
            )
        return sanitized

    @staticmethod
    def _metadata(
        findings: tuple[PrivacyFinding, ...],
        action: PrivacyAction,
    ) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for finding in findings:
            counts[finding.kind] = counts.get(finding.kind, 0) + 1
        return {
            "privacy": {
                "action": action.value,
                "finding_count": len(findings),
                "finding_kinds": sorted(counts),
                "counts": counts,
            }
        }


def _luhn_valid(raw: str) -> bool:
    digits = [int(ch) for ch in raw if ch.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    checksum = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


_DETECTORS = (
    _Detector(
        "private_key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----"),
    ),
    _Detector("openai_api_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    _Detector("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    _Detector(
        "bearer_token",
        re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    ),
    _Detector(
        "password_assignment",
        re.compile(r"(?i)\bpassword\s*[:=]\s*['\"]?[^'\"\s]{8,}"),
    ),
    _Detector(
        "api_key_assignment",
        re.compile(r"(?i)\b(?:api[_-]?key|token)\s*[:=]\s*['\"]?[A-Za-z0-9._~+/=-]{16,}"),
    ),
    _Detector("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    _Detector(
        "payment_card",
        re.compile(r"\b(?:\d[ -]*?){13,19}\b"),
        validator=_luhn_valid,
    ),
)
