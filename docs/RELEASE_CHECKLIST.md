# Release checklist

Use this before tagging or pushing a production release.

```bash
ruff check src tests scripts agent-integrations
pytest -q
PYTHONPATH=src python scripts/production_readiness_eval.py
python scripts/validate_production_env.py .env.production \
  --require-public-tls \
  --require-signed-artifacts \
  --require-real-embeddings
PYTHONPATH=src python scripts/scheduled_backup.py \
  --backup-dir ./backups \
  --audit-dir ./audit-export \
  --report ./backups/latest-backup-report.json
UAM_VAULT_SIGNING_KEY=... python scripts/export_vault.py ./vault-release
UAM_VAULT_SIGNING_KEY=... python scripts/import_vault.py ./vault-release \
  --require-signature
UAM_API_KEY=... PYTHONPATH=src python scripts/check_metrics_health.py \
  --metrics-url http://localhost:6798/metrics \
  --report ./ops/metrics-health.json
UAM_API_KEY=... python scripts/agent_soak_eval.py \
  --base-url http://localhost:6798 \
  --rounds 5 \
  --parallel 4 \
  --json-report ./ops/agent-soak.json
UAM_API_KEY=... python scripts/ui_walkthrough_eval.py \
  --base-url http://localhost:6798 \
  --json-report ./ops/ui-walkthrough.json
python scripts/real_memory_llm_eval.py \
  --base-url https://api.openai.com/v1 \
  --model gpt-5.6-terra \
  --json-report ./ops/memory-llm.json
GITHUB_TOKEN=... python scripts/check_branch_protection.py \
  --repo Alex12571333/universal-agent-memory \
  --required-check python \
  --required-check web \
  --json > ./ops/branch-protection.json
python scripts/verify_release_evidence.py ./release-evidence.json
docker compose --profile advanced config
docker compose -f docker-compose.prod.yml --env-file .env.production config
docker compose \
  -f docker-compose.prod.yml \
  -f deploy/reverse-proxy/docker-compose.caddy.yml \
  --env-file .env.production \
  config
python scripts/benchmark_suite.py
python scripts/enterprise_readiness_check.py
```

Manual checks:

- Open `http://localhost:6798/ui` and verify dashboard, graph, vault, settings.
- Confirm `scripts/validate_production_env.py .env.production` passes with
  strict production flags.
- Retain and recall a Russian and English memory.
- Verify conflict inbox can list and resolve at least one conflict.
- Export vault, edit a note, run dry-run import, then apply only after review.
- Confirm vault imports use `--require-signature` for release/operator bundles.
- Confirm the configured OpenAI-compatible memory LLM endpoint is reachable.
- Confirm `ops/memory-llm.json` reports `"ok": true` for that endpoint/model.
- Confirm embedding endpoint returns the configured dimension.
- Confirm `UAM_QDRANT_PAYLOAD_TEXT=false` so Qdrant stores vectors/filter
  metadata only and memory text is hydrated from PostgreSQL.
- Confirm `UAM_MEMORY_TEXT_ENCRYPTION=pgcrypto` and
  `UAM_MEMORY_TEXT_ENCRYPTION_KEY` are supplied from a secret manager, not from
  the repository.
- Confirm non-local deployments use HTTPS through the reverse proxy and direct
  backend port `6798` is localhost-only or blocked by firewall/security group.
- Confirm `audit-export/manifest.sha256` verifies before preserving release
  evidence.
- Confirm signed audit bundles verify with `scripts/export_audit.py --verify`.
- Confirm incident/audit exports use `--all-pages` for multi-day windows.
- Confirm `backups/latest-backup-report.json` reports `"ok": true`.
- Confirm `ops/metrics-health.json` reports `"ok": true`.
- Confirm `ops/agent-soak.json` reports `"ok": true` after running against the
  same server and `.14` OpenClaw/Hermes hosts used for production.
- Confirm `ops/ui-walkthrough.json` reports `"ok": true` and includes
  vault editable text, vault archive, conflict decision, model probe, reindex
  and metrics checks.
- Confirm `scripts/verify_release_evidence.py ./release-evidence.json` prints
  `release_evidence=PASS`.
- Confirm worker logs do not show repeated NATS/Qdrant connection failures.
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
- OpenAI-compatible memory LLM regression returns invalid JSON or keeps obsolete
  memory as current truth.
- UI walkthrough evidence is missing, skipped model probing, or shows vector /
  embedding data in the vault editor.
