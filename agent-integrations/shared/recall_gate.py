"""Deterministic, model-free gate for automatic memory recall."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from threading import Lock

VALID_RECALL_MODES = frozenset({"off", "adaptive", "always"})

_WORD_RE = re.compile(r"[\w-]+", re.UNICODE)
_GREETING_RE = re.compile(
    r"^(?:привет(?:ствую)?|здравствуй(?:те)?|доброе\s+(?:утро|день|вечер)|"
    r"hello|hi|hey|good\s+(?:morning|afternoon|evening))[!,.?\s]*$",
    re.IGNORECASE,
)
_CALC_PREFIX_RE = re.compile(
    r"^(?:сколько\s+будет|посчитай|вычисли|calculate|what\s+is)\s*", re.IGNORECASE
)
_CALC_BODY_RE = re.compile(r"^[\d\s.,()+*/%\-^=]+$")
_TRANSLATION_RE = re.compile(
    r"^(?:переведи|перевод|translate)(?:\s+(?:на|в|to)\s+[\w-]+)?\s*[:\-]?\s+",
    re.IGNORECASE,
)

_EXPLICIT_MEMORY = (
    "помнишь",
    "вспомни",
    "что ты знаешь обо мне",
    "из памяти",
    "в памяти",
    "remember",
    "recall",
    "from memory",
    "what do you know about me",
)
_HISTORICAL = (
    "раньше",
    "прошлый",
    "прошлая",
    "прошлые",
    "до этого",
    "в прошлый раз",
    "мы использовали",
    "мы делали",
    "previous",
    "last time",
    "used before",
    "we used",
    "we did",
    "history",
)
_PERSONAL = (
    "мои настройки",
    "мои предпочтения",
    "мой профиль",
    "обо мне",
    "я предпочитаю",
    "my settings",
    "my preferences",
    "my profile",
    "about me",
    "i prefer",
)
_PROJECT = (
    "наш проект",
    "нашем проекте",
    "наша система",
    "нашей системе",
    "наш сервер",
    "нашем сервере",
    "наш репозиторий",
    "нашем репозитории",
    "этот проект",
    "этот репо",
    "в репозитории",
    "our project",
    "our system",
    "our server",
    "our repository",
    "this project",
    "this repo",
    "in the repository",
)
_AMBIGUOUS = (
    "та модель",
    "тот конфиг",
    "тот сервер",
    "тот проект",
    "это пофиксили",
    "там агенты",
    "как тогда",
    "как раньше",
    "that model",
    "that config",
    "that server",
    "that project",
    "the previous one",
    "as before",
)
_CONTINUATIONS = frozenset(
    {
        "продолжай",
        "продолжи",
        "дальше",
        "делай дальше",
        "continue",
        "continue please",
        "go on",
        "keep going",
    }
)
_SHORT_COMMANDS = frozenset(
    {
        "да",
        "нет",
        "ок",
        "окей",
        "делай",
        "готово",
        "спасибо",
        "yes",
        "no",
        "ok",
        "okay",
        "do it",
        "thanks",
        "thank you",
    }
)
_SELF_CONTAINED_PREFIXES = (
    "напиши ",
    "объясни ",
    "перечисли ",
    "создай ",
    "сгенерируй ",
    "сравни ",
    "что такое ",
    "почему ",
    "как сделать ",
    "write ",
    "explain ",
    "list ",
    "create ",
    "generate ",
    "compare ",
    "what is ",
    "why ",
    "how to ",
)


@dataclass(frozen=True, slots=True)
class RecallGateDecision:
    """One inspectable automatic-recall decision."""

    should_recall: bool
    reason: str
    tier: str = "none"


class RecallGateMetrics:
    """Bounded in-process counters for host diagnostics and tests."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._decisions: Counter[tuple[str, str, str]] = Counter()
        self._recalls = 0
        self._injected_tokens = 0
        self._latency_seconds = 0.0

    def record_decision(self, decision: RecallGateDecision) -> None:
        outcome = "recall" if decision.should_recall else "skip"
        with self._lock:
            self._decisions[(outcome, decision.reason, decision.tier)] += 1

    def record_recall(self, *, latency_seconds: float, injected_tokens: int) -> None:
        with self._lock:
            self._recalls += 1
            self._latency_seconds += max(0.0, latency_seconds)
            self._injected_tokens += max(0, injected_tokens)

    def snapshot(self) -> dict[str, object]:
        """Return a text-free snapshot suitable for a host metrics bridge."""
        with self._lock:
            decisions = {
                f"{outcome}:{reason}:{tier}": count
                for (outcome, reason, tier), count in sorted(self._decisions.items())
            }
            return {
                "decisions": decisions,
                "recalls_total": self._recalls,
                "injected_tokens_total": self._injected_tokens,
                "recall_latency_seconds_sum": self._latency_seconds,
            }


def evaluate_recall_gate(
    query: str,
    *,
    mode: str = "adaptive",
    has_live_context: bool | None = None,
    force_full_recall: bool = False,
) -> RecallGateDecision:
    """Classify a turn without a model, network call, or stored prompt text."""
    normalized_mode = mode.strip().lower()
    if normalized_mode not in VALID_RECALL_MODES:
        normalized_mode = "adaptive"
    text = " ".join(query.casefold().strip().split())

    if not text:
        return RecallGateDecision(False, "empty")
    if force_full_recall:
        return RecallGateDecision(True, "explicit_full_recall", "full")
    if normalized_mode == "off":
        return RecallGateDecision(False, "mode_off")
    if normalized_mode == "always":
        return RecallGateDecision(True, "mode_always", "full")
    if _contains_any(text, _EXPLICIT_MEMORY):
        return RecallGateDecision(True, "explicit_memory", "compact")
    if _contains_any(text, _HISTORICAL):
        return RecallGateDecision(True, "historical_reference", "compact")
    if _contains_any(text, _PERSONAL):
        return RecallGateDecision(True, "personal_context", "compact")
    if _contains_any(text, _PROJECT):
        return RecallGateDecision(True, "project_context", "compact")
    if _contains_any(text, _AMBIGUOUS):
        return RecallGateDecision(True, "ambiguous_reference", "compact")

    command = text.rstrip(".!?, ")
    if command in _CONTINUATIONS:
        if has_live_context is False:
            return RecallGateDecision(True, "continuation_with_context", "compact")
        return RecallGateDecision(False, "short_command")
    if _GREETING_RE.fullmatch(text):
        return RecallGateDecision(False, "greeting")

    arithmetic = _CALC_PREFIX_RE.sub("", text).strip().rstrip("?")
    if arithmetic and _CALC_BODY_RE.fullmatch(arithmetic) and any(
        operator in arithmetic for operator in "+-*/%^"
    ):
        return RecallGateDecision(False, "simple_calculation")

    words = _WORD_RE.findall(text)
    if _TRANSLATION_RE.match(text) and len(words) <= 16:
        return RecallGateDecision(False, "single_phrase_translation")
    if command in _SHORT_COMMANDS or (len(words) <= 2 and text.endswith("!")):
        return RecallGateDecision(False, "short_command")
    if text.startswith(_SELF_CONTAINED_PREFIXES) or len(words) >= 8:
        return RecallGateDecision(False, "self_contained")
    return RecallGateDecision(True, "conservative_fallback", "compact")


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)
