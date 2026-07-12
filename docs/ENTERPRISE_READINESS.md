# Production readiness checklist

This checklist defines the current repository-level bar for Obelisk Memory.
Passing it means the repository contains the expected production-envelope
artifacts. It does not certify runtime correctness, a trusted pilot or a target
deployment. See
[PRODUCTION_GAP_AUDIT_2026_07_10.md](PRODUCTION_GAP_AUDIT_2026_07_10.md) for the
remaining hard requirements.

## Required before production

- [x] API/UI runs in Docker.
- [x] PostgreSQL is the source of truth.
- [x] Migrations are forward-only and run before API startup.
- [x] Production compose exposes only API/UI to the host.
- [x] Local-appliance deployment keeps data services inside Docker and exposes
      only the API/UI port to the trusted local network.
- [x] API key auth can protect API, docs, metrics, and UI.
- [x] Scoped API keys gate operator, agent, read, and write route classes.
- [x] React UI exchanges an operator key for an HttpOnly signed session, applies
      CSRF protection to mutations and invalidates sessions on key revocation.
- [x] Production can require every agent API principal to be bound to one
      tenant/workspace/agent identity; forged IDs and foreign threads are
      rejected and private recall is owner-filtered across all candidate layers.
- [x] Application secrets support `*_FILE` reads for API keys, model gateway
      keys, signing keys, database URLs and memory text encryption keys.
- [ ] Mount those secret files in the production topology and construct every
      database DSN from the file-backed application/admin passwords.
- [x] Secret-files preflight runner writes JSON evidence that production
      secrets are file-backed and raw secret env values are empty.
- [x] API-key registry tracks non-secret fingerprints, scopes, last-used time
      and revocation state.
- [x] Production env validator catches placeholders, weak secrets and local-only
      settings before deployment.
- [x] HTTP responses include baseline browser/API security headers.
- [x] Liveness and canonical readiness are separate; optional vector failures
      degrade recall to PostgreSQL and remain visible without failing readiness.
- [x] Durable audit log records memory writes, supersedes, conflict decisions,
      vault imports/archives and model-setting changes.
- [x] Audit export bundle writes JSONL, manifest, and SHA-256 checksum for
      incident review.
- [x] Audit export bundle can be HMAC-signed and verified with an operator-held
      signing key.
- [x] Audit export supports time-window pagination for long incident windows.
- [x] Audit retention runner exports and verifies old audit windows before
      pruning, writes JSON evidence, and requires signed exports for `--apply`.
- [x] Container liveness checks exist for API, PostgreSQL, and NATS.
- [x] API readiness actively checks PostgreSQL and reports per-source retrieval
      degradation without claiming liveness is readiness.
- [ ] Extend readiness evidence to NATS and deployed worker state.
- [x] Metrics endpoint exists.
- [x] Metrics health evaluator can fail on outbox lag/dead letters and emit JSON
      reports/webhook alerts.
- [x] Prometheus alert rules and Grafana dashboard templates exist for outbox,
      worker leases, embeddings, reindex and ledger growth.
- [x] Observability preflight runner writes JSON evidence that dashboard panels
      and alert rules cover required production metrics.
- [x] Embedding metric names and API-side counters exist.
- [ ] Expose metrics from the deployed embedding worker process and alert on its
      actual failures, latency and queue consumption.
- [x] Privacy guard redacts common secrets and high-risk PII.
- [x] Backup script exists.
- [x] Restore-drill script restores into an isolated PostgreSQL container,
      verifies schema presence, tenant RLS policies and source/restore row-count
      parity.
- [ ] Verify active-head recall and required Qdrant reindex after restore.
- [x] Scheduler-ready backup runner writes JSON reports and can alert on
      failures.
- [x] Ops schedule preflight runner writes JSON evidence for installed backup,
      audit-retention and metrics schedules, alert routing and durable artifact
      roots.
- [x] Vault export/import uses safe dry-run and CAS supersede.
- [x] Vault CLI export/import supports manifest checksums and HMAC signatures.
- [x] Operator UI can accept, override or dismiss conflict cases through the
      persisted conflict-review API.
- [x] Qdrant/vector indexing has an asynchronous worker path.
- [x] Isolate Qdrant startup/query failures so recall falls back to PostgreSQL;
      embedding workers fail fast for durable retry.
- [x] Workspace reindex preserves other workspaces and deletes stale scoped IDs
      only after replacement upserts succeed.
- [x] Collection startup enforces exact embedding model/dimension identity and a
      migration runner builds/verifies a separate rollback-safe collection.
- [ ] Add target multi-replica/crash and collection-migration release evidence.
- [x] PostgreSQL `memory_items.text` encryption can cover all memory rows or
      selected visibility scopes via `UAM_MEMORY_TEXT_ENCRYPTION_SCOPES`.
- [x] New provenance quotes, conversation content, proposals, checkpoint state
      and audit metadata are encrypted when pgcrypto is enabled; scheduled
      backup artifacts are encrypted too.
- [ ] Migrate legacy plaintext rows and protect the remaining JSON metadata
      fields before claiming full database encryption at rest.
- [x] Memory LLM is separate from embedding endpoint.
- [x] OpenAI-compatible embedding live regression runner exists and writes JSON
      release evidence for dimension and semantic recall checks.
- [x] OpenAI-compatible memory LLM defaults are documented.
- [x] OpenAI-compatible memory LLM live regression runner exists and writes JSON
      release evidence.
- [x] OpenClaw and Hermes integration guides exist.
- [x] OpenClaw/Hermes soak runner exists and writes JSON release evidence.
- [x] Conversation pipeline runner exists and writes JSON release evidence for
      raw transcript capture, explicit curation and recall.
- [x] Concurrent load smoke runner exists and writes JSON evidence for
      parallel retain/recall correctness, p95 latency and backlog health.
- [x] UI walkthrough runner exists and writes JSON evidence for served UI,
      conflict decision, vault editable text/archive, model probe, reindex and
      metrics.
- [x] Release evidence verifier checks saved agent, LLM, UI walkthrough,
      metrics, backup, signed vault import and branch-protection reports before
      a full-production claim.
- [x] Release evidence v2 binds reports to a source commit, immutable image
      digest and deployment; hashes every artifact, rejects unsafe paths,
      verifies freshness/target identity and requires an operator HMAC key.
- [x] Release evidence manifest generator keeps required artifact keys in sync
      with the verifier and seals only existing reports.
- [x] Release notes generator writes a versioned changelog and rollback
      instructions for release evidence.
- [x] CI workflow validates lint, tests, web build, and compose configs.
- [x] Enterprise readiness check script exists.
- [x] Branch-protection verifier exists for the `main` release gate.
- [x] Production gap audit exists and forbids over-claiming readiness.

## Required before calling it full production

- [x] Fresh-production smoke boots an isolated Compose project with generated
      secret files, verifies `/ready` and application-role PostgreSQL access,
      then removes all temporary resources.
- [ ] Preserve target PostgreSQL evidence for operator-provisioned agent/thread
      retain and concurrent checkpoint-CAS tests. The provisioning endpoint and
      optional integration test exist; live target proof remains required.
- [ ] Preserve target evidence for atomic accepted/overridden conflict winner
      revisions, multi-root stale-CAS rollback and Qdrant precedence. The
      implementation and local PostgreSQL/Qdrant live coverage exist.
- [ ] Implement retention-policy semantics and route LLM-derived durable memory
      through evidence-linked proposal/review with atomic acceptance.
- [ ] Complete authenticated browser UI flow and model-endpoint egress/SSRF
      controls with secret-safe durable settings.
- [ ] Verify the target bind address permits access only from the local host or
      trusted LAN, and preserve the deployment configuration output.
- [ ] Run secret-files preflight against the target environment, verify
      production secrets are mounted through `*_FILE` paths instead of raw env
      values, and preserve the generated report.
- [ ] Run ops schedule preflight against the target environment, verify backup,
      audit-retention and metrics schedules, alert routing and durable artifact
      roots, and preserve the generated report.
- [ ] Run observability preflight against the target monitoring artifacts,
      verify dashboard/alert coverage, and preserve the generated report.
- [ ] Generate release notes with rollback instructions and preserve
      `ops/release-notes.json` before tagging.
- [x] Require signed, unchanged vault bundles for release integrity evidence.
- [ ] Add an operator re-sign workflow before treating human-edited vault notes
      as signed production imports.
- [x] Enforce GitHub branch protection and PR-only merges to `main`.
- [ ] Run real OpenClaw/Hermes soak tests through the deployed native runtime
      versions and preserve the generated report.
- [ ] Run conversation pipeline tests against the target release server and
      preserve `ops/conversation-pipeline.json`.
- [ ] Run load smoke tests against the target release server and preserve the
      generated report.
- [ ] Run live embedding and memory LLM regression tests against the configured
      production endpoints and preserve `ops/embedding.json` and
      `ops/memory-llm.json`.
- [ ] Generate and verify the preserved release evidence manifest before
      tagging.
- [ ] Preserve `ops/ui-walkthrough.json` from the target deployment.

## Production interpretation

The target is a self-hosted, operator-owned memory appliance: private by default,
observable, recoverable, and usable through native agent plugins. Current code
remains an engineering preview until the runtime P0 blockers in the production
audit and every unchecked target-environment item above are resolved.
