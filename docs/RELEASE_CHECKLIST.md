# Release checklist

Use this before tagging or pushing a production release.

```bash
# Load the exact provider/model endpoints used by this deployment.
set -a
. ./.env.production
set +a

export RELEASE_ID=2026.07.10
export SOURCE_COMMIT="$(git rev-parse HEAD)"
export IMAGE_DIGEST='sha256:<64-hex-oci-digest>'
export DEPLOYMENT_ID='production-primary'
export RELEASE_API_URL='http://127.0.0.1:6798'
export RELEASE_PUBLIC_URL='http://127.0.0.1:6798'
export UAM_RELEASE_SIGNING_KEY_FILE=/run/secrets/obelisk_release_signing_key

# The running server must expose these same values at /v1/system/status.
test "$UAM_SOURCE_COMMIT" = "$SOURCE_COMMIT"
test "$UAM_IMAGE_DIGEST" = "$IMAGE_DIGEST"
test "$UAM_DEPLOYMENT_ID" = "$DEPLOYMENT_ID"

ruff check src tests scripts agent-integrations
pytest -q
PYTHONPATH=src python scripts/production_readiness_eval.py
python scripts/validate_production_env.py .env.production \
  --require-public-tls \
  --require-signed-artifacts \
  --require-real-embeddings
PYTHONPATH=src python scripts/ops_schedule_preflight.py .env.production \
  --backup-schedule-file ./deploy/schedules/backup.timer \
  --audit-retention-schedule-file ./deploy/schedules/audit-retention.timer \
  --metrics-schedule-file ./deploy/schedules/metrics-health.timer \
  --backup-artifact-root s3://obelisk-memory/backups \
  --audit-artifact-root s3://obelisk-memory/audit \
  --report ./ops/ops-schedule.json
PYTHONPATH=src python scripts/observability_preflight.py \
  --grafana-dashboard ./deploy/observability/grafana-dashboard.json \
  --prometheus-alerts ./deploy/observability/prometheus-alerts.yml \
  --report ./ops/observability-preflight.json
PYTHONPATH=src python scripts/scheduled_backup.py \
  --backup-dir ./backups \
  --audit-dir ./audit-export \
  --report ./backups/latest-backup-report.json
UAM_AUDIT_SIGNING_KEY_FILE=/run/secrets/uam_audit_signing_key \
UAM_AUDIT_RETENTION_DATABASE_URL_FILE=/run/secrets/uam_audit_retention_database_url \
PYTHONPATH=src python scripts/audit_retention.py \
  --retain-days 365 \
  --export-root ./audit-retention \
  --json-report ./ops/audit-retention.json
PYTHONPATH=src python scripts/secret_files_preflight.py .env.production \
  --report ./ops/secret-files.json
UAM_VAULT_SIGNING_KEY=... python scripts/export_vault.py ./vault-release
UAM_VAULT_SIGNING_KEY=... python scripts/import_vault.py ./vault-release \
  --require-signature \
  --json-report ./ops/vault-import.json
UAM_API_KEY=... PYTHONPATH=src python scripts/check_metrics_health.py \
  --metrics-url http://localhost:6798/metrics \
  --max-worker-unready 0 \
  --require-metric uam_worker_required \
  --require-metric uam_worker_ready \
  --require-metric uam_worker_unready \
  --require-metric uam_worker_missing \
  --require-metric uam_worker_stale \
  --report ./ops/metrics-health.json
UAM_API_KEY=... python scripts/agent_soak_eval.py \
  --base-url "$RELEASE_API_URL" \
  --rounds 5 \
  --parallel 4 \
  --json-report ./ops/agent-soak.json
UAM_API_KEY=... python scripts/load_smoke_eval.py \
  --base-url "$RELEASE_API_URL" \
  --agents 8 \
  --operations-per-agent 5 \
  --json-report ./ops/load-smoke.json
UAM_API_KEY=... python scripts/ui_walkthrough_eval.py \
  --base-url "$RELEASE_API_URL" \
  --json-report ./ops/ui-walkthrough.json
UAM_API_KEY=... python scripts/conversation_pipeline_eval.py \
  --base-url "$RELEASE_API_URL" \
  --json-report ./ops/conversation-pipeline.json
python scripts/real_embedding_eval.py \
  --provider "$UAM_EMBEDDING_PROVIDER" \
  --base-url "$UAM_EMBEDDING_BASE_URL" \
  --model "$UAM_EMBEDDING_MODEL" \
  --dimension "$UAM_EMBEDDING_DIM" \
  --json-report ./ops/embedding.json
python scripts/real_memory_llm_eval.py \
  --provider "$UAM_MEMORY_LLM_PROVIDER" \
  --base-url "$UAM_MEMORY_LLM_BASE_URL" \
  --model "$UAM_MEMORY_LLM_MODEL" \
  --json-report ./ops/memory-llm.json
GITHUB_TOKEN=... python scripts/check_branch_protection.py \
  --repo Alex12571333/universal-agent-memory \
  --required-check python \
  --required-check web \
  --json > ./ops/branch-protection.json
python scripts/generate_release_notes.py \
  --release "$RELEASE_ID" \
  --previous-ref v2026.07.09 \
  --current-ref "$SOURCE_COMMIT" \
  --evidence-manifest ./release-evidence.json \
  --output ./ops/release-notes.json
python scripts/generate_release_evidence_manifest.py \
  --release "$RELEASE_ID" \
  --source-commit "$SOURCE_COMMIT" \
  --image-digest "$IMAGE_DIGEST" \
  --deployment-id "$DEPLOYMENT_ID" \
  --api-url "$RELEASE_API_URL" \
  --public-url "$RELEASE_PUBLIC_URL" \
  --signing-key-id production-release-key-2026 \
  --output ./release-evidence.json
python scripts/verify_release_evidence.py ./release-evidence.json \
  --expected-source-commit "$SOURCE_COMMIT" \
  --expected-image-digest "$IMAGE_DIGEST" \
  --expected-deployment-id "$DEPLOYMENT_ID"
docker compose --profile advanced config
docker compose -f docker-compose.prod.yml --env-file .env.production config
python scripts/benchmark_suite.py
python scripts/enterprise_readiness_check.py
```

Manual checks:

- Open `http://localhost:6798/ui` and verify dashboard, graph, vault, settings.
- Confirm `scripts/validate_production_env.py .env.production` passes with
  strict production flags.
- Retain and recall a Russian and English memory.
- Verify conflict inbox can list and resolve at least one conflict.
- In a controlled non-production edit test, export with `--no-manifest`, edit a
  note, run dry-run import, then apply only after review. Do not preserve this as
  signed release evidence.
- Confirm release/operator integrity bundles remain unchanged after signing and
  use `--require-signature`. The current CLI cannot re-sign an edited vault.
- Confirm `ops/vault-import.json` reports `"ok": true`,
  `"require_signature": true`, `"manifest_verified": true` and
  `"manifest_signed": true`.
- Confirm the configured OpenAI-compatible memory LLM endpoint is reachable.
- Confirm `ops/memory-llm.json` reports `"ok": true` for that endpoint/model.
- Confirm `ops/conversation-pipeline.json` reports `"ok": true`, proves raw
  turns are not recalled before curation, and proves curated memory is recalled.
- Confirm embedding endpoint returns the configured dimension.
- Confirm `ops/embedding.json` reports `"ok": true` for the configured
  provider/base URL/model/dimension and semantic recall scenarios.
- Confirm `UAM_QDRANT_PAYLOAD_TEXT=false` so Qdrant stores vectors/filter
  metadata only and memory text is hydrated from PostgreSQL.
- Confirm `UAM_MEMORY_TEXT_ENCRYPTION=pgcrypto` and
  `UAM_MEMORY_TEXT_ENCRYPTION_KEY` are supplied from a secret manager, not from
  the repository.
- Confirm `ops/secret-files.json` reports `"ok": true` so required production
  secrets come from mounted `*_FILE` paths, not raw env values.
- Confirm `UAM_MEMORY_TEXT_ENCRYPTION_SCOPES=all` or a documented selective
  scope policy such as `private,thread`.
- Confirm non-local deployments use HTTPS through the reverse proxy and direct
  backend port `6798` is localhost-only or blocked by firewall/security group.
- Confirm `ops/deployment-preflight.json` reports `"ok": true` and
  `"backend_publicly_reachable": false`.
- Confirm `audit-export/manifest.sha256` verifies before preserving release
  evidence.
- Confirm signed audit bundles verify with `scripts/export_audit.py --verify`.
- Confirm incident/audit exports use `--all-pages` for multi-day windows.
- Send one invalid credential and one insufficient-scope request, then confirm
  the operator audit endpoint contains redacted `auth.request.denied` rows with
  fixed `reason` values and no submitted token, query string or request body.
- Confirm `ops/audit-retention.json` reports `"ok": true`,
  `"verified_export": true` and `"signed_export": true` before any audit prune.
- Confirm `backups/latest-backup-report.json` reports `"ok": true`.
- Confirm `ops/metrics-health.json` reports `"ok": true`.
- Confirm `ops/ops-schedule.json` reports `"ok": true`, with installed
  backup/audit-retention/metrics schedules, alert routes and durable artifact
  roots.
- Confirm `ops/observability-preflight.json` reports `"ok": true`, with
  Grafana dashboard and Prometheus alert coverage for required production
  metrics.
- Confirm `deploy/observability/grafana-dashboard.json` is imported into the
  target dashboard stack and `deploy/observability/prometheus-alerts.yml` is
  loaded into the target alerting stack.
- Confirm `ops/agent-soak.json` reports `"ok": true` after running through the
  deployed OpenClaw/Hermes runtime hooks against the release server.
- Confirm `ops/load-smoke.json` reports `"ok": true`, includes
  `concurrent-retain-recall`, `retain-p95`, `recall-p95` and
  `metrics-backlog`, and was run against the release server.
- Confirm `ops/ui-walkthrough.json` reports `"ok": true` and includes
  vault editable text, vault archive, conflict decision, model probe, reindex
  and metrics checks.
- Confirm `scripts/verify_release_evidence.py ./release-evidence.json` prints
  `release_evidence=PASS`.
- Confirm the signed manifest names the exact source commit, immutable image
  digest and deployment ID under release.
- Confirm every artifact SHA-256 and the manifest HMAC signature verify with an
  operator-held key no older than the configured release window.
- Confirm `ops/release-notes.json` contains the release changelog and rollback
  steps for redeploying the previous image/ref and restoring data if needed.
- Confirm `release-evidence.json` was generated by
  `scripts/generate_release_evidence_manifest.py` so it includes every required
  artifact.
- Confirm worker logs do not show repeated NATS/Qdrant connection failures.
- Confirm `/ready` fails after stopping each required worker role, recovers only
  after a fresh PostgreSQL heartbeat, and does not expose worker IDs, hostnames
  or process metadata.
- Confirm `ops/metrics-health.json` reports zero `uam_worker_unready`,
  `uam_worker_missing` and `uam_worker_stale` for the release tenant.
- When the embedding model or dimension changes, confirm
  `ops/vector-collection-migration.json` verifies the new collection count and
  the previous collection remains available for rollback.
- Confirm restore drill passes against the backup intended for rollback.
- Confirm `.env.production` is not staged.
- Confirm the release was merged through PR with green CI, not pushed directly
  to `main`.
- Confirm GitHub no longer reports `Bypassed rule violations` for `main`.

Do not release if:

- migrations fail on an existing volume;
- restore drill fails for the release backup;
- `benchmark_suite.py` reports any failed gate;
- production compose exposes internal infrastructure ports;
- production env validation fails or still contains placeholder secrets;
- non-local production exposes backend `6798` directly instead of HTTPS proxy;
- generated context contains rejected/archived/superseded memory as active truth.
- branch protection or PR-only merge policy is disabled for a shared production
  repository.
- OpenClaw/Hermes soak reports show cross-workspace leakage or missing recall.
- conversation pipeline evidence is missing, raw transcript appears in recall
  before curation, or curated memory cannot be recalled.
- embedding evidence is missing, dimension does not match the configured model,
  or semantic recall scenarios choose the wrong top memory.
- load smoke evidence is missing, has non-zero errors, violates p95 thresholds,
  or shows outbox dead letters/backlog after the run.
- OpenAI-compatible memory LLM regression returns invalid JSON or fails the
  explicit supersession-curation scenario.
- UI walkthrough evidence is missing, skipped model probing, or shows vector /
  embedding data in the vault editor.
- release notes evidence is missing, has an empty changelog, or lacks rollback
  instructions for the previous image/ref and restore procedure.
- release evidence is unsigned, stale, references a different commit/image/
  deployment, contains unsafe paths, or any artifact checksum differs.
- audit retention evidence is missing, unsigned, unverified, or produced after
  pruning rather than before pruning.
- observability dashboard/alert rules are not installed for the target
  production environment.
- any required worker role is missing or stale, or the production environment
  does not require all three roles: `outbox-relay`, `embedding-worker` and
  `maintenance-worker`.
