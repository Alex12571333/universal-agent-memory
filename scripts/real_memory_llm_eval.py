"""Live regression checks for the DGX Spark Qwen memory LLM endpoint."""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from memory_plane.adapters.llm import MemoryLLMClient, MemoryLLMConfig


@dataclass(frozen=True, slots=True)
class LLMCheck:
    """One live memory LLM check."""

    name: str
    ok: bool
    duration_ms: int
    detail: str


@dataclass(frozen=True, slots=True)
class LLMReport:
    """Machine-readable live LLM regression report."""

    format: str
    ok: bool
    base_url: str
    model: str
    checks: tuple[LLMCheck, ...]


def run_eval(client: MemoryLLMClient) -> LLMReport:
    """Run the live memory LLM regression suite."""
    checks = (
        _check("chat-completions", lambda: _check_chat(client)),
        _check("json-memory-curation", lambda: _check_json_curation(client)),
    )
    return LLMReport(
        format="obelisk-memory-llm-eval-v1",
        ok=all(check.ok for check in checks),
        base_url=client.config.base_url,
        model=client.config.model_name,
        checks=checks,
    )


def _check_chat(client: MemoryLLMClient) -> None:
    text = client.chat(
        [
            {
                "role": "system",
                "content": "Ты краткий runtime-checker системы памяти.",
            },
            {
                "role": "user",
                "content": "Ответь одним коротким русским словом без объяснений: память",
            },
        ],
        temperature=0.0,
        max_tokens=128,
    )
    if not text.strip():
        raise AssertionError("empty response")
    if "пам" not in text.lower():
        raise AssertionError(f"unexpected response: {text[:120]!r}")


def _check_json_curation(client: MemoryLLMClient) -> None:
    payload = client.chat_json(
        [
            {
                "role": "system",
                "content": (
                    "Ты Куратор памяти Obelisk Memory. Верни только JSON object. "
                    "Не добавляй markdown."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Контекст:\n"
                    "- Старое: production использует fake embeddings.\n"
                    "- Новое: production использует Jina embeddings v4 Q8_0 на DGX Spark .10.\n"
                    "- OpenClaw и Hermes подключаются через native plugin hooks.\n\n"
                    "Задача: выбери, что сохранить как актуальную память. "
                    "JSON schema: {\"action\":\"retain|reject\","
                    "\"proposal\":\"строка\", \"confidence\": число от 0 до 1, "
                    "\"tags\":[\"строки\"]}"
                ),
            },
        ],
        temperature=0.0,
        max_tokens=512,
    )
    action = str(payload.get("action", ""))
    proposal = str(payload.get("proposal", ""))
    confidence = payload.get("confidence")
    tags = payload.get("tags")
    if action != "retain":
        raise AssertionError(f"expected action=retain, got {action!r}")
    if "Jina" not in proposal and "jina" not in proposal:
        raise AssertionError(f"proposal missed current embedding model: {proposal!r}")
    if "fake" in proposal.lower():
        raise AssertionError(f"proposal preserved obsolete fake embedding claim: {proposal!r}")
    if not isinstance(confidence, int | float) or not 0 <= float(confidence) <= 1:
        raise AssertionError(f"invalid confidence: {confidence!r}")
    if not isinstance(tags, list) or not tags:
        raise AssertionError("tags must be a non-empty list")


def _check(name: str, fn: Any) -> LLMCheck:
    started = time.perf_counter()
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - eval reports every endpoint failure.
        return LLMCheck(
            name=name,
            ok=False,
            duration_ms=_elapsed_ms(started),
            detail=f"{type(exc).__name__}: {exc}",
        )
    return LLMCheck(name=name, ok=True, duration_ms=_elapsed_ms(started), detail="ok")


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def write_report(report: LLMReport, path: Path) -> None:
    """Write a JSON report."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(report), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://192.168.0.10:8000/v1")
    parser.add_argument("--model", default="qwen3.6-35b-a3b")
    parser.add_argument("--api-key")
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--json-report", type=Path)
    args = parser.parse_args()

    client = MemoryLLMClient(
        MemoryLLMConfig(
            provider="spark",
            model_name=args.model,
            base_url=args.base_url.rstrip("/"),
            api_key=args.api_key,
            timeout_seconds=args.timeout_seconds,
            temperature=0.0,
            context_window_tokens=131072,
            max_tokens=1600,
            enable_thinking=False,
        )
    )
    report = run_eval(client)
    if args.json_report:
        write_report(report, args.json_report)
    print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
