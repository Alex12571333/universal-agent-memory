# Obelisk Memory operations runbook

> **Engineering preview:** the production-shaped Compose topology is available,
> but a fresh production rollout is currently blocked by the P0 runtime issues in
> [PRODUCTION_GAP_AUDIT_2026_07_10.md](PRODUCTION_GAP_AUDIT_2026_07_10.md),
> including vector collection migration, encryption breadth and authenticated UI. Database-role,
> operator identity provisioning and principal binding exist but need target evidence. The
> commands below are reference operating procedures; do not use
> them to claim or run a production deployment until those blockers are resolved
> and the target release gates pass.

## Start the reference topology

```bash
cp .env.production.example .env.production
install -d -m 0700 /srv/obelisk-secrets
openssl rand -base64 48 > /srv/obelisk-secrets/postgres_password
openssl rand -base64 48 > /srv/obelisk-secrets/app_db_password
chmod 0600 /srv/obelisk-secrets/postgres_password /srv/obelisk-secrets/app_db_password
# Set the two absolute host paths in .env.production before validation/start.
docker compose -f docker-compose.prod.yml --env-file .env.production up -d --build
```

The migration job creates or rotates `UAM_APP_DB_USER` with the mounted
`UAM_APP_DB_PASSWORD_FILE` value and grants runtime privileges after schema
migrations. It rejects reserved, malformed, or administrator role names. Never
reuse the PostgreSQL administrator identity as the application role.

Only API/UI port `6798` is exposed. PostgreSQL, Qdrant, NATS, and MinIO remain
inside the Docker network.

For any host reachable from another machine, start with the TLS proxy overlay
instead:

```bash
docker compose \
  -f docker-compose.prod.yml \
  -f deploy/reverse-proxy/docker-compose.caddy.yml \
  --env-file .env.production \
  up -d --build
```

Set `UAM_PUBLIC_HOST` and `UAM_PUBLIC_EMAIL` in `.env.production`, then verify
external clients use `https://$UAM_PUBLIC_HOST`. See
[TLS_REVERSE_PROXY.md](TLS_REVERSE_PROXY.md) before exposing the service outside
localhost/VPN.

Before starting a real production deployment, validate that the environment no
longer contains placeholders or local-only defaults:

```bash
python scripts/validate_production_env.py .env.production \
  --require-public-tls \
  --require-signed-artifacts \
  --require-real-embeddings
```

The example `.env.production.example` is expected to fail this strict check; it
contains placeholders on purpose.

## Health checks

```bash
curl http://localhost:6798/health
curl http://localhost:6798/ready
curl -H "Authorization: Bearer $UAM_API_KEY" http://localhost:6798/metrics
UAM_API_KEY=... PYTHONPATH=src python scripts/check_metrics_health.py \
  --metrics-url http://localhost:6798/metrics \
  --report ./ops/metrics-health.json
docker compose -f docker-compose.prod.yml --env-file .env.production ps
```

Healthy production means:

- `memory-server` is healthy;
- `UAM_MEMORY_TEXT_ENCRYPTION=pgcrypto` is enabled and its key is delivered by
  the deployment secret manager;
- `UAM_MEMORY_TEXT_ENCRYPTION_SCOPES=all` unless the deployment has an explicit
  row-level policy such as `private,thread`;
- `UAM_QDRANT_PAYLOAD_TEXT=false`, so Qdrant stores vectors/filter metadata and
  recall hydrates memory text from PostgreSQL;
- `postgres` is healthy;
- `nats` is healthy;
- `outbox-relay` and `embedding-worker` are running;
- `/metrics` does not show growing pending/dead-letter backlogs.

`check_metrics_health.py` turns Prometheus text into an operator gate. It fails
when outbox pending, dead-letter, lag or in-flight values exceed configured
thresholds, writes a JSON report, and can post failed reports through
`UAM_METRICS_ALERT_WEBHOOK`. The API `/metrics` surface contains embedding
counter names, but the deployed embedding worker is a separate process and does
not yet export its own metrics endpoint. Do not treat API-side counters as proof
of worker health; worker metrics are an open production blocker.

Import `deploy/observability/grafana-dashboard.json` and
`deploy/observability/prometheus-alerts.yml` into the target monitoring stack.
See [OBSERVABILITY.md](OBSERVABILITY.md) for scrape config, dashboard coverage
and alert rule details.

Preserve observability installation evidence before a full-production release:

```bash
PYTHONPATH=src python scripts/observability_preflight.py \
  --grafana-dashboard ./deploy/observability/grafana-dashboard.json \
  --prometheus-alerts ./deploy/observability/prometheus-alerts.yml \
  --report ./ops/observability-preflight.json
```

The report uses format `obelisk-observability-preflight-v1` and verifies that
dashboard panels and alert rules cover required production metrics.

## Deployment boundary preflight

For any non-local deployment, preserve a preflight report that proves the public
entrypoint is HTTPS and the direct backend port is not externally reachable from
the probe location:

```bash
UAM_API_KEY=... PYTHONPATH=src python scripts/deployment_preflight.py \
  --public-url https://$UAM_PUBLIC_HOST \
  --backend-url http://$UAM_PUBLIC_HOST:6798 \
  --report ./ops/deployment-preflight.json
```

The report uses format `obelisk-deployment-preflight-v1` and is required by
`scripts/verify_release_evidence.py` before a full-production release claim.

## Access keys

Use one master `UAM_API_KEY` for break-glass operations and scoped keys for
normal integrations:

```dotenv
UAM_API_KEYS=openclaw:...:agent,hermes:...:agent,operator:...:operator
UAM_API_PRINCIPAL_BINDINGS_JSON={"openclaw":{"tenant_id":"<server-uuid>","workspace_id":"<project-uuid>","agent_id":"<openclaw-uuid>"},"hermes":{"tenant_id":"<server-uuid>","workspace_id":"<project-uuid>","agent_id":"<hermes-uuid>"}}
UAM_REQUIRE_IDENTITY_BINDINGS=true
```

Provision the referenced agent and thread UUIDs with the operator key before
starting native integrations. Prefer
`UAM_API_PRINCIPAL_BINDINGS_JSON_FILE=/run/secrets/uam_principal_bindings` when
deployment configuration is mounted from files. Although the binding document
contains IDs rather than credentials, mounting it keeps identity policy changes
auditable and avoids shell-quoting mistakes.

For production, verify that required secrets are mounted from the secret manager
through `*_FILE` paths and that raw secret env values are empty:

```bash
PYTHONPATH=src python scripts/secret_files_preflight.py .env.production \
  --report ./ops/secret-files.json
```

The report uses format `obelisk-secret-files-preflight-v1` and is required by
`scripts/verify_release_evidence.py` before a full-production release claim.

Rotate an agent key by replacing its secret in `.env.production` and restarting
`memory-server`. If a key leaked, rotate first, then inspect memories retained by
that agent for accidental secret capture.

The server stores non-secret key metadata in the API-key registry. Operators can
inspect last-used timestamps and revoke a fingerprint before replacing the env
secret:

```bash
curl -H "Authorization: Bearer $UAM_API_KEY" http://localhost:6798/v1/keys
curl -X POST -H "Authorization: Bearer $UAM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"reason":"rotation drill"}' \
  http://localhost:6798/v1/keys/<key_id>/revoke
```

Revocation is immediate for future requests; env replacement and restart make
the rotation permanent.

## Backup

```bash
docker compose -f docker-compose.prod.yml --env-file .env.production --profile ops run --rm backup
```

Move `./backups/obelisk-memory.dump` to durable storage outside Docker volumes.
Before a destructive maintenance task, make two backups and test restore on a
separate stack.

Run a non-destructive restore drill after creating a backup and before upgrades:

```bash
python scripts/restore_drill.py ./backups/obelisk-memory.dump
```

The drill creates a temporary PostgreSQL Docker container and volume, restores
the dump inside it, verifies required production tables, then removes the
temporary resources. Use `--keep` only when you need manual forensic inspection.

## Scheduled backup job

For unattended operations, run the scheduler-ready backup wrapper from cron,
systemd timer, launchd, or your orchestrator:

```bash
UAM_BACKUP_DATABASE_URL=postgresql://... \
UAM_BACKUP_ALERT_WEBHOOK=https://alerts.example/obelisk-backup \
PYTHONPATH=src python scripts/scheduled_backup.py \
  --backup-dir ./backups \
  --audit-dir ./audit-exports \
  --report ./backups/latest-backup-report.json
```

The job:

- creates a timestamped PostgreSQL dump;
- runs the isolated restore drill against that dump;
- exports a recent audit bundle;
- writes a JSON report with every step and return code;
- posts the report to `UAM_BACKUP_ALERT_WEBHOOK` when any required step fails.

Production deployments should run this on a fixed schedule and ship
`latest-backup-report.json`, backup dumps, and audit bundles to durable storage
outside the Docker host. The repository provides the runner and alert hook; the
actual cron/systemd/orchestrator schedule is an environment-level control.

Preserve machine-readable schedule evidence before a full-production release:

```bash
PYTHONPATH=src python scripts/ops_schedule_preflight.py .env.production \
  --backup-schedule-file ./deploy/schedules/backup.timer \
  --audit-retention-schedule-file ./deploy/schedules/audit-retention.timer \
  --metrics-schedule-file ./deploy/schedules/metrics-health.timer \
  --backup-artifact-root s3://obelisk-memory/backups \
  --audit-artifact-root s3://obelisk-memory/audit \
  --report ./ops/ops-schedule.json
```

The report uses format `obelisk-ops-schedule-preflight-v1`. It verifies that
backup, audit-retention and metrics-health schedules are installed, alert
routes are configured, and backup/audit artifact roots use an approved durable
storage prefix.

## Audit export

Export recent operator/agent audit events before upgrades, incident response, or
security review:

```bash
PYTHONPATH=src python scripts/export_audit.py ./audit-export \
  --tenant-id "$UAM_SERVER_ID" \
  --workspace-id "$UAM_PROJECT_ID" \
  --since 2026-07-01T00:00:00Z \
  --until 2026-07-11T00:00:00Z \
  --all-pages \
  --signing-key "$UAM_AUDIT_SIGNING_KEY" \
  --batch-size 500
```

The bundle contains:

- `audit-events.jsonl` — newline-delimited audit events;
- `manifest.json` — filters, event count, created-at range, file checksum;
- `manifest.sha256` — checksum for `manifest.json`.
- `manifest.sig` — HMAC-SHA256 signature when `UAM_AUDIT_SIGNING_KEY` or
  `--signing-key` is set.

Verify the bundle before relying on it:

```bash
cd audit-export
shasum -a 256 -c manifest.sha256
shasum -a 256 audit-events.jsonl
PYTHONPATH=src python ../scripts/export_audit.py . --verify \
  --signing-key "$UAM_AUDIT_SIGNING_KEY"
```

The current export is intentionally bounded to the recent filtered audit window
unless `--all-pages` is supplied. For regulated retention, protect
`UAM_AUDIT_SIGNING_KEY` in an external secret manager, run scheduled range
exports, and store bundles in immutable storage.

## Audit retention

Audit pruning is intentionally a two-step controlled workflow: export and verify
first, prune only when explicitly applying the retention policy.

Dry-run the retention job:

```bash
UAM_AUDIT_SIGNING_KEY=... PYTHONPATH=src python scripts/audit_retention.py \
  --database-url "$UAM_DATABASE_URL" \
  --tenant-id "$UAM_SERVER_ID" \
  --workspace-id "$UAM_PROJECT_ID" \
  --retain-days 365 \
  --export-root ./audit-retention \
  --json-report ./ops/audit-retention.json
```

Apply only after the exported bundle has been shipped to durable/immutable
storage:

```bash
UAM_AUDIT_SIGNING_KEY=... PYTHONPATH=src python scripts/audit_retention.py \
  --database-url "$UAM_DATABASE_URL" \
  --tenant-id "$UAM_SERVER_ID" \
  --workspace-id "$UAM_PROJECT_ID" \
  --retain-days 365 \
  --export-root ./audit-retention \
  --json-report ./ops/audit-retention.json \
  --apply
```

`--apply` requires a signing key unless `--allow-unsigned-export` is explicitly
passed. Production deployments should not use unsigned retention exports. The
JSON report uses format `obelisk-audit-retention-v1` and records cutoff,
exported event count, signature/verification status and pruned row count.

## Signed vault bundles

Production operators can protect an unchanged vault export with a signed
manifest:

```bash
UAM_VAULT_SIGNING_KEY=... PYTHONPATH=src python scripts/export_vault.py ./vault-review
UAM_VAULT_SIGNING_KEY=... PYTHONPATH=src python scripts/import_vault.py ./vault-review \
  --require-signature \
  --json-report ./ops/vault-import.json
```

The exporter writes:

- `.uam-vault-manifest.json` — every Markdown path, byte count and SHA-256;
- `.uam-vault-manifest.sha256` — checksum for the manifest;
- `.uam-vault-manifest.sig` — HMAC-SHA256 signature when the key is provided.

Use `--require-signature` for production imports, including dry-run planning,
and keep `UAM_VAULT_SIGNING_KEY` in the same class of secret storage as
`UAM_AUDIT_SIGNING_KEY`. Preserve the `obelisk-vault-import-report-v1` JSON
report as release evidence; full-production release verification requires it to
show a verified signed manifest and `require_signature: true`.

The signature covers every Markdown file. Editing a signed export invalidates
the manifest, and the current CLI does not provide a review-and-re-sign command.
Therefore the signed flow above is an integrity/release-evidence check, not yet a
production workflow for applying human edits. See [VAULT.md](VAULT.md) for the
manifest-free trusted-environment edit path and the remaining production gap.

## Upgrade

1. Pull/build the new image.
2. Run config validation:

   ```bash
   docker compose -f docker-compose.prod.yml --env-file .env.production config
   ```

3. Run `PYTHONPATH=src python scripts/scheduled_backup.py --backup-dir ./backups --audit-dir ./audit-export`.
4. Confirm `./backups/latest-backup-report.json` has `"ok": true`.
5. Start the stack; migrations run through the one-shot `migrate` service.
6. Run:

   ```bash
   python scripts/benchmark_suite.py
   python scripts/enterprise_readiness_check.py
   ```

7. Review `/metrics` and worker logs.

## Model changes

Changing embedding model or dimension is a schema-level operational event:

- update `UAM_EMBEDDING_MODEL`, `UAM_EMBEDDING_DIM`, and endpoint together;
- choose a new immutable `UAM_QDRANT_COLLECTION` name;
- run `scripts/migrate_vector_collection.py` and preserve its verified report;
- switch API and embedding worker to the new collection in one deployment;
- keep the old collection unchanged until semantic recall and rollback gates pass.

Follow [VECTOR_COLLECTION_MIGRATION.md](VECTOR_COLLECTION_MIGRATION.md). Startup
rejects a collection whose stored model or dimension differs from runtime
configuration.

Changing the OpenAI-compatible memory LLM endpoint/model is less risky than
changing embedding dimensions because the API fails soft, but curation/proposal
quality may change. Run benchmark and manually review proposal quality before
trusting automatic curation.

## Incident response

If memory contents may have leaked:

1. Remove network exposure immediately.
2. Rotate `UAM_API_KEY` and all downstream credentials that may have appeared in
   memory/tool logs.
3. Export audit/vault evidence for review and preserve `manifest.sha256`.
4. Search for leaked secret patterns and reject/supersede affected memories.
5. Re-enable access only after reverse proxy and auth are verified.

If embeddings/LLM are down:

- keep API running;
- disable the failing provider or switch to `fake` only for emergency local
  operation;
- expect lower recall quality until reindexing catches up.
