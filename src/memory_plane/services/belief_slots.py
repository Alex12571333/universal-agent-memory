"""Conservative deterministic belief-slot extraction for memory maintenance."""

from __future__ import annotations

import re
from dataclasses import dataclass

_DATE_SEPARATORS = re.compile(r"[-/,]+")
_PUNCTUATION = re.compile(r"[^\w\s-]", re.UNICODE)
_TEMPORAL_TRANSITION = re.compile(
    r"\b(?:"
    r"раньше|сейчас|теперь|недавно|переш(?:ел|ёл|ла)|измен(?:ил|ила|илось)|"
    r"before|previously|formerly|now|recently|changed|switched"
    r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class BeliefSlot:
    """Comparable deterministic relation extracted from a single memory."""

    subject: str
    predicate: str
    value: str

    @property
    def key(self) -> str:
        """Group comparable facts about the same entity/relation."""
        return f"{self.subject}|{self.predicate}"


def extract_belief_slot(text: str) -> BeliefSlot:
    """Extract only unambiguous, non-temporal relations from memory text.

    A temporal transition stays an opaque statement. It must be handled through
    evidence-bound curation and operator review, never by a heuristic that can
    turn a historical preference into a timeless conflict winner.
    """
    normalized = normalize(text)
    if _TEMPORAL_TRANSITION.search(normalized):
        return BeliefSlot(subject=normalized, predicate="statement", value="true")

    owner = re.fullmatch(r"(?P<owner>.+?) owns (?P<thing>.+)", normalized)
    if owner:
        return BeliefSlot(
            subject=normalize_entity(owner.group("thing")),
            predicate="owner",
            value=normalize_entity(owner.group("owner")),
        )

    release_date = re.fullmatch(r"(?P<subject>.+?) releases? on (?P<value>.+)", normalized)
    if release_date:
        return BeliefSlot(
            subject=normalize_entity(release_date.group("subject")),
            predicate="release_date",
            value=normalize_value(release_date.group("value")),
        )

    preference = re.fullmatch(
        r"(?P<subject>.+?) (?:prefers?|likes?|предпочитает|любит) (?P<value>.+)",
        normalized,
    )
    if preference:
        return BeliefSlot(
            subject=normalize_entity(preference.group("subject")),
            predicate="preference",
            value=normalize_value(preference.group("value")),
        )

    state = re.fullmatch(
        r"(?P<subject>.+?) "
        r"(?:is|are|was|were|will be|является|являлся|являлась|был|была|будет) "
        r"(?P<value>.+)",
        normalized,
    )
    if state:
        return BeliefSlot(
            subject=normalize_entity(state.group("subject")),
            predicate="state",
            value=normalize_value(state.group("value")),
        )

    return BeliefSlot(subject=normalized, predicate="statement", value="true")


def normalize_entity(text: str) -> str:
    """Normalize an entity key while preserving enough meaning for audit."""
    return normalize(text.removeprefix("the "))


def normalize_value(text: str) -> str:
    """Normalize a comparable value, including common date separators."""
    return _DATE_SEPARATORS.sub(" ", normalize(text))


def normalize(text: str) -> str:
    """Normalize text only enough for safe exact-belief grouping."""
    text = _PUNCTUATION.sub(" ", text.casefold().strip())
    return " ".join(text.split())
