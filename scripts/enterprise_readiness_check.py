"""Static enterprise-readiness gate for Obelisk Memory.

The benchmark suite validates runtime behavior. This script validates the
production envelope: docs, compose hardening, CI, generated assets, and the
latest benchmark report. It intentionally has no third-party dependencies.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "docs" / "ENTERPRISE_READINESS_REPORT_2026_07_09.md"


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def exists(path: str) -> bool:
    return (ROOT / path).exists()


def check_file(path: str) -> Check:
    return Check(f"file:{path}", exists(path), "required production artifact")


def run_checks(*, static_only: bool) -> list[Check]:
    checks: list[Check] = []
    required_files = [
        "README.md",
        "SECURITY.md",
        ".env.production.example",
        "docker-compose.prod.yml",
        ".github/workflows/ci.yml",
        "docs/assets/obelisk-memory-hero.png",
        "docs/OPERATIONS_RUNBOOK.md",
        "docs/ENTERPRISE_READINESS.md",
        "docs/RELEASE_CHECKLIST.md",
        "docs/DGX_SPARK_MEMORY_LLM.md",
        "docs/BENCHMARK_RESULTS_2026_07_09.md",
    ]
    checks.extend(check_file(path) for path in required_files)

    readme = read("README.md")
    checks.extend(
        [
            Check("readme:brand", "Obelisk Memory" in readme, "README uses product name"),
            Check(
                "readme:hero",
                "docs/assets/obelisk-memory-hero.png" in readme,
                "README references generated hero asset",
            ),
            Check("readme:production", "Production deployment" in readme, "prod path documented"),
            Check(
                "readme:agents",
                "OpenClaw" in readme and "Hermes" in readme,
                "agent adapters documented",
            ),
            Check("readme:128k", "131072" in readme, "128k context budget documented"),
            Check("readme:dgx", "192.168.0.10" in readme, "DGX Spark .10 endpoint documented"),
        ]
    )

    prod_compose = read("docker-compose.prod.yml")
    checks.extend(
        [
            Check(
                "prod-compose:only-api-published",
                'ports:\n      - "6798:8080"' in prod_compose and "6548:5432" not in prod_compose,
                "production compose publishes API/UI but not PostgreSQL",
            ),
            Check(
                "prod-compose:internal-qdrant",
                "6799:6333" not in prod_compose
                and "UAM_QDRANT_URL: http://qdrant:6333" in prod_compose,
                "Qdrant is internal in production",
            ),
            Check(
                "prod-compose:nats-health",
                "healthz" in prod_compose
                and 'command: ["-js", "-sd", "/data", "-m", "8222"]' in prod_compose,
                "NATS JetStream has monitoring healthcheck",
            ),
            Check(
                "prod-compose:required-api-key",
                "${UAM_API_KEY:?set UAM_API_KEY" in prod_compose,
                "production API key is required",
            ),
        ]
    )

    ci = read(".github/workflows/ci.yml")
    checks.extend(
        [
            Check("ci:ruff", "ruff check" in ci, "CI runs ruff"),
            Check("ci:pytest", "pytest -q" in ci, "CI runs pytest"),
            Check("ci:web-build", "npm run build" in ci, "CI builds web UI"),
            Check(
                "ci:prod-compose",
                "docker-compose.prod.yml" in ci,
                "CI validates production compose",
            ),
        ]
    )

    env = read(".env.production.example")
    checks.extend(
        [
            Check(
                "env:memory-llm",
                "UAM_MEMORY_LLM_BASE_URL=http://192.168.0.10:8000/v1" in env,
                "Qwen memory LLM endpoint",
            ),
            Check(
                "env:embeddings",
                "UAM_EMBEDDING_BASE_URL=http://192.168.0.10:8002" in env,
                "embedding endpoint",
            ),
            Check("env:privacy", "UAM_PRIVACY_ACTION=redact" in env, "privacy defaults"),
            Check("env:scoped-keys", "UAM_API_KEYS=" in env, "scoped API keys documented"),
        ]
    )

    if not static_only:
        benchmark = read("docs/BENCHMARK_RESULTS_2026_07_09.md")
        checks.extend(
            [
                Check(
                    "benchmark:passed",
                    "Passed: 12" in benchmark or "12 passed" in benchmark.lower(),
                    "latest benchmark pass count",
                ),
                Check(
                    "benchmark:no-failures",
                    "Failed: 0" in benchmark or "0 failed" in benchmark.lower(),
                    "latest benchmark failure count",
                ),
            ]
        )

    return checks


def render_report(checks: list[Check]) -> str:
    passed = sum(1 for check in checks if check.passed)
    failed = len(checks) - passed
    lines = [
        "# Enterprise readiness report — 2026-07-09",
        "",
        f"Passed: {passed}",
        f"Failed: {failed}",
        "",
        "| Check | Status | Detail |",
        "|---|---:|---|",
    ]
    for check in checks:
        status = "PASS" if check.passed else "FAIL"
        lines.append(f"| `{check.name}` | {status} | {check.detail} |")
    lines.extend(
        [
            "",
            "## Verdict",
            "",
            (
                "Obelisk Memory passes the repository-level enterprise readiness gate."
                if failed == 0
                else "Obelisk Memory is not enterprise-ready until failed checks are fixed."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--static-only", action="store_true")
    args = parser.parse_args()

    checks = run_checks(static_only=args.static_only)
    report = render_report(checks)
    if not args.static_only:
        REPORT_PATH.write_text(report, encoding="utf-8")
    print(report)
    return 0 if all(check.passed for check in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
