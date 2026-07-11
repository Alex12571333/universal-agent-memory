# Signed release evidence bundle

Full-production release claims require preserved machine-readable evidence, not
only console output. Store release artifacts in a directory outside Docker
volumes, then seal and verify the bundle before tagging. The signed manifest
binds the reports to one source commit, immutable image digest and deployment.

## Release identity

Set release identity from the image that was actually deployed and tested:

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
export RELEASE_PUBLIC_URL='https://memory.example.com'
export UAM_RELEASE_SIGNING_KEY_FILE=/run/secrets/obelisk_release_signing_key
```

`RELEASE_API_URL` must be the exact URL used by agent soak, conversation,
load-smoke and UI walkthrough reports. `RELEASE_PUBLIC_URL` must be the HTTPS
URL recorded by deployment preflight. The deployed server's
`UAM_SOURCE_COMMIT`, `UAM_IMAGE_DIGEST` and `UAM_DEPLOYMENT_ID` must equal the
release identity above; the live runners read them back from
`/v1/system/status` and the verifier rejects mismatches or stale reports.

## Seal after all reports exist

Generate `release-evidence.json` only after every required report has been
written:

```bash
python scripts/generate_release_evidence_manifest.py \
  --release "$RELEASE_ID" \
  --source-commit "$SOURCE_COMMIT" \
  --image-digest "$IMAGE_DIGEST" \
  --deployment-id "$DEPLOYMENT_ID" \
  --api-url "$RELEASE_API_URL" \
  --public-url "$RELEASE_PUBLIC_URL" \
  --signing-key-id production-release-key-2026 \
  --output ./release-evidence.json
```

The v2 manifest contains identity, an SHA-256 digest for every report and an
HMAC-SHA256 signature over canonical manifest content:

```json
{
  "format": "obelisk-release-evidence-manifest-v2",
  "release": "2026.07.10",
  "generated_at": "2026-07-10T12:00:00Z",
  "source_commit": "0123456789abcdef0123456789abcdef01234567",
  "image_digest": "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "target": {
    "deployment_id": "production-primary",
    "api_url": "http://127.0.0.1:6798",
    "public_url": "https://memory.example.com"
  },
  "models": {
    "embedding": {
      "provider": "openai-compatible",
      "base_url": "https://embedding-gateway.example.com/v1",
      "model": "provider/embedding-model-id",
      "dimension": 1536
    },
    "memory_llm": {
      "provider": "openai-compatible",
      "base_url": "https://model-gateway.example.com/v1",
      "model": "provider/memory-model-id",
      "config_fingerprint": "<sha256-of-non-secret-generation-config>"
    }
  },
  "artifacts": {
    "agent_soak": {
      "path": "ops/agent-soak.json",
      "sha256": "<64-hex-sha256>"
    }
  },
  "signature": {
    "algorithm": "hmac-sha256",
    "key_id": "production-release-key-2026",
    "value": "<64-hex-hmac>"
  }
}
```

Every required artifact is included in the real manifest. Artifact paths must
be relative to the bundle directory; absolute paths and path traversal are
rejected. Keep the signing key in an external secret manager and preserve the
sealed directory in immutable storage.

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
```

## Verify before release

```bash
python scripts/verify_release_evidence.py ./release-evidence.json \
  --expected-source-commit "$SOURCE_COMMIT" \
  --expected-image-digest "$IMAGE_DIGEST" \
  --expected-deployment-id "$DEPLOYMENT_ID"
```

Expected result:

```text
release_evidence=PASS
```

The verifier requires:

- manifest format `obelisk-release-evidence-manifest-v2`, a non-empty release,
  valid generation time, source commit, OCI image digest and target identity;
- an operator-held HMAC-SHA256 key and a valid manifest signature;
- an exact required artifact set, safe relative paths and matching SHA-256 for
  every report;
- a manifest no older than 24 hours at release verification time. Use
  `--max-age-hours 0` only when verifying an archived historical bundle;
- release notes whose release and current commit match the signed manifest;
- agent soak, conversation pipeline, load smoke and UI walkthrough reports
  whose `base_url` matches the signed target API URL, plus deployment preflight
  whose public URL matches the signed target public URL;
- those four live reports must include timezone-aware `generated_at` plus one
  consistent runtime `build` identity whose source commit, image digest and
  deployment ID match the signed manifest; old reports cannot be re-sealed as a
  new release;
- embedding and memory-LLM reports must be fresh and match the provider, base
  URL, model, vector dimension and non-secret generation-config fingerprint
  copied into the signed manifest;

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
- scheduled backup report format `obelisk-scheduled-backup-report-v2`,
  `ok: true`, AES-256-GCM artifact, restore drill not skipped and audit export
  not skipped;
- restore/recovery report format `obelisk-restore-recovery-evidence-v1`,
  `ok: true`, successful restore, reindex, semantic recall and recovery-probe
  checks. Its bound probe input must use
  `obelisk-restored-reindex-probe-v1` with a non-empty embedding model and a
  positive embedding dimension;
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

Do not call a deployment full production if this verifier fails. A valid
signature proves bundle integrity and operator custody; it does not replace OCI
provenance or image signing. Those remain separate supply-chain release gates.
