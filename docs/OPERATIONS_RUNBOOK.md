# Obelisk Memory operations runbook

## Start production

```bash
cp .env.production.example .env.production
docker compose -f docker-compose.prod.yml --env-file .env.production up -d --build
```

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

## Health checks

```bash
curl http://localhost:6798/health
curl -H "Authorization: Bearer $UAM_API_KEY" http://localhost:6798/metrics
UAM_API_KEY=... PYTHONPATH=src python scripts/check_metrics_health.py \
  --metrics-url http://localhost:6798/metrics \
  --report ./ops/metrics-health.json
docker compose -f docker-compose.prod.yml --env-file .env.production ps
```

Healthy production means:

- `memory-server` is healthy;
- `postgres` is healthy;
- `nats` is healthy;
- `outbox-relay` and `embedding-worker` are running;
- `/metrics` does not show growing pending/dead-letter backlogs.

`check_metrics_health.py` turns Prometheus text into an operator gate. It fails
when outbox pending, dead-letter, lag or in-flight values exceed configured
thresholds, writes a JSON report, and can post failed reports through
`UAM_METRICS_ALERT_WEBHOOK`. The `/metrics` endpoint also exposes embedding
operation count, failure count, last duration, cumulative duration and reindex
health so a deployment can alert when vector indexing degrades.

## Access keys

Use one master `UAM_API_KEY` for break-glass operations and scoped keys for
normal integrations:

```dotenv
UAM_API_KEYS=openclaw:...:agent,hermes:...:agent,operator:...:operator
```

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

## Signed vault bundles

Vault export/import is human-editable, so production operators should protect
the review boundary with a signed manifest:

```bash
UAM_VAULT_SIGNING_KEY=... PYTHONPATH=src python scripts/export_vault.py ./vault-review
UAM_VAULT_SIGNING_KEY=... PYTHONPATH=src python scripts/import_vault.py ./vault-review \
  --require-signature
```

The exporter writes:

- `.uam-vault-manifest.json` — every Markdown path, byte count and SHA-256;
- `.uam-vault-manifest.sha256` — checksum for the manifest;
- `.uam-vault-manifest.sig` — HMAC-SHA256 signature when the key is provided.

Use `--require-signature` for production imports, including dry-run planning,
and keep `UAM_VAULT_SIGNING_KEY` in the same class of secret storage as
`UAM_AUDIT_SIGNING_KEY`.

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
- reindex affected memories;
- confirm Qdrant collection dimension matches the new vectors;
- keep the old backup until semantic recall quality is verified.

Changing Qwen/Spark memory LLM is less risky because the API fails soft, but
curation/proposal quality may change. Run benchmark and manually review
proposal quality before trusting automatic curation.

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
