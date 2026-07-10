# Release checklist

Use this before tagging or pushing a production release.

```bash
ruff check src tests scripts agent-integrations
pytest -q
PYTHONPATH=src python scripts/production_readiness_eval.py
PYTHONPATH=src python scripts/scheduled_backup.py \
  --backup-dir ./backups \
  --audit-dir ./audit-export \
  --report ./backups/latest-backup-report.json
UAM_API_KEY=... PYTHONPATH=src python scripts/check_metrics_health.py \
  --metrics-url http://localhost:6798/metrics \
  --report ./ops/metrics-health.json
GITHUB_TOKEN=... python scripts/check_branch_protection.py \
  --repo Alex12571333/universal-agent-memory \
  --required-check python \
  --required-check web
docker compose --profile advanced config
docker compose -f docker-compose.prod.yml --env-file .env.production config
python scripts/benchmark_suite.py
python scripts/enterprise_readiness_check.py
```

Manual checks:

- Open `http://localhost:6798/ui` and verify dashboard, graph, vault, settings.
- Retain and recall a Russian and English memory.
- Verify conflict inbox can list and resolve at least one conflict.
- Export vault, edit a note, run dry-run import, then apply only after review.
- Confirm Qwen/Spark memory LLM endpoint is reachable.
- Confirm embedding endpoint returns the configured dimension.
- Confirm `audit-export/manifest.sha256` verifies before preserving release
  evidence.
- Confirm signed audit bundles verify with `scripts/export_audit.py --verify`.
- Confirm incident/audit exports use `--all-pages` for multi-day windows.
- Confirm `backups/latest-backup-report.json` reports `"ok": true`.
- Confirm `ops/metrics-health.json` reports `"ok": true`.
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
- generated context contains rejected/archived/superseded memory as active truth.
- branch protection or PR-only merge policy is disabled for a shared production
  repository.
