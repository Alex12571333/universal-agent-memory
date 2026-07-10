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
    "metrics_health": "ops/metrics-health.json",
    "scheduled_backup": "backups/latest-backup-report.json",
    "branch_protection": "ops/branch-protection.json"
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

UAM_API_KEY=... python scripts/agent_soak_eval.py \
  --base-url http://localhost:6798 \
  --rounds 5 \
  --parallel 4 \
  --json-report ./ops/agent-soak.json

python scripts/real_memory_llm_eval.py \
  --base-url http://192.168.0.10:8000/v1 \
  --model qwen3.6-35b-a3b \
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
- metrics health report format `obelisk-metrics-health-v1`, `ok: true`, outbox
  pending/dead-letter/lag and inflight checks;
- scheduled backup report format `obelisk-scheduled-backup-report-v1`,
  `ok: true`, restore drill not skipped and audit export not skipped;
- branch protection JSON with `passed: true`, PR requirement, required status
  checks, strict mode and admin enforcement.

Do not call a deployment full production if this verifier fails.
