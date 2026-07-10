# Release evidence manifest

Full-production release claims require preserved machine-readable evidence, not
only console output. Store release artifacts in a directory outside Docker
volumes, then verify the manifest before tagging.

## Required manifest

Create `release-evidence.json` next to the referenced reports:

```json
{
  "format": "obelisk-release-evidence-manifest-v1",
  "release": "2026.07.10",
  "artifacts": {
    "agent_soak": "ops/agent-soak.json",
    "memory_llm": "ops/memory-llm.json",
    "load_smoke": "ops/load-smoke.json",
    "metrics_health": "ops/metrics-health.json",
    "scheduled_backup": "backups/latest-backup-report.json",
    "audit_retention": "ops/audit-retention.json",
    "vault_import": "ops/vault-import.json",
    "branch_protection": "ops/branch-protection.json",
    "ui_walkthrough": "ops/ui-walkthrough.json"
  }
}
```

Paths are resolved relative to the manifest location unless absolute.

## Generate the reports

```bash
UAM_API_KEY=... PYTHONPATH=src python scripts/check_metrics_health.py \
  --metrics-url http://localhost:6798/metrics \
  --report ./ops/metrics-health.json

PYTHONPATH=src python scripts/scheduled_backup.py \
  --backup-dir ./backups \
  --audit-dir ./audit-export \
  --report ./backups/latest-backup-report.json

UAM_AUDIT_SIGNING_KEY=... PYTHONPATH=src python scripts/audit_retention.py \
  --database-url "$UAM_DATABASE_URL" \
  --retain-days 365 \
  --export-root ./audit-retention \
  --json-report ./ops/audit-retention.json

UAM_VAULT_SIGNING_KEY=... PYTHONPATH=src python scripts/export_vault.py ./vault-review
UAM_VAULT_SIGNING_KEY=... PYTHONPATH=src python scripts/import_vault.py ./vault-review \
  --require-signature \
  --json-report ./ops/vault-import.json

UAM_API_KEY=... python scripts/agent_soak_eval.py \
  --base-url http://localhost:6798 \
  --rounds 5 \
  --parallel 4 \
  --json-report ./ops/agent-soak.json

UAM_API_KEY=... python scripts/load_smoke_eval.py \
  --base-url http://localhost:6798 \
  --agents 8 \
  --operations-per-agent 5 \
  --json-report ./ops/load-smoke.json

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
```

## Verify before release

```bash
python scripts/verify_release_evidence.py ./release-evidence.json
```

Expected result:

```text
release_evidence=PASS
```

The verifier requires:

- agent soak report format `obelisk-agent-soak-v1`, `ok: true`, OpenClaw recall,
  Hermes recall and cross-workspace leakage checks;
- memory LLM report format `obelisk-memory-llm-eval-v1`, `ok: true`,
  chat-completions and JSON curation checks;
- load smoke report format `obelisk-load-smoke-v1`, `ok: true`, parallel
  retain/recall correctness, error-rate, p95 latency and metrics-backlog checks;
- metrics health report format `obelisk-metrics-health-v1`, `ok: true`, outbox
  pending/dead-letter/lag and inflight checks;
- scheduled backup report format `obelisk-scheduled-backup-report-v1`,
  `ok: true`, restore drill not skipped and audit export not skipped;
- audit retention report format `obelisk-audit-retention-v1`, `ok: true`,
  signed pre-prune export and verified export;
- vault import report format `obelisk-vault-import-report-v1`, `ok: true`,
  `require_signature: true`, and a verified signed manifest before import
  planning or apply;
- branch protection JSON with `passed: true`, PR requirement, required status
  checks, strict mode and admin enforcement;
- UI walkthrough report format `obelisk-ui-walkthrough-v1`, `ok: true`,
  served UI, retain/recall, conflict decision, vault editable text,
  vault archive, model settings probe, reindex and metrics checks. The model
  settings probe must run; skipped probes are not accepted for release evidence.

Do not call a deployment full production if this verifier fails.
