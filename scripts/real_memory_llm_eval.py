"""Live regression checks for an OpenAI-compatible memory LLM endpoint."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from memory_plane.adapters.llm import MemoryLLMClient, MemoryLLMConfig
from memory_plane.config.secrets import read_secret_env

DEFAULT_CONTEXT_WINDOW_TOKENS = 8192


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
    generated_at: str
    provider: str
    base_url: str
    model: str
    config_fingerprint: str
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
        generated_at=datetime.now(UTC).isoformat(),
        provider=client.config.provider,
        base_url=client.config.base_url,
        model=client.config.model_name,
        config_fingerprint=_config_fingerprint(client.config),
        checks=checks,
    )


def _config_fingerprint(config: MemoryLLMConfig) -> str:
    """Hash non-secret routing and generation settings used by the live eval."""
    payload = {
        "provider": config.provider,
        "base_url": config.base_url,
        "model": config.model_name,
        "timeout_seconds": config.timeout_seconds,
        "temperature": config.temperature,
        "context_window_tokens": config.context_window_tokens,
        "max_tokens": config.max_tokens,
        "extra_body": config.extra_body or {},
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
                    "- Старое: резервные копии проекта хранят 30 дней.\n"
                    "- Новое: резервные копии проекта хранят 365 дней.\n"
                    "- Новое утверждение явно заменяет старое.\n\n"
                    "Задача: выбери, что сохранить как актуальную память. В поле "
                    "proposal верни только актуальный факт, без старого значения. "
                    'JSON schema: {"action":"retain|reject",'
                    '"proposal":"строка", "confidence": число от 0 до 1, '
                    '"tags":["строки"]}'
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
    if "30 " in proposal or "30 д" in proposal.lower():
        raise AssertionError(f"proposal preserved superseded retention value: {proposal!r}")
    if "365" not in proposal:
        raise AssertionError(f"proposal missed current retention value: {proposal!r}")
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
    parser.add_argument(
        "--base-url",
        default=os.getenv("UAM_MEMORY_LLM_BASE_URL", "http://localhost:8000/v1"),
    )
    parser.add_argument(
        "--model",
        default=os.getenv("UAM_MEMORY_LLM_MODEL", "memory-model"),
    )
    parser.add_argument(
        "--api-key",
        default=read_secret_env("UAM_MEMORY_LLM_API_KEY"),
    )
    parser.add_argument(
        "--provider",
        default=os.getenv("UAM_MEMORY_LLM_PROVIDER", "openai-compatible"),
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=float(os.getenv("UAM_MEMORY_LLM_TIMEOUT_SECONDS", "60")),
    )
    parser.add_argument(
        "--context-window-tokens",
        type=int,
        default=int(
            os.getenv("UAM_MEMORY_LLM_CONTEXT_WINDOW_TOKENS", DEFAULT_CONTEXT_WINDOW_TOKENS)
        ),
        help="Bounded context available to the maintenance model (default: 8192).",
    )
    parser.add_argument(
        "--extra-body-json",
        default=os.getenv("UAM_MEMORY_LLM_EXTRA_BODY_JSON", "{}"),
    )
    parser.add_argument("--json-report", type=Path)
    args = parser.parse_args()

    try:
        extra_body = json.loads(args.extra_body_json)
    except json.JSONDecodeError as exc:
        parser.error(f"--extra-body-json must be valid JSON: {exc}")
    if not isinstance(extra_body, dict):
        parser.error("--extra-body-json must be a JSON object")
    if args.context_window_tokens < 512:
        parser.error("--context-window-tokens must be at least 512")

    client = MemoryLLMClient(
        MemoryLLMConfig(
            provider=args.provider,
            model_name=args.model,
            base_url=args.base_url.rstrip("/"),
            api_key=args.api_key,
            timeout_seconds=args.timeout_seconds,
            temperature=0.0,
            context_window_tokens=args.context_window_tokens,
            max_tokens=1600,
            extra_body=extra_body or None,
        )
    )
    report = run_eval(client)
    if args.json_report:
        write_report(report, args.json_report)
    print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
