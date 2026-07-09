# Release checklist

Use this before tagging or pushing a production release.

```bash
ruff check src tests scripts agent-integrations
pytest -q
PYTHONPATH=src python scripts/production_readiness_eval.py
python scripts/restore_drill.py ./backups/obelisk-memory.dump
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
- Confirm worker logs do not show repeated NATS/Qdrant connection failures.
- Confirm restore drill passes against the backup intended for rollback.
- Confirm `.env.production` is not staged.
- Confirm the release was merged through PR with green CI, not pushed directly
  to `main`.

Do not release if:

- migrations fail on an existing volume;
- restore drill fails for the release backup;
- `benchmark_suite.py` reports any failed gate;
- production compose exposes internal infrastructure ports;
- generated context contains rejected/archived/superseded memory as active truth.
- branch protection or PR-only merge policy is disabled for a shared production
  repository.
