"""Static production-envelope gate for Obelisk Memory.

The benchmark suite validates runtime behavior. This script validates the
production envelope: docs, compose hardening, CI, generated assets, and the
latest benchmark report. It intentionally has no third-party dependencies.

Passing this gate means "ready for a trusted self-hosted pilot", not "fully
production-complete". The full gap list lives in the production gap audit.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "docs" / "ENTERPRISE_READINESS_REPORT_2026_07_10.md"


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
        "deploy/reverse-proxy/Caddyfile",
        "deploy/reverse-proxy/docker-compose.caddy.yml",
        ".github/workflows/ci.yml",
        "migrations/008_audit_events.sql",
        "migrations/009_api_key_registry.sql",
        "scripts/check_branch_protection.py",
        "scripts/check_metrics_health.py",
        "scripts/validate_production_env.py",
        "scripts/export_audit.py",
        "scripts/agent_soak_eval.py",
        "scripts/real_memory_llm_eval.py",
        "scripts/vault_manifest.py",
        "scripts/restore_drill.py",
        "scripts/scheduled_backup.py",
        "docs/assets/obelisk-memory-hero.png",
        "docs/GITHUB_BRANCH_PROTECTION.md",
        "docs/OPERATIONS_RUNBOOK.md",
        "docs/TLS_REVERSE_PROXY.md",
        "docs/ENTERPRISE_READINESS.md",
        "docs/PRODUCTION_GAP_AUDIT_2026_07_10.md",
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
                "readme:honest-status",
                "trusted local/team pilot" in readme
                and "Full production still requires" in readme,
                "README does not over-claim full production readiness",
            ),
            Check(
                "readme:gap-audit",
                "PRODUCTION_GAP_AUDIT_2026_07_10.md" in readme,
                "README links the honest production gap audit",
            ),
            Check(
                "readme:agents",
                "OpenClaw" in readme and "Hermes" in readme,
                "agent adapters documented",
            ),
            Check(
                "readme:agent-soak",
                "scripts/agent_soak_eval.py" in readme
                and "cross-workspace leakage" in readme,
                "README documents live agent soak evidence",
            ),
            Check(
                "readme:env-validation",
                "scripts/validate_production_env.py" in readme
                and "--require-public-tls" in readme,
                "README documents strict production env validation",
            ),
            Check(
                "readme:memory-llm-eval",
                "scripts/real_memory_llm_eval.py" in readme
                and "ops/memory-llm.json" in readme,
                "README documents live memory LLM regression evidence",
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
            Check(
                "reverse-proxy:caddy-overlay",
                "caddy:2.8-alpine" in read("deploy/reverse-proxy/docker-compose.caddy.yml")
                and '"443:443"' in read("deploy/reverse-proxy/docker-compose.caddy.yml")
                and "ports: !override" in read(
                    "deploy/reverse-proxy/docker-compose.caddy.yml"
                )
                and "127.0.0.1:6798:8080" in read(
                    "deploy/reverse-proxy/docker-compose.caddy.yml"
                )
                and "reverse_proxy memory-server:8080" in read(
                    "deploy/reverse-proxy/Caddyfile"
                )
                and "Strict-Transport-Security" in read(
                    "deploy/reverse-proxy/Caddyfile"
                ),
                "Caddy TLS reverse proxy example exists",
            ),
            Check(
                "docs:tls-reverse-proxy",
                "UAM_PUBLIC_HOST" in read("docs/TLS_REVERSE_PROXY.md")
                and "Do not call the deployment production-hardened" in read(
                    "docs/TLS_REVERSE_PROXY.md"
                )
                and "6798" in read("docs/TLS_REVERSE_PROXY.md"),
                "TLS reverse proxy guide documents backend exposure limits",
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
                "ci:production-readiness-eval",
                "scripts/production_readiness_eval.py" in ci,
                "CI runs in-process production readiness eval",
            ),
            Check(
                "ci:prod-compose",
                "docker-compose.prod.yml" in ci,
                "CI validates production compose",
            ),
            Check(
                "ci:reverse-proxy-compose",
                "deploy/reverse-proxy/docker-compose.caddy.yml" in ci,
                "CI validates reverse proxy compose overlay",
            ),
            Check(
                "ci:env-placeholder-guard",
                "validate_production_env.py .env.production.example" in ci
                and "unexpectedly passed strict production validation" in ci,
                "CI confirms placeholder env cannot pass strict production validation",
            ),
            Check(
                "release:branch-protection-verifier",
                "required_pull_request_reviews" in read("scripts/check_branch_protection.py")
                and "enforce_admins" in read("scripts/check_branch_protection.py")
                and "required_status_checks" in read("scripts/check_branch_protection.py"),
                "branch-protection verifier checks PR, status, and admin enforcement",
            ),
            Check(
                "tests:branch-protection-verifier",
                "test_check_branch_protection_accepts_pr_checks_and_admin_enforcement"
                in read("tests/test_backup_restore_scripts.py")
                and "test_check_branch_protection_rejects_missing_required_status_check"
                in read("tests/test_backup_restore_scripts.py"),
                "branch-protection verifier behavior is covered by tests",
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
            Check(
                "env:signing-keys",
                "UAM_AUDIT_SIGNING_KEY=" in env and "UAM_VAULT_SIGNING_KEY=" in env,
                "operator-held signing keys are documented",
            ),
            Check(
                "env:public-host",
                "UAM_PUBLIC_HOST=" in env and "UAM_PUBLIC_EMAIL=" in env,
                "public TLS endpoint env is documented",
            ),
        ]
    )

    api = read("src/memory_plane/api/app.py")
    tests = read("tests/test_api.py")
    audit_migration = read("migrations/008_audit_events.sql")
    key_migration = read("migrations/009_api_key_registry.sql")
    checks.extend(
        [
            Check(
                "api:security-headers",
                "Content-Security-Policy" in api
                and "X-Frame-Options" in api
                and "X-Content-Type-Options" in api,
                "API applies baseline security headers",
            ),
            Check(
                "tests:security-headers",
                "test_api_responses_include_security_headers" in tests,
                "security headers are covered by API tests",
            ),
            Check(
                "ops:metrics-health-evaluator",
                "outbox_dead_letter_total" in read("scripts/check_metrics_health.py")
                and "outbox_lag_seconds" in read("scripts/check_metrics_health.py")
                and "embedding_failures_total" in read("src/memory_plane/services/embedding.py")
                and "embedding_last_duration_seconds" in read(
                    "src/memory_plane/services/embedding.py"
                )
                and "UAM_METRICS_ALERT_WEBHOOK" in read("scripts/check_metrics_health.py"),
                (
                    "metrics health script evaluates outbox lag/dead letters; "
                    "embedding exposes failure/latency metrics"
                ),
            ),
            Check(
                "tests:metrics-health-evaluator",
                "test_metrics_health_fails_on_dead_letters_lag_and_missing_metric"
                in read("tests/test_metrics.py")
                and "test_metrics_health_cli_writes_report_and_alerts_on_failure"
                in read("tests/test_metrics.py"),
                "metrics health thresholds and report behavior are covered",
            ),
            Check(
                "audit:rls",
                "create table audit_events" in audit_migration
                and "enable row level security" in audit_migration
                and "force row level security" in audit_migration,
                "audit events are durable and tenant-isolated",
            ),
            Check(
                "audit:operator-export",
                '@app.get("/v1/audit/events")' in api
                and 'path.startswith("/v1/audit")' in api,
                "audit export endpoint is operator-scoped",
            ),
            Check(
                "tests:audit-trail",
                "test_audit_trail_records_operator_memory_and_vault_actions" in tests
                and "test_audit_events_require_operator_scope" in tests,
                "audit trail behavior is covered by API tests",
            ),
            Check(
                "audit:tamper-evident-bundle",
                "audit-events.jsonl" in read("scripts/export_audit.py")
                and "manifest.sha256" in read("scripts/export_audit.py")
                and "manifest.sig" in read("scripts/export_audit.py")
                and "hmac-sha256" in read("scripts/export_audit.py"),
                "audit script exports JSONL plus checksum and optional signature",
            ),
            Check(
                "audit:range-export",
                "--all-pages" in read("scripts/export_audit.py")
                and "--since" in read("scripts/export_audit.py")
                and "before_event_id" in read("scripts/export_audit.py"),
                "audit export supports time-window pagination",
            ),
            Check(
                "tests:audit-export-bundle",
                "test_export_audit_writes_jsonl_manifest_and_checksum" in read(
                    "tests/test_backup_restore_scripts.py"
                )
                and "test_export_audit_signs_and_verifies_bundle" in read(
                    "tests/test_backup_restore_scripts.py"
                )
                and "test_export_audit_can_export_all_pages_with_time_range" in read(
                    "tests/test_backup_restore_scripts.py"
                ),
                "audit bundle checksum/signature/range behavior is covered by tests",
            ),
            Check(
                "keys:registry-rls",
                "create table api_key_registry" in key_migration
                and "secret_fingerprint" in key_migration
                and "force row level security" in key_migration,
                "API key registry stores non-secret metadata under RLS",
            ),
            Check(
                "keys:operator-api",
                '@app.get("/v1/keys")' in api
                and '@app.post("/v1/keys/{key_id}/revoke")' in api
                and 'path.startswith("/v1/keys")' in api,
                "API key registry is operator-scoped",
            ),
            Check(
                "tests:key-registry",
                "test_api_key_registry_tracks_last_used_and_revocation" in tests,
                "key registry last-used and revocation behavior is covered",
            ),
            Check(
                "restore-drill:script",
                "docker" in read("scripts/restore_drill.py")
                and "pg_restore" in read("scripts/restore_drill.py")
                and "REQUIRED_TABLES" in read("scripts/restore_drill.py"),
                "restore drill verifies backups in isolated PostgreSQL",
            ),
            Check(
                "tests:restore-drill",
                "test_restore_drill_uses_temporary_docker_target" in read(
                    "tests/test_backup_restore_scripts.py"
                ),
                "restore drill command flow is covered by tests",
            ),
            Check(
                "backup:schedule-runner",
                '"backup.py"' in read("scripts/scheduled_backup.py")
                and '"restore_drill.py"' in read("scripts/scheduled_backup.py")
                and '"export_audit.py"' in read("scripts/scheduled_backup.py")
                and "UAM_BACKUP_ALERT_WEBHOOK" in read("scripts/scheduled_backup.py"),
                "scheduled backup runner performs backup, restore drill and alert hook",
            ),
            Check(
                "tests:scheduled-backup",
                "test_scheduled_backup_runs_backup_drill_audit_and_writes_report"
                in read("tests/test_backup_restore_scripts.py")
                and "test_scheduled_backup_alerts_on_failure"
                in read("tests/test_backup_restore_scripts.py"),
                "scheduled backup success/failure reporting is covered",
            ),
            Check(
                "agents:soak-runner",
                "OpenClaw/Hermes soak checks" in read("scripts/agent_soak_eval.py")
                and "cross-workspace-leakage" in read("scripts/agent_soak_eval.py")
                and "obelisk-agent-soak-v1" in read("scripts/agent_soak_eval.py"),
                "live agent soak runner validates retain/recall/leakage",
            ),
            Check(
                "tests:agent-soak-runner",
                "test_agent_soak_eval_passes_parallel_agent_lifecycle"
                in read("tests/test_agent_soak_eval.py")
                and "test_agent_soak_eval_fails_on_cross_workspace_leakage"
                in read("tests/test_agent_soak_eval.py"),
                "agent soak runner success and leakage failure are covered",
            ),
            Check(
                "vault:signed-manifest",
                "MANIFEST_FORMAT = \"obelisk-vault-manifest-v1\"" in read(
                    "scripts/vault_manifest.py"
                )
                and "SIGNATURE_ALGORITHM = \"hmac-sha256\"" in read(
                    "scripts/vault_manifest.py"
                )
                and "--require-signature" in read("scripts/import_vault.py")
                and "UAM_VAULT_SIGNING_KEY" in read("scripts/export_vault.py"),
                "vault export/import supports manifest checksum and HMAC signatures",
            ),
            Check(
                "tests:vault-signed-manifest",
                "test_export_vault_can_sign_manifest" in read(
                    "tests/test_backup_restore_scripts.py"
                )
                and "test_import_vault_verifies_signed_manifest_before_apply" in read(
                    "tests/test_backup_restore_scripts.py"
                )
                and "test_import_vault_rejects_tampered_signed_manifest" in read(
                    "tests/test_backup_restore_scripts.py"
                ),
                "signed vault manifest behavior is covered by tests",
            ),
            Check(
                "env:validator",
                "PLACEHOLDER_PATTERNS" in read("scripts/validate_production_env.py")
                and "--require-public-tls" in read("scripts/validate_production_env.py")
                and "--require-real-embeddings" in read("scripts/validate_production_env.py")
                and "UAM_QDRANT_PAYLOAD_TEXT" in read("scripts/validate_production_env.py")
                and "UAM_MEMORY_TEXT_ENCRYPTION" in read(
                    "scripts/validate_production_env.py"
                ),
                "production env validator rejects placeholder/local-only config",
            ),
            Check(
                "tests:env-validator",
                "test_validate_production_env_accepts_strict_real_config" in read(
                    "tests/test_backup_restore_scripts.py"
                )
                and "test_validate_production_env_rejects_placeholders" in read(
                    "tests/test_backup_restore_scripts.py"
                ),
                "production env validator behavior is covered",
            ),
            Check(
                "qdrant:redacted-payload",
                "payload_text" in read("src/memory_plane/adapters/qdrant.py")
                and "text_redacted" in read("src/memory_plane/adapters/qdrant.py")
                and "_payload_to_candidate_item" in read(
                    "src/memory_plane/adapters/qdrant.py"
                ),
                "Qdrant can store vectors/filter metadata without raw memory text",
            ),
            Check(
                "tests:qdrant-redacted-payload",
                "test_upsert_qdrant_can_redact_text_payload" in read("tests/test_qdrant.py")
                and "test_live_qdrant_search_hydrates_redacted_payload_from_ledger"
                in read("tests/test_qdrant.py"),
                "Qdrant payload redaction and ledger hydration are covered",
            ),
            Check(
                "postgres:pgcrypto-text",
                "enc:pgcrypto:v1:" in read("src/memory_plane/adapters/postgres.py")
                and "pgp_sym_encrypt" in read("src/memory_plane/adapters/postgres.py")
                and "pgp_sym_decrypt" in read("src/memory_plane/adapters/postgres.py"),
                "PostgreSQL canonical memory text can be encrypted with pgcrypto",
            ),
            Check(
                "tests:postgres-pgcrypto-text",
                "test_postgres_pgcrypto_mode_requires_key" in read(
                    "tests/test_postgres_encryption.py"
                )
                and "test_postgres_encrypts_memory_text_before_insert" in read(
                    "tests/test_postgres_encryption.py"
                ),
                "PostgreSQL memory text encryption behavior is covered",
            ),
            Check(
                "llm:live-regression-runner",
                "obelisk-memory-llm-eval-v1" in read("scripts/real_memory_llm_eval.py")
                and "json-memory-curation" in read("scripts/real_memory_llm_eval.py")
                and "fake" in read("scripts/real_memory_llm_eval.py"),
                "live Qwen/Spark memory LLM runner validates chat and curation",
            ),
            Check(
                "tests:llm-live-regression-runner",
                "test_real_memory_llm_eval_passes_memory_contract" in read(
                    "tests/test_real_memory_llm_eval.py"
                )
                and "test_real_memory_llm_eval_fails_when_model_keeps_obsolete_claim"
                in read("tests/test_real_memory_llm_eval.py"),
                "memory LLM live regression runner behavior is covered",
            ),
        ]
    )

    gap_audit = read("docs/PRODUCTION_GAP_AUDIT_2026_07_10.md")
    checks.extend(
        [
            Check(
                "gap-audit:no-overclaim",
                "Things that must not be claimed yet" in gap_audit,
                "gap audit explicitly forbids readiness over-claims",
            ),
            Check(
                "gap-audit:full-production-gates",
                "Security gate" in gap_audit
                and "Reliability gate" in gap_audit
                and "Agent-integration gate" in gap_audit,
                "gap audit defines full-production gates",
            ),
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
        "# Production envelope report — 2026-07-10",
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
                "Obelisk Memory passes the repository-level trusted self-hosted pilot gate. "
                "This is not a full-production certification; see the production gap audit."
                if failed == 0
                else (
                    "Obelisk Memory is not ready for the trusted self-hosted "
                    "pilot gate until failed checks are fixed."
                )
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
