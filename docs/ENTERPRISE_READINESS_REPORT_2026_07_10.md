# Production envelope report — 2026-07-10

Passed: 140
Failed: 0

| Check | Status | Detail |
|---|---:|---|
| `file:README.md` | PASS | required production artifact |
| `file:SECURITY.md` | PASS | required production artifact |
| `file:.env.production.example` | PASS | required production artifact |
| `file:docker-compose.prod.yml` | PASS | required production artifact |
| `file:deploy/reverse-proxy/Caddyfile` | PASS | required production artifact |
| `file:deploy/reverse-proxy/docker-compose.caddy.yml` | PASS | required production artifact |
| `file:.github/workflows/ci.yml` | PASS | required production artifact |
| `file:src/memory_plane/config/secrets.py` | PASS | required production artifact |
| `file:migrations/008_audit_events.sql` | PASS | required production artifact |
| `file:migrations/009_api_key_registry.sql` | PASS | required production artifact |
| `file:scripts/check_branch_protection.py` | PASS | required production artifact |
| `file:scripts/check_metrics_health.py` | PASS | required production artifact |
| `file:scripts/deployment_preflight.py` | PASS | required production artifact |
| `file:scripts/observability_preflight.py` | PASS | required production artifact |
| `file:scripts/ops_schedule_preflight.py` | PASS | required production artifact |
| `file:scripts/secret_files_preflight.py` | PASS | required production artifact |
| `file:scripts/validate_production_env.py` | PASS | required production artifact |
| `file:scripts/export_audit.py` | PASS | required production artifact |
| `file:scripts/agent_soak_eval.py` | PASS | required production artifact |
| `file:scripts/conversation_pipeline_eval.py` | PASS | required production artifact |
| `file:scripts/load_smoke_eval.py` | PASS | required production artifact |
| `file:scripts/ui_walkthrough_eval.py` | PASS | required production artifact |
| `file:scripts/real_embedding_eval.py` | PASS | required production artifact |
| `file:scripts/real_memory_llm_eval.py` | PASS | required production artifact |
| `file:scripts/generate_release_evidence_manifest.py` | PASS | required production artifact |
| `file:scripts/generate_release_notes.py` | PASS | required production artifact |
| `file:scripts/vault_manifest.py` | PASS | required production artifact |
| `file:scripts/restore_drill.py` | PASS | required production artifact |
| `file:scripts/scheduled_backup.py` | PASS | required production artifact |
| `file:scripts/audit_retention.py` | PASS | required production artifact |
| `file:scripts/verify_release_evidence.py` | PASS | required production artifact |
| `file:docs/assets/obelisk-memory-hero.png` | PASS | required production artifact |
| `file:docs/GITHUB_BRANCH_PROTECTION.md` | PASS | required production artifact |
| `file:docs/OPERATIONS_RUNBOOK.md` | PASS | required production artifact |
| `file:docs/OBSERVABILITY.md` | PASS | required production artifact |
| `file:docs/TLS_REVERSE_PROXY.md` | PASS | required production artifact |
| `file:docs/ENTERPRISE_READINESS.md` | PASS | required production artifact |
| `file:docs/PRODUCTION_GAP_AUDIT_2026_07_10.md` | PASS | required production artifact |
| `file:docs/RELEASE_CHECKLIST.md` | PASS | required production artifact |
| `file:docs/RELEASE_EVIDENCE.md` | PASS | required production artifact |
| `file:docs/DGX_SPARK_MEMORY_LLM.md` | PASS | required production artifact |
| `file:deploy/observability/grafana-dashboard.json` | PASS | required production artifact |
| `file:deploy/observability/prometheus-alerts.yml` | PASS | required production artifact |
| `readme:brand` | PASS | README uses product name |
| `readme:hero` | PASS | README references generated hero asset |
| `readme:production-reference` | PASS | README documents the reference topology without approving production |
| `readme:honest-status` | PASS | README does not over-claim full production readiness |
| `readme:gap-audit` | PASS | README links the honest production gap audit |
| `readme:agents` | PASS | agent adapters documented |
| `readme:agent-soak` | PASS | README documents live agent soak evidence |
| `readme:env-validation` | PASS | README documents strict production env validation |
| `readme:release-memory-llm-eval` | PASS | README delegates live memory LLM evidence to release documentation |
| `readme:release-ui-walkthrough` | PASS | README delegates live UI walkthrough evidence to release documentation |
| `readme:128k` | PASS | 128k context budget documented |
| `readme:openai-compatible-llm` | PASS | README documents provider-neutral memory LLM endpoint |
| `prod-compose:only-api-published` | PASS | production compose publishes API/UI but not PostgreSQL |
| `prod-compose:internal-qdrant` | PASS | Qdrant is internal in production |
| `prod-compose:nats-health` | PASS | NATS JetStream has monitoring healthcheck |
| `prod-compose:secret-files` | PASS | production compose includes dedicated database secret mounts and *_FILE paths |
| `prod-compose:provider-neutral-embeddings` | PASS | production API and worker use provider-neutral embedding defaults |
| `prod-compose:text-encryption` | PASS | production API and embedding worker receive canonical text encryption settings |
| `prod-compose:qdrant-redacted-payload` | PASS | production API and embedding worker keep raw text out of Qdrant payloads |
| `reverse-proxy:caddy-overlay` | PASS | Caddy TLS reverse proxy example exists |
| `docs:tls-reverse-proxy` | PASS | TLS reverse proxy guide documents backend exposure limits |
| `ci:ruff` | PASS | CI runs ruff |
| `ci:pytest` | PASS | CI runs pytest |
| `ci:web-build` | PASS | CI builds web UI |
| `ci:production-readiness-eval` | PASS | CI runs in-process production readiness eval |
| `ci:prod-compose` | PASS | CI validates production compose |
| `ci:reverse-proxy-compose` | PASS | CI validates reverse proxy compose overlay |
| `ci:env-placeholder-guard` | PASS | CI confirms placeholder env cannot pass strict production validation |
| `release:branch-protection-verifier` | PASS | branch-protection verifier checks PR, status, and admin enforcement |
| `tests:branch-protection-verifier` | PASS | branch-protection verifier behavior is covered by tests |
| `env:memory-llm` | PASS | OpenAI-compatible memory LLM endpoint |
| `env:embeddings` | PASS | OpenAI-compatible embedding endpoint |
| `env:privacy` | PASS | privacy defaults |
| `env:scoped-keys` | PASS | scoped API keys documented |
| `env:secret-files` | PASS | mounted secret-file env alternatives are documented |
| `env:text-encryption-scopes` | PASS | canonical memory text encryption scopes are documented |
| `env:signing-keys` | PASS | operator-held signing keys are documented |
| `env:public-host` | PASS | public TLS endpoint env is documented |
| `api:security-headers` | PASS | API applies baseline security headers |
| `tests:security-headers` | PASS | security headers are covered by API tests |
| `ui:conflict-actions` | PASS | operator UI can accept, override or dismiss conflicts |
| `tests:ui-conflict-actions` | PASS | conflict UI/API decision behavior is covered |
| `ops:metrics-health-evaluator` | PASS | static metrics contracts cover outbox and embedding counters; worker export remains a runtime gap |
| `ops:observability-artifacts` | PASS | Prometheus alerts and Grafana dashboard cover production metrics |
| `tests:observability-artifacts` | PASS | observability artifacts are covered by tests |
| `tests:metrics-health-evaluator` | PASS | metrics health thresholds and report behavior are covered |
| `audit:rls` | PASS | audit events are durable and tenant-isolated |
| `audit:operator-export` | PASS | audit export endpoint is operator-scoped |
| `tests:audit-trail` | PASS | audit trail behavior is covered by API tests |
| `audit:tamper-evident-bundle` | PASS | audit script exports JSONL plus checksum and optional signature |
| `audit:range-export` | PASS | audit export supports time-window pagination |
| `tests:audit-export-bundle` | PASS | audit bundle checksum/signature/range behavior is covered by tests |
| `audit:retention-runner` | PASS | audit retention exports and verifies old windows before pruning |
| `tests:audit-retention-runner` | PASS | audit retention safety behavior is covered |
| `keys:registry-rls` | PASS | API key registry stores non-secret metadata under RLS |
| `keys:operator-api` | PASS | API key registry is operator-scoped |
| `tests:key-registry` | PASS | key registry last-used and revocation behavior is covered |
| `restore-drill:script` | PASS | restore drill restores into isolated PostgreSQL and checks schema presence |
| `tests:restore-drill` | PASS | restore drill command flow is covered by tests |
| `backup:schedule-runner` | PASS | scheduled backup runner performs backup, restore drill and alert hook |
| `tests:scheduled-backup` | PASS | scheduled backup success/failure reporting is covered |
| `release:evidence-verifier` | PASS | release evidence verifier checks saved production reports |
| `release:evidence-generator` | PASS | release evidence generator hashes, identifies and signs the bundle |
| `release:notes-generator` | PASS | release notes generator writes changelog and rollback evidence |
| `tests:release-evidence-verifier` | PASS | release evidence semantics, tamper resistance and identity are covered |
| `tests:release-evidence-generator` | PASS | release evidence manifest generator behavior is covered |
| `tests:release-notes-generator` | PASS | release notes generator behavior is covered |
| `ops:observability-preflight-runner` | PASS | observability preflight validates dashboard and alert coverage |
| `tests:observability-preflight-runner` | PASS | observability preflight behavior is covered |
| `ops:schedule-preflight-runner` | PASS | ops schedule preflight validates schedules, alerts and artifact roots |
| `tests:ops-schedule-preflight-runner` | PASS | ops schedule preflight behavior is covered |
| `deploy:preflight-runner` | PASS | deployment preflight runner validates public TLS and backend exposure |
| `tests:deployment-preflight-runner` | PASS | deployment preflight behavior is covered |
| `secrets:preflight-runner` | PASS | secret-files preflight runner validates mounted secret posture |
| `tests:secret-files-preflight-runner` | PASS | secret-files preflight behavior is covered |
| `ui:walkthrough-runner` | PASS | live UI walkthrough runner validates editable vault text and operator flows |
| `tests:ui-walkthrough-runner` | PASS | UI walkthrough runner success and vector-leak failure are covered |
| `agents:soak-runner` | PASS | live agent soak runner validates retain/recall/leakage |
| `load:smoke-runner` | PASS | load smoke runner validates concurrent retain/recall, latency and backlog |
| `tests:load-smoke-runner` | PASS | load smoke runner behavior is covered |
| `tests:agent-soak-runner` | PASS | agent soak runner success and leakage failure are covered |
| `vault:signed-manifest` | PASS | vault export/import supports manifest checksum, HMAC signatures and evidence |
| `tests:vault-signed-manifest` | PASS | signed vault manifest behavior is covered by tests |
| `env:validator` | PASS | production env validator rejects placeholder/local-only config |
| `tests:env-validator` | PASS | production env validator behavior is covered |
| `qdrant:redacted-payload` | PASS | Qdrant can store vectors/filter metadata without raw memory text |
| `tests:qdrant-redacted-payload` | PASS | Qdrant payload redaction and ledger hydration are covered |
| `postgres:pgcrypto-text` | PASS | PostgreSQL canonical memory text can be encrypted with pgcrypto by scope |
| `tests:postgres-pgcrypto-text` | PASS | PostgreSQL memory text encryption behavior is covered |
| `conversation:pipeline-runner` | PASS | conversation pipeline runner validates raw capture, curation and recall |
| `tests:conversation-pipeline-runner` | PASS | conversation pipeline runner behavior is covered |
| `embedding:live-regression-runner` | PASS | live OpenAI-compatible embedding runner validates dimension and semantic recall |
| `tests:embedding-live-regression-runner` | PASS | embedding live regression runner behavior is covered |
| `llm:live-regression-runner` | PASS | live OpenAI-compatible memory LLM runner validates chat and curation |
| `tests:llm-live-regression-runner` | PASS | memory LLM live regression runner behavior is covered |
| `gap-audit:no-overclaim` | PASS | gap audit explicitly forbids readiness over-claims |
| `gap-audit:full-production-gates` | PASS | gap audit defines full-production gates |

## Verdict

Obelisk Memory passes repository-level envelope checks. This does not certify runtime correctness, a trusted pilot, or production readiness; see the production gap audit.
