# Production envelope report — 2026-07-10

Passed: 72
Failed: 0

| Check | Status | Detail |
|---|---:|---|
| `file:README.md` | PASS | required production artifact |
| `file:SECURITY.md` | PASS | required production artifact |
| `file:.env.production.example` | PASS | required production artifact |
| `file:docker-compose.prod.yml` | PASS | required production artifact |
| `file:.github/workflows/ci.yml` | PASS | required production artifact |
| `file:migrations/008_audit_events.sql` | PASS | required production artifact |
| `file:migrations/009_api_key_registry.sql` | PASS | required production artifact |
| `file:scripts/check_branch_protection.py` | PASS | required production artifact |
| `file:scripts/check_metrics_health.py` | PASS | required production artifact |
| `file:scripts/export_audit.py` | PASS | required production artifact |
| `file:scripts/agent_soak_eval.py` | PASS | required production artifact |
| `file:scripts/vault_manifest.py` | PASS | required production artifact |
| `file:scripts/restore_drill.py` | PASS | required production artifact |
| `file:scripts/scheduled_backup.py` | PASS | required production artifact |
| `file:docs/assets/obelisk-memory-hero.png` | PASS | required production artifact |
| `file:docs/GITHUB_BRANCH_PROTECTION.md` | PASS | required production artifact |
| `file:docs/OPERATIONS_RUNBOOK.md` | PASS | required production artifact |
| `file:docs/ENTERPRISE_READINESS.md` | PASS | required production artifact |
| `file:docs/PRODUCTION_GAP_AUDIT_2026_07_10.md` | PASS | required production artifact |
| `file:docs/RELEASE_CHECKLIST.md` | PASS | required production artifact |
| `file:docs/DGX_SPARK_MEMORY_LLM.md` | PASS | required production artifact |
| `file:docs/BENCHMARK_RESULTS_2026_07_09.md` | PASS | required production artifact |
| `readme:brand` | PASS | README uses product name |
| `readme:hero` | PASS | README references generated hero asset |
| `readme:production` | PASS | prod path documented |
| `readme:honest-status` | PASS | README does not over-claim full production readiness |
| `readme:gap-audit` | PASS | README links the honest production gap audit |
| `readme:agents` | PASS | agent adapters documented |
| `readme:agent-soak` | PASS | README documents live agent soak evidence |
| `readme:128k` | PASS | 128k context budget documented |
| `readme:dgx` | PASS | DGX Spark .10 endpoint documented |
| `prod-compose:only-api-published` | PASS | production compose publishes API/UI but not PostgreSQL |
| `prod-compose:internal-qdrant` | PASS | Qdrant is internal in production |
| `prod-compose:nats-health` | PASS | NATS JetStream has monitoring healthcheck |
| `prod-compose:required-api-key` | PASS | production API key is required |
| `ci:ruff` | PASS | CI runs ruff |
| `ci:pytest` | PASS | CI runs pytest |
| `ci:web-build` | PASS | CI builds web UI |
| `ci:production-readiness-eval` | PASS | CI runs in-process production readiness eval |
| `ci:prod-compose` | PASS | CI validates production compose |
| `release:branch-protection-verifier` | PASS | branch-protection verifier checks PR, status, and admin enforcement |
| `tests:branch-protection-verifier` | PASS | branch-protection verifier behavior is covered by tests |
| `env:memory-llm` | PASS | Qwen memory LLM endpoint |
| `env:embeddings` | PASS | embedding endpoint |
| `env:privacy` | PASS | privacy defaults |
| `env:scoped-keys` | PASS | scoped API keys documented |
| `env:signing-keys` | PASS | operator-held signing keys are documented |
| `api:security-headers` | PASS | API applies baseline security headers |
| `tests:security-headers` | PASS | security headers are covered by API tests |
| `ops:metrics-health-evaluator` | PASS | metrics health script evaluates outbox lag/dead letters; embedding exposes failure/latency metrics |
| `tests:metrics-health-evaluator` | PASS | metrics health thresholds and report behavior are covered |
| `audit:rls` | PASS | audit events are durable and tenant-isolated |
| `audit:operator-export` | PASS | audit export endpoint is operator-scoped |
| `tests:audit-trail` | PASS | audit trail behavior is covered by API tests |
| `audit:tamper-evident-bundle` | PASS | audit script exports JSONL plus checksum and optional signature |
| `audit:range-export` | PASS | audit export supports time-window pagination |
| `tests:audit-export-bundle` | PASS | audit bundle checksum/signature/range behavior is covered by tests |
| `keys:registry-rls` | PASS | API key registry stores non-secret metadata under RLS |
| `keys:operator-api` | PASS | API key registry is operator-scoped |
| `tests:key-registry` | PASS | key registry last-used and revocation behavior is covered |
| `restore-drill:script` | PASS | restore drill verifies backups in isolated PostgreSQL |
| `tests:restore-drill` | PASS | restore drill command flow is covered by tests |
| `backup:schedule-runner` | PASS | scheduled backup runner performs backup, restore drill and alert hook |
| `tests:scheduled-backup` | PASS | scheduled backup success/failure reporting is covered |
| `agents:soak-runner` | PASS | live agent soak runner validates retain/recall/leakage |
| `tests:agent-soak-runner` | PASS | agent soak runner success and leakage failure are covered |
| `vault:signed-manifest` | PASS | vault export/import supports manifest checksum and HMAC signatures |
| `tests:vault-signed-manifest` | PASS | signed vault manifest behavior is covered by tests |
| `gap-audit:no-overclaim` | PASS | gap audit explicitly forbids readiness over-claims |
| `gap-audit:full-production-gates` | PASS | gap audit defines full-production gates |
| `benchmark:passed` | PASS | latest benchmark pass count |
| `benchmark:no-failures` | PASS | latest benchmark failure count |

## Verdict

Obelisk Memory passes the repository-level trusted self-hosted pilot gate. This is not a full-production certification; see the production gap audit.
