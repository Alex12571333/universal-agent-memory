"""Static production-envelope gate for Obelisk Memory.

The benchmark suite validates runtime behavior. This script validates the
production envelope: docs, compose hardening, CI, generated assets, and the
latest benchmark report. It intentionally has no third-party dependencies.

Passing this gate proves repository envelope artifacts are present. It does not
certify runtime correctness, a trusted pilot, or a production deployment. The
full blocker list lives in the production gap audit.
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
        ".github/workflows/ci.yml",
        "src/memory_plane/config/secrets.py",
        "migrations/008_audit_events.sql",
        "migrations/009_api_key_registry.sql",
        "scripts/check_branch_protection.py",
        "scripts/check_metrics_health.py",
        "scripts/deployment_preflight.py",
        "scripts/observability_preflight.py",
        "scripts/ops_schedule_preflight.py",
        "scripts/secret_files_preflight.py",
        "scripts/validate_production_env.py",
        "scripts/export_audit.py",
        "scripts/agent_soak_eval.py",
        "scripts/conversation_pipeline_eval.py",
        "scripts/load_smoke_eval.py",
        "scripts/ui_walkthrough_eval.py",
        "scripts/real_embedding_eval.py",
        "scripts/real_memory_llm_eval.py",
        "scripts/generate_release_evidence_manifest.py",
        "scripts/generate_release_notes.py",
        "scripts/vault_manifest.py",
        "scripts/restore_drill.py",
        "scripts/scheduled_backup.py",
        "scripts/audit_retention.py",
        "scripts/verify_release_evidence.py",
        "scripts/migrate_vector_collection.py",
        "scripts/purge_expired_conversations.py",
        "docs/assets/obelisk-memory-hero.png",
        "docs/GITHUB_BRANCH_PROTECTION.md",
        "docs/OPERATIONS_RUNBOOK.md",
        "docs/OBSERVABILITY.md",
        "docs/ENTERPRISE_READINESS.md",
        "docs/PRODUCTION_GAP_AUDIT_2026_07_10.md",
        "docs/RELEASE_CHECKLIST.md",
        "docs/RELEASE_EVIDENCE.md",
        "docs/VECTOR_COLLECTION_MIGRATION.md",
        "deploy/observability/grafana-dashboard.json",
        "deploy/observability/prometheus-alerts.yml",
    ]
    checks.extend(check_file(path) for path in required_files)

    readme = read("README.md")
    release_checklist = read("docs/RELEASE_CHECKLIST.md")
    release_evidence = read("docs/RELEASE_EVIDENCE.md")
    checks.extend(
        [
            Check("readme:brand", "Obelisk Memory" in readme, "README uses product name"),
            Check(
                "readme:hero",
                "docs/assets/obelisk-memory-hero.png" in readme,
                "README references product hero asset",
            ),
            Check(
                "readme:production-reference",
                "Production reference deployment" in readme
                and "signed target evidence" in readme,
                "README ties deployment approval to signed target evidence",
            ),
            Check(
                "readme:honest-status",
                "Repository checks alone" in readme
                and "deployment certification" in readme
                and "release checklist" in readme,
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
                "scripts/agent_soak_eval.py" in readme and "cross-workspace leakage" in readme,
                "README documents live agent soak evidence",
            ),
            Check(
                "readme:env-validation",
                "scripts/validate_production_env.py" in readme
                and "--require-real-embeddings" in readme,
                "README documents strict local-appliance env validation",
            ),
            Check(
                "readme:release-memory-llm-eval",
                "docs/RELEASE_CHECKLIST.md" in readme
                and "docs/RELEASE_EVIDENCE.md" in readme
                and "scripts/real_memory_llm_eval.py" in release_checklist
                and "ops/memory-llm.json" in release_evidence,
                "README delegates live memory LLM evidence to release documentation",
            ),
            Check(
                "readme:release-ui-walkthrough",
                "docs/RELEASE_CHECKLIST.md" in readme
                and "docs/RELEASE_EVIDENCE.md" in readme
                and "scripts/ui_walkthrough_eval.py" in release_checklist
                and "ops/ui-walkthrough.json" in release_evidence,
                "README delegates live UI walkthrough evidence to release documentation",
            ),
            Check("readme:128k", "131072" in readme, "128k context budget documented"),
            Check(
                "readme:openai-compatible-llm",
                "OpenAI-compatible means the wire protocol, not the company" in readme
                and "`/v1/chat/completions`" in readme
                and "any provider" in readme
                and "UAM_MEMORY_LLM_PROVIDER=openai-compatible" in readme
                and "provider/model-id" in readme
                and "LiteLLM" in readme
                and "llama.cpp" in readme,
                "README documents provider-neutral memory LLM endpoint",
            ),
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
                "prod-compose:secret-files",
                "UAM_API_KEY_FILE: ${UAM_API_KEY_FILE:-}" in prod_compose
                and "UAM_API_KEYS_FILE: ${UAM_API_KEYS_FILE:-}" in prod_compose
                and "UAM_UI_SESSION_SIGNING_KEY_FILE: /run/secrets/ui_session_signing_key"
                in prod_compose
                and (
                    "UAM_API_PRINCIPAL_BINDINGS_JSON_FILE: "
                    "${UAM_API_PRINCIPAL_BINDINGS_JSON_FILE:-}"
                )
                in prod_compose
                and "UAM_REQUIRE_IDENTITY_BINDINGS: ${UAM_REQUIRE_IDENTITY_BINDINGS:-true}"
                in prod_compose
                and "UAM_MEMORY_LLM_API_KEY_FILE: ${UAM_MEMORY_LLM_API_KEY_FILE:-}" in prod_compose
                and "UAM_EMBEDDING_API_KEY_FILE: ${UAM_EMBEDDING_API_KEY_FILE:-}" in prod_compose
                and "UAM_DATABASE_PASSWORD_FILE: /run/secrets/app_db_password" in prod_compose
                and "POSTGRES_PASSWORD_FILE: /run/secrets/postgres_password" in prod_compose
                and "file: ${POSTGRES_PASSWORD_FILE:?" in prod_compose
                and "file: ${UAM_APP_DB_PASSWORD_FILE:?" in prod_compose
                and "file: ${UAM_UI_SESSION_SIGNING_KEY_FILE:?" in prod_compose,
                "production compose includes dedicated database secret mounts and *_FILE paths",
            ),
            Check(
                "prod-compose:provider-neutral-embeddings",
                prod_compose.count(
                    "UAM_EMBEDDING_PROVIDER: ${UAM_EMBEDDING_PROVIDER:-openai-compatible}"
                )
                >= 2
                and prod_compose.count("UAM_EMBEDDING_SEND_DIMENSIONS: ") >= 2,
                "production API and worker use provider-neutral embedding defaults",
            ),
            Check(
                "prod-compose:text-encryption",
                prod_compose.count(
                    "UAM_MEMORY_TEXT_ENCRYPTION: ${UAM_MEMORY_TEXT_ENCRYPTION:-pgcrypto}"
                )
                >= 2
                and prod_compose.count("UAM_MEMORY_TEXT_ENCRYPTION_KEY_FILE: ") >= 2
                and prod_compose.count("UAM_MEMORY_TEXT_ENCRYPTION_SCOPES: ") >= 2,
                "production API and embedding worker receive canonical text encryption settings",
            ),
            Check(
                "prod-compose:qdrant-redacted-payload",
                prod_compose.count("UAM_QDRANT_PAYLOAD_TEXT: ${UAM_QDRANT_PAYLOAD_TEXT:-false}")
                >= 2,
                "production API and embedding worker keep raw text out of Qdrant payloads",
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
                "UAM_MEMORY_LLM_PROVIDER=openai-compatible" in env
                and "UAM_MEMORY_LLM_MODEL=provider/memory-model-id" in env
                and "UAM_MEMORY_LLM_BASE_URL=https://model-gateway.example.com/v1" in env
                and "OpenRouter" in env
                and "LiteLLM" in env
                and "another compatible gateway" in env,
                "OpenAI-compatible memory LLM endpoint",
            ),
            Check(
                "env:embeddings",
                "UAM_EMBEDDING_PROVIDER=openai-compatible" in env
                and "UAM_EMBEDDING_MODEL=provider/embedding-model-id" in env
                and "UAM_EMBEDDING_DIM=" in env
                and "UAM_EMBEDDING_BASE_URL=https://embedding-gateway.example.com/v1" in env
                and "UAM_EMBEDDING_SEND_DIMENSIONS=false" in env
                and "not a provider lock-in" in env,
                "OpenAI-compatible embedding endpoint",
            ),
            Check(
                "env:qdrant-collection",
                "UAM_QDRANT_COLLECTION=memory_items" in env,
                "stable Qdrant collection identity is documented",
            ),
            Check("env:privacy", "UAM_PRIVACY_ACTION=redact" in env, "privacy defaults"),
            Check("env:scoped-keys", "UAM_API_KEYS=" in env, "scoped API keys documented"),
            Check(
                "env:secret-files",
                "UAM_API_KEY_FILE=" in env
                and "UAM_API_KEYS_FILE=" in env
                and "UAM_API_PRINCIPAL_BINDINGS_JSON_FILE=" in env
                and "UAM_UI_SESSION_SIGNING_KEY_FILE=" in env
                and "UAM_MEMORY_TEXT_ENCRYPTION_KEY_FILE=" in env
                and "UAM_MEMORY_LLM_API_KEY_FILE=" in env
                and "UAM_EMBEDDING_API_KEY_FILE=" in env
                and "UAM_RELEASE_SIGNING_KEY_FILE=" in env,
                "mounted secret-file env alternatives are documented",
            ),
            Check(
                "env:ui-session",
                "UAM_UI_COOKIE_SECURE=true" in env
                and "UAM_UI_SESSION_TTL_SECONDS=28800" in env,
                "secure browser-session policy is documented",
            ),
            Check(
                "env:model-endpoint-allowlist",
                "UAM_MODEL_ENDPOINT_ALLOWLIST=" in env,
                "exact-origin model endpoint allowlist is documented",
            ),
            Check(
                "env:identity-bindings",
                "UAM_API_PRINCIPAL_BINDINGS_JSON=" in env
                and "UAM_REQUIRE_IDENTITY_BINDINGS=true" in env,
                "strict agent principal bindings are documented",
            ),
            Check(
                "env:text-encryption-scopes",
                "UAM_MEMORY_TEXT_ENCRYPTION_SCOPES=all" in env
                and "private,thread,team,workspace,organization" in env,
                "canonical memory text encryption scopes are documented",
            ),
            Check(
                "env:signing-keys",
                "UAM_AUDIT_SIGNING_KEY=" in env
                and "UAM_VAULT_SIGNING_KEY=" in env
                and "UAM_RELEASE_SIGNING_KEY=" in env,
                "operator-held signing keys are documented",
            ),
            Check(
                "env:local-appliance",
                "UAM_SERVER_ID=" in env
                and "UAM_PROJECT_ID=" in env
                and "UAM_PUBLIC_HOST=" not in env
                and "UAM_PUBLIC_EMAIL=" not in env,
                "local-appliance deployment does not require a public endpoint",
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
                "ui:conflict-actions",
                "Принять рекомендацию" in read("web/src/App.tsx")
                and "Скрыть как неактуальный" in read("web/src/App.tsx")
                and "decideConflict(" in api
                and "evidence:" in read("web/src/App.tsx"),
                "operator UI can accept, override or dismiss conflicts",
            ),
            Check(
                "tests:ui-conflict-actions",
                "test_conflict_decision_can_dismiss_without_winner" in tests
                and "decideConflict(" in tests,
                "conflict UI/API decision behavior is covered",
            ),
            Check(
                "ops:metrics-health-evaluator",
                "outbox_dead_letter_total" in read("scripts/check_metrics_health.py")
                and "outbox_lag_seconds" in read("scripts/check_metrics_health.py")
                and "embedding_failures_total" in read("src/memory_plane/services/embedding.py")
                and "embedding_last_duration_seconds"
                in read("src/memory_plane/services/embedding.py")
                and "UAM_METRICS_ALERT_WEBHOOK" in read("scripts/check_metrics_health.py")
                and "UAM_WORKER_METRICS_PORT" in prod_compose
                and "embedding-worker:9091" in read("docs/OBSERVABILITY.md"),
                "embedding worker exports a private Prometheus endpoint",
            ),
            Check(
                "ops:observability-artifacts",
                "uam_outbox_dead_letter_total" in read("deploy/observability/prometheus-alerts.yml")
                and "uam_outbox_lag_seconds" in read("deploy/observability/prometheus-alerts.yml")
                and "uam_embedding_failures_total"
                in read("deploy/observability/prometheus-alerts.yml")
                and "uam_embedding_reindex_failures_total"
                in read("deploy/observability/prometheus-alerts.yml")
                and "uam_memory_items_total" in read("deploy/observability/grafana-dashboard.json")
                and "docs/OBSERVABILITY.md" in read("README.md"),
                "Prometheus alerts and Grafana dashboard cover production metrics",
            ),
            Check(
                "tests:observability-artifacts",
                "test_grafana_dashboard_uses_real_exposed_metrics"
                in read("tests/test_observability_artifacts.py")
                and "test_prometheus_alerts_cover_production_failure_modes"
                in read("tests/test_observability_artifacts.py"),
                "observability artifacts are covered by tests",
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
                '@app.get("/v1/audit/events")' in api and 'path.startswith("/v1/audit")' in api,
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
                "test_export_audit_writes_jsonl_manifest_and_checksum"
                in read("tests/test_backup_restore_scripts.py")
                and "test_export_audit_signs_and_verifies_bundle"
                in read("tests/test_backup_restore_scripts.py")
                and "test_export_audit_can_export_all_pages_with_time_range"
                in read("tests/test_backup_restore_scripts.py"),
                "audit bundle checksum/signature/range behavior is covered by tests",
            ),
            Check(
                "audit:retention-runner",
                "obelisk-audit-retention-v1" in read("scripts/audit_retention.py")
                and '"export_audit.py"' in read("scripts/audit_retention.py")
                and "--verify" in read("scripts/audit_retention.py")
                and "prune_events" in read("scripts/audit_retention.py")
                and "--apply requires --signing-key" in read("scripts/audit_retention.py"),
                "audit retention exports and verifies old windows before pruning",
            ),
            Check(
                "tests:audit-retention-runner",
                "test_audit_retention_apply_prunes_only_after_verify"
                in read("tests/test_backup_restore_scripts.py")
                and "test_audit_retention_does_not_prune_when_verify_fails"
                in read("tests/test_backup_restore_scripts.py")
                and "test_audit_retention_apply_requires_signed_export"
                in read("tests/test_backup_restore_scripts.py"),
                "audit retention safety behavior is covered",
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
                "restore drill restores into isolated PostgreSQL and checks schema presence",
            ),
            Check(
                "tests:restore-drill",
                "test_restore_drill_uses_temporary_docker_target"
                in read("tests/test_backup_restore_scripts.py"),
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
                "release:evidence-verifier",
                "obelisk-release-evidence-manifest-v2" in read("scripts/verify_release_evidence.py")
                and 'SIGNATURE_ALGORITHM = "hmac-sha256"'
                in read("scripts/verify_release_evidence.py")
                and "artifact checksum verified" in read("scripts/verify_release_evidence.py")
                and "artifact path escapes the release bundle"
                in read("scripts/verify_release_evidence.py")
                and "agent_soak" in read("scripts/verify_release_evidence.py")
                and "conversation_pipeline" in read("scripts/verify_release_evidence.py")
                and "obelisk-conversation-pipeline-v1" in read("scripts/verify_release_evidence.py")
                and "embedding" in read("scripts/verify_release_evidence.py")
                and "obelisk-embedding-eval-v1" in read("scripts/verify_release_evidence.py")
                and "load_smoke" in read("scripts/verify_release_evidence.py")
                and "ops_schedule" in read("scripts/verify_release_evidence.py")
                and "obelisk-ops-schedule-preflight-v1"
                in read("scripts/verify_release_evidence.py")
                and "observability" in read("scripts/verify_release_evidence.py")
                and "obelisk-observability-preflight-v1"
                in read("scripts/verify_release_evidence.py")
                and "audit_retention" in read("scripts/verify_release_evidence.py")
                and "deployment_preflight" in read("scripts/verify_release_evidence.py")
                and "obelisk-deployment-preflight-v1" in read("scripts/verify_release_evidence.py")
                and "secret_files" in read("scripts/verify_release_evidence.py")
                and "obelisk-secret-files-preflight-v1"
                in read("scripts/verify_release_evidence.py")
                and "vault_import" in read("scripts/verify_release_evidence.py")
                and "obelisk-vault-import-report-v1" in read("scripts/verify_release_evidence.py")
                and "branch_protection" in read("scripts/verify_release_evidence.py")
                and "ui_walkthrough" in read("scripts/verify_release_evidence.py")
                and "release_notes" in read("scripts/verify_release_evidence.py")
                and "obelisk-release-notes-v1" in read("scripts/verify_release_evidence.py")
                and "ops/ops-schedule.json" in read("docs/RELEASE_EVIDENCE.md")
                and "ops/observability-preflight.json" in read("docs/RELEASE_EVIDENCE.md")
                and "ops/conversation-pipeline.json" in read("docs/RELEASE_EVIDENCE.md")
                and "ops/embedding.json" in read("docs/RELEASE_EVIDENCE.md")
                and "ops/release-notes.json" in read("docs/RELEASE_EVIDENCE.md")
                and "ops/deployment-preflight.json" in read("docs/RELEASE_EVIDENCE.md")
                and "ops/secret-files.json" in read("docs/RELEASE_EVIDENCE.md")
                and "ops/vault-import.json" in read("docs/RELEASE_EVIDENCE.md")
                and "release_evidence=PASS" in read("docs/RELEASE_EVIDENCE.md"),
                "release evidence verifier checks saved production reports",
            ),
            Check(
                "release:evidence-generator",
                "generate_release_evidence_manifest.py" in read("docs/RELEASE_EVIDENCE.md")
                and "REQUIRED_ARTIFACTS" in read("scripts/generate_release_evidence_manifest.py")
                and "DEFAULT_ARTIFACT_PATHS"
                in read("scripts/generate_release_evidence_manifest.py")
                and "image_digest" in read("scripts/generate_release_evidence_manifest.py")
                and "sign_manifest" in read("scripts/generate_release_evidence_manifest.py"),
                "release evidence generator hashes, identifies and signs the bundle",
            ),
            Check(
                "release:notes-generator",
                "generate_release_notes.py" in read("docs/RELEASE_EVIDENCE.md")
                and "generate_release_notes.py" in read("docs/RELEASE_CHECKLIST.md")
                and "obelisk-release-notes-v1" in read("scripts/generate_release_notes.py")
                and '"log", "--oneline"' in read("scripts/generate_release_notes.py")
                and "rollback" in read("scripts/generate_release_notes.py"),
                "release notes generator writes changelog and rollback evidence",
            ),
            Check(
                "tests:release-evidence-verifier",
                "test_verify_release_evidence_accepts_complete_manifest"
                in read("tests/test_backup_restore_scripts.py")
                and "test_verify_release_evidence_rejects_skipped_restore_drill"
                in read("tests/test_backup_restore_scripts.py")
                and "test_verify_release_evidence_rejects_unsigned_vault_import"
                in read("tests/test_backup_restore_scripts.py")
                and "test_verify_release_evidence_rejects_reachable_backend"
                in read("tests/test_backup_restore_scripts.py")
                and "test_verify_release_evidence_rejects_raw_secret_env"
                in read("tests/test_backup_restore_scripts.py")
                and "test_verify_release_evidence_rejects_missing_ops_alert_route"
                in read("tests/test_backup_restore_scripts.py")
                and "test_verify_release_evidence_rejects_missing_observability_alert"
                in read("tests/test_backup_restore_scripts.py")
                and "test_verify_release_evidence_rejects_missing_rollback_steps"
                in read("tests/test_backup_restore_scripts.py")
                and "test_verify_release_evidence_rejects_failed_embedding_eval"
                in read("tests/test_backup_restore_scripts.py")
                and "test_verify_release_evidence_rejects_conversation_pipeline_leak"
                in read("tests/test_backup_restore_scripts.py")
                and "test_verify_release_evidence_rejects_artifact_tampering"
                in read("tests/test_backup_restore_scripts.py")
                and "test_verify_release_evidence_rejects_manifest_tampering"
                in read("tests/test_backup_restore_scripts.py")
                and "test_verify_release_evidence_rejects_path_escape_even_when_signed"
                in read("tests/test_backup_restore_scripts.py")
                and "test_verify_release_evidence_rejects_stale_manifest"
                in read("tests/test_backup_restore_scripts.py")
                and "test_verify_release_evidence_rejects_report_from_other_target"
                in read("tests/test_backup_restore_scripts.py"),
                "release evidence semantics, tamper resistance and identity are covered",
            ),
            Check(
                "tests:release-evidence-generator",
                "test_generate_release_evidence_manifest_contains_required_artifacts"
                in read("tests/test_backup_restore_scripts.py")
                and "test_generate_release_evidence_manifest_cli_writes_manifest"
                in read("tests/test_backup_restore_scripts.py"),
                "release evidence manifest generator behavior is covered",
            ),
            Check(
                "tests:release-notes-generator",
                "test_generate_release_notes_builds_changelog_and_rollback"
                in read("tests/test_backup_restore_scripts.py")
                and "test_generate_release_notes_cli_writes_report"
                in read("tests/test_backup_restore_scripts.py"),
                "release notes generator behavior is covered",
            ),
            Check(
                "ops:observability-preflight-runner",
                "obelisk-observability-preflight-v1" in read("scripts/observability_preflight.py")
                and "grafana-dashboard:required-metrics"
                in read("scripts/observability_preflight.py")
                and "prometheus-alerts:required-alerts"
                in read("scripts/observability_preflight.py")
                and "ObeliskReindexFailures" in read("scripts/observability_preflight.py"),
                "observability preflight validates dashboard and alert coverage",
            ),
            Check(
                "tests:observability-preflight-runner",
                "test_observability_preflight_accepts_repository_artifacts"
                in read("tests/test_backup_restore_scripts.py")
                and "test_observability_preflight_rejects_missing_alert"
                in read("tests/test_backup_restore_scripts.py"),
                "observability preflight behavior is covered",
            ),
            Check(
                "ops:schedule-preflight-runner",
                "obelisk-ops-schedule-preflight-v1" in read("scripts/ops_schedule_preflight.py")
                and "scheduled_backup.py" in read("scripts/ops_schedule_preflight.py")
                and "audit_retention.py" in read("scripts/ops_schedule_preflight.py")
                and "check_metrics_health.py" in read("scripts/ops_schedule_preflight.py")
                and "durable-prefix" in read("scripts/ops_schedule_preflight.py"),
                "ops schedule preflight validates schedules, alerts and artifact roots",
            ),
            Check(
                "tests:ops-schedule-preflight-runner",
                "test_ops_schedule_preflight_accepts_installed_schedules"
                in read("tests/test_backup_restore_scripts.py")
                and "test_ops_schedule_preflight_rejects_local_artifact_storage"
                in read("tests/test_backup_restore_scripts.py"),
                "ops schedule preflight behavior is covered",
            ),
            Check(
                "deploy:preflight-runner",
                "obelisk-deployment-preflight-v1" in read("scripts/deployment_preflight.py")
                and "public-url-https" in read("scripts/deployment_preflight.py")
                and "backend-not-public" in read("scripts/deployment_preflight.py")
                and "public-security-headers" in read("scripts/deployment_preflight.py"),
                "deployment preflight runner validates public TLS and backend exposure",
            ),
            Check(
                "tests:deployment-preflight-runner",
                "test_deployment_preflight_passes_when_public_https_and_backend_blocked"
                in read("tests/test_backup_restore_scripts.py")
                and "test_deployment_preflight_fails_when_backend_is_public"
                in read("tests/test_backup_restore_scripts.py"),
                "deployment preflight behavior is covered",
            ),
            Check(
                "secrets:preflight-runner",
                "obelisk-secret-files-preflight-v1" in read("scripts/secret_files_preflight.py")
                and "raw-empty" in read("scripts/secret_files_preflight.py")
                and "file-configured" in read("scripts/secret_files_preflight.py")
                and "file-readable" in read("scripts/secret_files_preflight.py")
                and "file-prefix" in read("scripts/secret_files_preflight.py"),
                "secret-files preflight runner validates mounted secret posture",
            ),
            Check(
                "tests:secret-files-preflight-runner",
                "test_secret_files_preflight_accepts_file_backed_secrets"
                in read("tests/test_backup_restore_scripts.py")
                and "test_secret_files_preflight_rejects_raw_secret_values"
                in read("tests/test_backup_restore_scripts.py"),
                "secret-files preflight behavior is covered",
            ),
            Check(
                "ui:walkthrough-runner",
                "obelisk-ui-walkthrough-v1" in read("scripts/ui_walkthrough_eval.py")
                and "vault-editable-text" in read("scripts/ui_walkthrough_eval.py")
                and "model-settings-probe" in read("scripts/ui_walkthrough_eval.py")
                and "FORBIDDEN_EDIT_TOKENS" in read("scripts/ui_walkthrough_eval.py"),
                "live UI walkthrough runner validates editable vault text and operator flows",
            ),
            Check(
                "tests:ui-walkthrough-runner",
                "test_ui_walkthrough_eval_passes_operator_flows"
                in read("tests/test_ui_walkthrough_eval.py")
                and "test_ui_walkthrough_eval_fails_when_vault_editor_exposes_vectors"
                in read("tests/test_ui_walkthrough_eval.py"),
                "UI walkthrough runner success and vector-leak failure are covered",
            ),
            Check(
                "agents:soak-runner",
                "OpenClaw/Hermes soak checks" in read("scripts/agent_soak_eval.py")
                and "cross-workspace-leakage" in read("scripts/agent_soak_eval.py")
                and "obelisk-agent-soak-v1" in read("scripts/agent_soak_eval.py"),
                "live agent soak runner validates retain/recall/leakage",
            ),
            Check(
                "load:smoke-runner",
                "obelisk-load-smoke-v1" in read("scripts/load_smoke_eval.py")
                and "concurrent-retain-recall" in read("scripts/load_smoke_eval.py")
                and "retain-p95" in read("scripts/load_smoke_eval.py")
                and "metrics-backlog" in read("scripts/load_smoke_eval.py"),
                "load smoke runner validates concurrent retain/recall, latency and backlog",
            ),
            Check(
                "tests:load-smoke-runner",
                "test_load_smoke_eval_passes_parallel_retain_recall"
                in read("tests/test_load_smoke_eval.py")
                and "test_load_smoke_eval_fails_on_backlog_metrics"
                in read("tests/test_load_smoke_eval.py"),
                "load smoke runner behavior is covered",
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
                'MANIFEST_FORMAT = "obelisk-vault-manifest-v1"' in read("scripts/vault_manifest.py")
                and 'SIGNATURE_ALGORITHM = "hmac-sha256"' in read("scripts/vault_manifest.py")
                and "--require-signature" in read("scripts/import_vault.py")
                and "--json-report" in read("scripts/import_vault.py")
                and "obelisk-vault-import-report-v1" in read("scripts/import_vault.py")
                and "UAM_VAULT_SIGNING_KEY" in read("scripts/export_vault.py"),
                "vault export/import supports manifest checksum, HMAC signatures and evidence",
            ),
            Check(
                "tests:vault-signed-manifest",
                "test_export_vault_can_sign_manifest"
                in read("tests/test_backup_restore_scripts.py")
                and "test_import_vault_verifies_signed_manifest_before_apply"
                in read("tests/test_backup_restore_scripts.py")
                and "test_import_vault_rejects_tampered_signed_manifest"
                in read("tests/test_backup_restore_scripts.py"),
                "signed vault manifest behavior is covered by tests",
            ),
            Check(
                "env:validator",
                "PLACEHOLDER_PATTERNS" in read("scripts/validate_production_env.py")
                and "--require-public-tls" in read("scripts/validate_production_env.py")
                and "--require-real-embeddings" in read("scripts/validate_production_env.py")
                and "UAM_QDRANT_PAYLOAD_TEXT" in read("scripts/validate_production_env.py")
                and "UAM_MEMORY_TEXT_ENCRYPTION" in read("scripts/validate_production_env.py"),
                "production env validator rejects placeholder/local-only config",
            ),
            Check(
                "tests:env-validator",
                "test_validate_production_env_accepts_strict_real_config"
                in read("tests/test_backup_restore_scripts.py")
                and "test_validate_production_env_rejects_placeholders"
                in read("tests/test_backup_restore_scripts.py"),
                "production env validator behavior is covered",
            ),
            Check(
                "qdrant:redacted-payload",
                "payload_text" in read("src/memory_plane/adapters/qdrant.py")
                and "text_redacted" in read("src/memory_plane/adapters/qdrant.py")
                and "_payload_to_candidate_item" in read("src/memory_plane/adapters/qdrant.py"),
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
                and "pgp_sym_decrypt" in read("src/memory_plane/adapters/postgres.py")
                and "UAM_MEMORY_TEXT_ENCRYPTION_SCOPES"
                in read("src/memory_plane/adapters/postgres.py")
                and "_should_encrypt_item" in read("src/memory_plane/adapters/postgres.py"),
                "PostgreSQL canonical memory text can be encrypted with pgcrypto by scope",
            ),
            Check(
                "tests:postgres-pgcrypto-text",
                "test_postgres_pgcrypto_mode_requires_key"
                in read("tests/test_postgres_encryption.py")
                and "test_postgres_encrypts_memory_text_before_insert"
                in read("tests/test_postgres_encryption.py")
                and "test_postgres_encrypts_only_selected_memory_scopes"
                in read("tests/test_postgres_encryption.py"),
                "PostgreSQL memory text encryption behavior is covered",
            ),
            Check(
                "conversation:pipeline-runner",
                "obelisk-conversation-pipeline-v1" in read("scripts/conversation_pipeline_eval.py")
                and "raw-turn-not-recalled" in read("scripts/conversation_pipeline_eval.py")
                and "curated-memory-recalled" in read("scripts/conversation_pipeline_eval.py"),
                "conversation pipeline runner validates raw capture, curation and recall",
            ),
            Check(
                "conversation:curated-only-purge",
                "purge_turn_content" in read("src/memory_plane/services/conversations.py")
                and "test_curated_only_policy_purges_raw_text" in tests,
                "curated-only policy purges raw message content after curation",
            ),
            Check(
                "conversation:staging-ttl",
                "UAM_CONVERSATION_CURATED_ONLY_TTL_SECONDS" in env
                and "purge_expired_turns" in read("src/memory_plane/services/conversations.py")
                and "test_curated_only_staging_ttl_purges_abandoned_raw_text" in tests,
                "curated-only staging has bounded TTL and operator purge coverage",
            ),
            Check(
                "conversation:retention-schedule",
                "purge_expired_conversations.py" in read("deploy/ops/conversation-retention.cron")
                and "UAM_RETENTION_OPERATOR_KEY" in read("deploy/ops/conversation-retention.cron"),
                "hourly transcript-staging retention schedule is supplied",
            ),
            Check(
                "proposals:atomic-accept",
                "accept_proposal_with_memory" in read("src/memory_plane/adapters/postgres.py")
                and "accept_proposal_with_memory" in read("src/memory_plane/services/proposals.py"),
                "proposal acceptance has one PostgreSQL memory/outbox/status transaction",
            ),
            Check(
                "tests:conversation-pipeline-runner",
                "test_conversation_pipeline_eval_passes_full_pipeline"
                in read("tests/test_conversation_pipeline_eval.py")
                and "test_conversation_pipeline_eval_fails_when_raw_turn_leaks_into_recall"
                in read("tests/test_conversation_pipeline_eval.py"),
                "conversation pipeline runner behavior is covered",
            ),
            Check(
                "embedding:live-regression-runner",
                "obelisk-embedding-eval-v1" in read("scripts/real_embedding_eval.py")
                and "production embedding model" in read("scripts/real_embedding_eval.py")
                and "--json-report" in read("scripts/real_embedding_eval.py"),
                "live OpenAI-compatible embedding runner validates dimension and semantic recall",
            ),
            Check(
                "tests:embedding-live-regression-runner",
                "test_real_embedding_eval_passes_semantic_contract"
                in read("tests/test_real_embedding_eval.py")
                and "test_real_embedding_eval_fails_dimension_mismatch"
                in read("tests/test_real_embedding_eval.py")
                and "test_real_embedding_eval_fails_wrong_semantic_top"
                in read("tests/test_real_embedding_eval.py"),
                "embedding live regression runner behavior is covered",
            ),
            Check(
                "llm:live-regression-runner",
                "obelisk-memory-llm-eval-v1" in read("scripts/real_memory_llm_eval.py")
                and "json-memory-curation" in read("scripts/real_memory_llm_eval.py")
                and "openai-compatible" in read("scripts/real_memory_llm_eval.py"),
                "live OpenAI-compatible memory LLM runner validates chat and curation",
            ),
            Check(
                "tests:llm-live-regression-runner",
                "test_real_memory_llm_eval_passes_memory_contract"
                in read("tests/test_real_memory_llm_eval.py")
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
                "Obelisk Memory passes repository-level envelope checks. This does not "
                "certify runtime correctness, a trusted pilot, or production readiness; "
                "see the production gap audit."
                if failed == 0
                else (
                    "Obelisk Memory does not pass repository-level envelope checks "
                    "until failed checks are fixed."
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
