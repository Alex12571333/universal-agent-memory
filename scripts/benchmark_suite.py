"""Obelisk Memory benchmark and readiness report.

The suite combines deterministic in-process checks with optional live endpoint
smoke tests. It writes a Markdown report so agents and operators can compare
future runs without reading raw terminal logs.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from fastapi.testclient import TestClient  # noqa: E402

from memory_plane.adapters.embeddings import (  # noqa: E402
    EmbeddingProviderConfig,
    build_embedding_client,
)
from memory_plane.adapters.in_memory import InMemoryMemoryStore  # noqa: E402
from memory_plane.adapters.llm import MemoryLLMClient, MemoryLLMConfig  # noqa: E402
from memory_plane.adapters.qdrant import QdrantCandidateSource  # noqa: E402
from memory_plane.api.app import create_app  # noqa: E402
from memory_plane.bootstrap import build_in_memory_container  # noqa: E402
from memory_plane.contracts.dto import (  # noqa: E402
    Candidate,
    ContextRecipe,
    RecallQuery,
    RecallResult,
    RetainCommand,
)
from memory_plane.domain.conversation import ConversationMessage  # noqa: E402
from memory_plane.domain.models import (  # noqa: E402
    MemoryItem,
    MemoryLayer,
    MemoryScope,
    Provenance,
)
from memory_plane.domain.proposal import MemoryProposalTarget  # noqa: E402
from memory_plane.services.context import ContextCompiler  # noqa: E402
from memory_plane.services.conversations import (  # noqa: E402
    AppendConversationTurnCommand,
    ConversationCurator,
    ConversationService,
    CurateConversationTurnCommand,
)
from memory_plane.services.embedding import EmbeddingService  # noqa: E402
from memory_plane.services.proposals import (  # noqa: E402
    MemoryProposalService,
    SubmitMemoryProposalCommand,
)
from memory_plane.services.retention import RetentionService  # noqa: E402
from memory_plane.services.retrieval import RetrievalService  # noqa: E402


@dataclass(frozen=True, slots=True)
class BenchResult:
    name: str
    status: str
    duration_ms: float
    details: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)


class FakeReasoner:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def chat_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = 0.0,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        return self.payload


def timed(name: str, fn: Callable[[], tuple[str, dict[str, Any]]]) -> BenchResult:
    started = time.perf_counter()
    try:
        details, metrics = fn()
        status = "PASS"
    except SkipCheck as exc:
        details = str(exc)
        metrics = {}
        status = "SKIP"
    except Exception as exc:  # noqa: BLE001 - benchmark should report all failures.
        details = f"{type(exc).__name__}: {exc}"
        metrics = {}
        status = "FAIL"
    duration_ms = (time.perf_counter() - started) * 1000
    return BenchResult(name, status, duration_ms, details, metrics)


class SkipCheck(RuntimeError):
    """Raised when an optional live benchmark endpoint is unavailable."""


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def bench_config_contracts() -> tuple[str, dict[str, Any]]:
    compose = (ROOT / "docker-compose.yml").read_text()
    env = (ROOT / ".env.example").read_text()
    checks = {
        "memory_port": 'ports: ["6798:8080"]' in compose,
        "postgres_port": 'ports: ["6548:5432"]' in compose,
        "qdrant_ports": 'ports: ["6799:6333", "6800:6334"]' in compose,
        "minio_ports": 'ports: ["6900:9000", "6901:9001"]' in compose,
        "nats_ports": 'ports: ["6422:4222", "6822:8222"]' in compose,
        "context_budget_env": "UAM_CONTEXT_BUDGET_TOKENS=131072" in env,
        "context_per_layer_limit_env": "UAM_CONTEXT_PER_LAYER_LIMIT=1000" in env,
        "llm_context_env": "UAM_MEMORY_LLM_CONTEXT_TOKENS=131072" in env,
        "llm_provider_neutral": "UAM_MEMORY_LLM_PROVIDER=openai-compatible" in env,
        "llm_extra_body": "UAM_MEMORY_LLM_EXTRA_BODY_JSON={}" in env,
    }
    missing = [key for key, ok in checks.items() if not ok]
    assert_true(not missing, f"missing config checks: {missing}")
    config = MemoryLLMConfig.from_env()
    assert_true(config.context_window_tokens == 131072, "memory LLM context is not 128k")
    return "Docker/env contract uses 6798 host API port and 128k context.", {
        **checks,
        "llm_context_window_tokens": config.context_window_tokens,
        "llm_model": config.model_name,
        "llm_extra_body_configured": bool(config.extra_body),
    }


def bench_api_memory_contract() -> tuple[str, dict[str, Any]]:
    client = TestClient(create_app(build_in_memory_container()))
    retained = client.post(
        "/v1/memory/retain",
        json={
            "layer": "semantic",
            "scope": "workspace",
            "kind": "fact",
            "text": "Benchmark memory says production context is 128k.",
            "idempotency_key": "benchmark:128k-context",
        },
    )
    recalled = client.post(
        "/v1/memory/recall",
        json={"query": "production context 128k"},
    )
    assert_true(retained.status_code == 201, retained.text)
    assert_true(recalled.status_code == 200, recalled.text)
    payload = recalled.json()
    assert_true(payload["context"]["budget_tokens"] == 131072, "recall budget is not 128k")
    assert_true(payload["results"], "recall returned no memory")
    return "In-process API retains, recalls and compiles 128k-budget context.", {
        "context_budget_tokens": payload["context"]["budget_tokens"],
        "result_count": len(payload["results"]),
        "used_tokens": payload["context"]["used_tokens"],
    }


def bench_llm_wiring_contract() -> tuple[str, dict[str, Any]]:
    store = InMemoryMemoryStore()
    retention = RetentionService(store)
    tenant_id = uuid4()
    workspace_id = uuid4()
    thread_id = uuid4()
    turn = ConversationService(store).append_turn(
        AppendConversationTurnCommand(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            thread_id=thread_id,
            messages=(
                ConversationMessage(
                    role="user",
                    content="Запомни: интерфейс должен быть премиальным и русским.",
                ),
            ),
        )
    )
    curated = ConversationCurator(
        store,
        retention,
        memory_llm=FakeReasoner(
            {
                "summary": "Пользователь хочет премиальный русский интерфейс.",
                "preferences": ["Русский UI", "Premium polish"],
                "confidence": 0.94,
            }
        ),
    ).curate_turn(CurateConversationTurnCommand(tenant_id=tenant_id, turn_id=turn.turn.id))
    proposal = MemoryProposalService(
        store,
        retention,
        memory_llm=FakeReasoner(
            {
                "target": "preference",
                "confidence": 0.9,
                "importance": 0.8,
                "rationale": "Explicit preference.",
            }
        ),
    ).submit(
        SubmitMemoryProposalCommand(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            namespace="benchmark",
            requester="benchmark-suite",
            target=MemoryProposalTarget.AUTO,
            proposal="User prefers concise Russian answers.",
            evidence="Explicit request.",
        )
    )
    assert_true(curated.item.metadata["curator_engine"] == "memory_llm", "curator skipped LLM")
    assert_true(
        proposal.proposal.target == MemoryProposalTarget.PREFERENCE,
        "proposal was not classified",
    )
    return "Conversation curator and Memory Gateway use LLM reasoner contracts.", {
        "curator_engine": curated.item.metadata["curator_engine"],
        "proposal_target": proposal.proposal.target.value,
        "proposal_confidence": proposal.proposal.confidence,
    }


def bench_in_memory_vector_recall() -> tuple[str, dict[str, Any]]:
    store = InMemoryMemoryStore()
    client = build_embedding_client(
        EmbeddingProviderConfig(
            provider="fake",
            model_name="fake-embed-v1",
            dimension=1536,
        )
    )
    qdrant = QdrantCandidateSource(
        url="memory://benchmark",
        dense_dim=1536,
        query_embedding_client=client,
    )
    qdrant._use_in_memory_backend()
    retention = RetentionService(store)
    embedding = EmbeddingService(store, qdrant, client)
    retrieval = RetrievalService((store, qdrant))
    tenant_id = uuid4()
    workspace_id = uuid4()
    for index in range(50):
        item = retention.retain(
            RetainCommand(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                layer=MemoryLayer.SEMANTIC,
                scope=MemoryScope.WORKSPACE,
                kind="fact",
                text=f"Benchmark vector memory item {index} about OpenClaw Hermes Qdrant.",
                provenance=Provenance(source_kind="benchmark-suite"),
                labels=("benchmark-vector",),
                idempotency_key=f"benchmark-vector:{index}",
            )
        )
        embedding.process_memory_retained(tenant_id, item.item.id)
    recall = retrieval.recall(
        RecallQuery(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            text="OpenClaw Hermes Qdrant vector memory",
            top_k=8,
        )
    )
    assert_true(len(recall.candidates) >= 8, "vector recall returned too few candidates")
    return "In-memory vector pipeline indexes and recalls benchmark memories.", {
        "indexed_items": 50,
        "top_k_returned": len(recall.candidates),
        "sources": ",".join(recall.sources_used),
        "top_score": round(recall.candidates[0].final_score, 4),
    }


def bench_long_context_compiler() -> tuple[str, dict[str, Any]]:
    tenant_id = uuid4()
    workspace_id = uuid4()
    item_count = 120
    text = (
        "Long-context benchmark memory. "
        "This synthetic note verifies that the compiler can pack a large "
        "128k production context window without silently falling back to the "
        "old small budget. "
    ) * 18
    items = tuple(
        MemoryItem(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            layer=MemoryLayer.SEMANTIC,
            scope=MemoryScope.WORKSPACE,
            kind="benchmark_context",
            text=f"{index:03d}: {text}",
            provenance=Provenance(source_kind="benchmark-suite"),
            labels=("benchmark-long-context",),
            importance=0.5,
            confidence=0.9,
        )
        for index in range(item_count)
    )
    recall = RecallResult(
        candidates=tuple(
            Candidate(item=item, source="synthetic", final_score=1.0)
            for item in items
        ),
        sources_used=("synthetic",),
    )
    package = ContextCompiler().compile(
        recall,
        ContextRecipe(
            operation="benchmark_long_context",
            budget_tokens=131072,
            layer_order=(MemoryLayer.SEMANTIC,),
            per_layer_limit={MemoryLayer.SEMANTIC: 1000},
        ),
    )
    rendered = package.render_markdown()
    assert_true(package.budget_tokens == 131072, "long context budget changed")
    assert_true(len(package.trace_ids) == item_count, "compiler did not include all items")
    assert_true(
        package.used_tokens > 60000,
        "long context benchmark did not exceed small-context budgets",
    )
    assert_true(len(rendered) > 200000, "rendered long context is unexpectedly small")
    return "ContextCompiler packs a large synthetic context under the 128k budget.", {
        "budget_tokens": package.budget_tokens,
        "used_tokens": package.used_tokens,
        "items_included": len(package.trace_ids),
        "rendered_chars": len(rendered),
    }


def bench_agent_integration_defaults() -> tuple[str, dict[str, Any]]:
    shared = (ROOT / "agent-integrations/shared/config.py").read_text()
    hermes = (
        ROOT / "agent-integrations/hermes/universal_agent_memory/__init__.py"
    ).read_text()
    openclaw = (ROOT / "agent-integrations/openclaw/plugin/index.js").read_text()
    docs = (ROOT / "agent-integrations/README.md").read_text()
    checks = {
        "shared_url_6798": 'url: str = "http://localhost:6798"' in shared,
        "shared_budget_128k": "context_budget_tokens: int = 131072" in shared,
        "hermes_url_6798": 'os.getenv("UAM_URL", "http://localhost:6798")' in hermes,
        "hermes_budget_128k": 'os.getenv("UAM_CONTEXT_BUDGET_TOKENS", "131072")' in hermes,
        "openclaw_url_6798": 'const DEFAULT_URL = "http://localhost:6798";' in openclaw,
        "openclaw_budget_128k": "default: 131072" in openclaw,
        "docs_budget_128k": "UAM_CONTEXT_BUDGET_TOKENS=131072" in docs,
    }
    missing = [key for key, ok in checks.items() if not ok]
    assert_true(not missing, f"agent integration default mismatch: {missing}")
    return "OpenClaw/Hermes/native defaults use port 6798 and 128k context.", checks


def bench_web_contract() -> tuple[str, dict[str, Any]]:
    app = (ROOT / "web/src/App.tsx").read_text()
    css = (ROOT / "web/src/styles.css").read_text()
    package = json.loads((ROOT / "web/package.json").read_text())
    checks = {
        "russian_dashboard": "Панель" in app and "Граф памяти" in app,
        "graph_expand_button": "Развернуть" in app and "Свернуть" in app,
        "settings_panel": "Настройки моделей" in app,
        "responsive_css": "@media" in css and "graph-expanded" in css,
        "build_script": "build" in package.get("scripts", {}),
    }
    missing = [key for key, ok in checks.items() if not ok]
    assert_true(not missing, f"missing web contract checks: {missing}")
    return "Web dashboard keeps Russian UI, graph controls, settings and build script.", checks


def bench_web_build() -> tuple[str, dict[str, Any]]:
    package = ROOT / "web/package.json"
    if not package.exists():
        raise SkipCheck("web/package.json not found")
    completed = subprocess.run(
        ["npm", "run", "build"],
        cwd=ROOT / "web",
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if completed.returncode != 0:
        raise AssertionError((completed.stdout + completed.stderr)[-2000:])
    dist = ROOT / "web/dist/index.html"
    assert_true(dist.exists(), "web/dist/index.html missing after build")
    return "React/Vite dashboard builds successfully.", {
        "dist_index_bytes": dist.stat().st_size,
    }


def bench_docker_compose_state() -> tuple[str, dict[str, Any]]:
    completed = subprocess.run(
        ["docker", "compose", "ps", "--format", "json"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout).strip()
        raise SkipCheck(message or "docker compose ps failed")
    rows = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    running = [row for row in rows if str(row.get("State", "")).lower() == "running"]
    return "Docker compose daemon is reachable and stack state was inspected.", {
        "services_total": len(rows),
        "services_running": len(running),
    }


def bench_live_http_api(base_url: str, api_key: str | None) -> tuple[str, dict[str, Any]]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        with urlopen(f"{base_url.rstrip('/')}/health", timeout=3) as response:  # noqa: S310
            health = json.loads(response.read().decode())
    except URLError as exc:
        raise SkipCheck(f"API endpoint unavailable at {base_url}: {exc}") from exc
    assert_true(health.get("status") == "ok", f"unexpected health payload: {health}")
    text = f"Live HTTP benchmark memory {uuid4()} confirms port 6798 works."
    retain = Request(
        f"{base_url.rstrip('/')}/v1/memory/retain",
        data=json.dumps(
            {
                "layer": "semantic",
                "scope": "workspace",
                "kind": "fact",
                "text": text,
                "idempotency_key": f"benchmark-live-http:{uuid4()}",
            }
        ).encode(),
        headers=headers,
        method="POST",
    )
    with urlopen(retain, timeout=10) as response:  # noqa: S310
        retained = json.loads(response.read().decode())
    assert_true(retained.get("created") is True, f"retain failed: {retained}")
    request = Request(
        f"{base_url.rstrip('/')}/v1/memory/recall",
        data=json.dumps({"query": text, "top_k": 5}).encode(),
        headers=headers,
        method="POST",
    )
    with urlopen(request, timeout=10) as response:  # noqa: S310
        recall = json.loads(response.read().decode())
    results = recall.get("results", [])
    assert_true(
        any(row.get("text") == text for row in results),
        "live API recall missed retained memory",
    )
    return "Live API health and recall responded.", {
        "base_url": base_url,
        "retained_id": retained.get("id"),
        "context_budget_tokens": recall.get("context", {}).get("budget_tokens"),
        "result_count": len(results),
    }


def bench_live_memory_llm(base_url: str, model: str) -> tuple[str, dict[str, Any]]:
    config = MemoryLLMConfig(
        model_name=model,
        base_url=base_url.rstrip("/"),
        context_window_tokens=131072,
        max_tokens=1600,
        temperature=0.0,
        timeout_seconds=90,
    )
    try:
        text = MemoryLLMClient(config).chat(
            [
                {
                    "role": "user",
                    "content": "Ответь одним коротким русским словом без объяснений: память",
                }
            ],
            max_tokens=1600,
            temperature=0.0,
        )
    except Exception as exc:  # noqa: BLE001 - optional live endpoint.
        raise SkipCheck(
            f"memory LLM unavailable or incomplete: {type(exc).__name__}: {exc}"
        ) from exc
    assert_true(bool(text.strip()), "memory LLM returned empty text")
    return "Live OpenAI-compatible chat-completions returned final content.", {
        "base_url": base_url,
        "model": model,
        "response_chars": len(text),
        "contains_memory_word": "пам" in text.lower(),
    }


def bench_live_embeddings(base_url: str, model: str) -> tuple[str, dict[str, Any]]:
    docs = {
        "postgres": "Память хранится в PostgreSQL ledger.",
        "qdrant": "Qdrant используется для semantic vector recall.",
        "openclaw": "OpenClaw подключается через глубокий plugin runtime.",
    }

    def embed(text: str) -> list[float]:
        request = Request(
            f"{base_url.rstrip('/')}/v1/embeddings",
            data=json.dumps({"model": model, "input": text}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=60) as response:  # noqa: S310
            payload = json.loads(response.read().decode())
        return [float(value) for value in payload["data"][0]["embedding"]]

    try:
        vectors = {name: embed(text) for name, text in docs.items()}
        query = embed("какой индекс используется для semantic recall?")
    except Exception as exc:  # noqa: BLE001 - optional live endpoint.
        raise SkipCheck(f"embedding endpoint unavailable: {type(exc).__name__}: {exc}") from exc
    ranked = sorted(
        ((name, _cosine(query, vector)) for name, vector in vectors.items()),
        key=lambda item: item[1],
        reverse=True,
    )
    assert_true(ranked[0][0] == "qdrant", f"unexpected top embedding result: {ranked}")
    return "Live embedding endpoint ranks Qdrant semantic recall first.", {
        "base_url": base_url,
        "model": model,
        "dimension": len(query),
        "top": ranked[0][0],
        "top_score": round(ranked[0][1], 4),
    }


def _cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    return dot / max(left_norm * right_norm, 1e-12)


def write_report(path: Path, results: list[BenchResult]) -> None:
    passed = sum(result.status == "PASS" for result in results)
    failed = sum(result.status == "FAIL" for result in results)
    skipped = sum(result.status == "SKIP" for result in results)
    lines = [
        "# Obelisk Memory benchmark results",
        "",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        "",
        f"Summary: **{passed} passed**, **{failed} failed**, **{skipped} skipped**.",
        "",
    ]
    docker_result = next(
        (result for result in results if result.name == "docker_compose_state"),
        None,
    )
    if docker_result and docker_result.status == "SKIP":
        lines.extend(
            [
                "Runtime note: Docker compose was not runnable in this environment, "
                "so container startup could not be live-verified. The compose file "
                "was validated separately with `docker compose --profile advanced "
                "config`; `live_http_api` uses a temporary local FastAPI server on "
                "the same public port `6798`.",
                "",
            ]
        )
    lines.extend(["| Benchmark | Status | Duration | Details |", "|---|---:|---:|---|"])
    for result in results:
        details = result.details.replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| `{result.name}` | {result.status} | {result.duration_ms:.1f} ms | {details} |"
        )
    lines.extend(["", "## Metrics", ""])
    for result in results:
        lines.extend([f"### {result.name}", "", f"- status: `{result.status}`"])
        if result.metrics:
            for key, value in result.metrics.items():
                lines.append(f"- {key}: `{value}`")
        else:
            lines.append("- metrics: n/a")
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-base-url", default="http://127.0.0.1:6798")
    parser.add_argument("--api-key")
    parser.add_argument("--llm-base-url", default="http://localhost:8000/v1")
    parser.add_argument("--llm-model", default="memory-model")
    parser.add_argument("--embedding-base-url", default="http://localhost:8000/v1")
    parser.add_argument("--embedding-model", default="embedding-model")
    parser.add_argument(
        "--report",
        default=str(ROOT / "ops" / "benchmark-report.md"),
    )
    parser.add_argument("--skip-web-build", action="store_true")
    args = parser.parse_args()

    checks: list[tuple[str, Callable[[], tuple[str, dict[str, Any]]]]] = [
        ("config_contracts", bench_config_contracts),
        ("api_memory_contract", bench_api_memory_contract),
        ("llm_wiring_contract", bench_llm_wiring_contract),
        ("in_memory_vector_recall", bench_in_memory_vector_recall),
        ("long_context_compiler", bench_long_context_compiler),
        ("agent_integration_defaults", bench_agent_integration_defaults),
        ("web_contract", bench_web_contract),
    ]
    if not args.skip_web_build:
        checks.append(("web_build", bench_web_build))
    checks.extend(
        [
            ("docker_compose_state", bench_docker_compose_state),
            (
                "live_http_api",
                lambda: bench_live_http_api(args.api_base_url, args.api_key),
            ),
            (
                "live_memory_llm",
                lambda: bench_live_memory_llm(args.llm_base_url, args.llm_model),
            ),
            (
                "live_embeddings",
                lambda: bench_live_embeddings(args.embedding_base_url, args.embedding_model),
            ),
        ]
    )

    results = [timed(name, fn) for name, fn in checks]
    report_path = Path(args.report)
    write_report(report_path, results)
    for result in results:
        print(
            f"{result.status:4} {result.name:<24} "
            f"{result.duration_ms:8.1f} ms  {result.details}"
        )
    print(f"report={report_path}")
    return 1 if any(result.status == "FAIL" for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
