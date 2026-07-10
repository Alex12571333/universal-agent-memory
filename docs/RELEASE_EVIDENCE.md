# Release evidence manifest

Full-production release claims require preserved machine-readable evidence, not
only console output. Store release artifacts in a directory outside Docker
volumes, then verify the manifest before tagging.

## Required manifest

Generate `release-evidence.json` next to the referenced reports:

```bash
python scripts/generate_release_evidence_manifest.py \
  --release 2026.07.10 \
  --output ./release-evidence.json
```

The generated manifest contains every artifact currently required by
`scripts/verify_release_evidence.py`:

```json
{
  "format": "obelisk-release-evidence-manifest-v1",
  "release": "2026.07.10",
  "artifacts": {
    "agent_soak": "ops/agent-soak.json",
    "conversation_pipeline": "ops/conversation-pipeline.json",
    "embedding": "ops/embedding.json",
    "memory_llm": "ops/memory-llm.json",
    "load_smoke": "ops/load-smoke.json",
    "metrics_health": "ops/metrics-health.json",
    "ops_schedule": "ops/ops-schedule.json",
    "observability": "ops/observability-preflight.json",
    "release_notes": "ops/release-notes.json",
    "scheduled_backup": "backups/latest-backup-report.json",
    "audit_retention": "ops/audit-retention.json",
    "deployment_preflight": "ops/deployment-preflight.json",
    "secret_files": "ops/secret-files.json",
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

UAM_AUDIT_SIGNING_KEY=... PYTHONPATH=src python scripts/audit_retention.py \
  --database-url "$UAM_DATABASE_URL" \
  --retain-days 365 \
  --export-root ./audit-retention \
  --json-report ./ops/audit-retention.json

UAM_API_KEY=... PYTHONPATH=src python scripts/deployment_preflight.py \
  --public-url https://memory.example.com \
  --backend-url http://memory.example.com:6798 \
  --report ./ops/deployment-preflight.json

PYTHONPATH=src python scripts/secret_files_preflight.py .env.production \
  --report ./ops/secret-files.json

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

UAM_API_KEY=... python scripts/conversation_pipeline_eval.py \
  --base-url http://localhost:6798 \
  --json-report ./ops/conversation-pipeline.json

python scripts/real_embedding_eval.py \
  --provider openai-compatible \
  --base-url https://api.openai.com/v1 \
  --model text-embedding-3-large \
  --dimension 3072 \
  --json-report ./ops/embedding.json

python scripts/real_memory_llm_eval.py \
  --base-url https://api.openai.com/v1 \
  --model gpt-5.6-terra \
  --json-report ./ops/memory-llm.json

GITHUB_TOKEN=... python scripts/check_branch_protection.py \
  --repo Alex12571333/universal-agent-memory \
  --required-check python \
  --required-check web \
  --json > ./ops/branch-protection.json

python scripts/generate_release_notes.py \
  --release 2026.07.10 \
  --previous-ref v2026.07.09 \
  --current-ref HEAD \
  --evidence-manifest ./release-evidence.json \
  --output ./ops/release-notes.json
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
- conversation pipeline report format `obelisk-conversation-pipeline-v1`,
  `ok: true`, raw turn capture/listing, proof that raw turns are not recalled
  before curation, explicit curation and curated memory recall;
- embedding report format `obelisk-embedding-eval-v1`, `ok: true`, endpoint
  reachability, exact vector dimension, and all semantic recall scenarios;
- memory LLM report format `obelisk-memory-llm-eval-v1`, `ok: true`,
  chat-completions and JSON curation checks;
- load smoke report format `obelisk-load-smoke-v1`, `ok: true`, parallel
  retain/recall correctness, error-rate, p95 latency and metrics-backlog checks;
- metrics health report format `obelisk-metrics-health-v1`, `ok: true`, outbox
  pending/dead-letter/lag and inflight checks;
- ops schedule report format `obelisk-ops-schedule-preflight-v1`, `ok: true`,
  installed backup/audit-retention/metrics schedule evidence, alert routing and
  durable artifact roots;
- observability report format `obelisk-observability-preflight-v1`, `ok: true`,
  Grafana dashboard coverage and Prometheus alert rules for required production
  metrics/failure modes;
- release notes report format `obelisk-release-notes-v1`, `ok: true`, a
  non-empty versioned changelog, and rollback instructions that name the
  previous ref/image and restore procedure;
- scheduled backup report format `obelisk-scheduled-backup-report-v1`,
  `ok: true`, restore drill not skipped and audit export not skipped;
- audit retention report format `obelisk-audit-retention-v1`, `ok: true`,
  signed pre-prune export and verified export;
- deployment preflight report format `obelisk-deployment-preflight-v1`,
  `ok: true`, public HTTPS health/security-header checks, and evidence that the
  direct backend URL probe was performed and was not publicly reachable;
- secret-files preflight report format `obelisk-secret-files-preflight-v1`,
  `ok: true`, raw secret env values empty, required `*_FILE` paths configured,
  readable, non-empty, and under the approved mounted secret prefix;
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
