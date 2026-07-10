# Production readiness checklist

This checklist defines the current repository-level bar for Obelisk Memory.
Passing it means the project is suitable for a trusted self-hosted pilot. It does
not mean the deployment has passed every full-production gate. See
[PRODUCTION_GAP_AUDIT_2026_07_10.md](PRODUCTION_GAP_AUDIT_2026_07_10.md) for the
remaining hard requirements.

## Required before production

- [x] API/UI runs in Docker.
- [x] PostgreSQL is the source of truth.
- [x] Migrations are forward-only and run before API startup.
- [x] Production compose exposes only API/UI to the host.
- [x] TLS reverse-proxy deployment example exists for non-local access.
- [x] API key auth can protect API, docs, metrics, and UI.
- [x] Scoped API keys can separate operator, agent, read, and write access.
- [x] API-key registry tracks non-secret fingerprints, scopes, last-used time
      and revocation state.
- [x] Production env validator catches placeholders, weak secrets and local-only
      settings before deployment.
- [x] HTTP responses include baseline browser/API security headers.
- [x] Durable audit log records memory writes, supersedes, conflict decisions,
      vault imports/archives and model-setting changes.
- [x] Audit export bundle writes JSONL, manifest, and SHA-256 checksum for
      incident review.
- [x] Audit export bundle can be HMAC-signed and verified with an operator-held
      signing key.
- [x] Audit export supports time-window pagination for long incident windows.
- [x] Health checks exist for API, PostgreSQL, and NATS.
- [x] Metrics endpoint exists.
- [x] Metrics health evaluator can fail on outbox lag/dead letters and emit JSON
      reports/webhook alerts.
- [x] Embedding service exposes operation, failure, latency and reindex metrics.
- [x] Privacy guard redacts common secrets and high-risk PII.
- [x] Backup script exists.
- [x] Restore-drill script verifies backups in an isolated PostgreSQL container.
- [x] Scheduler-ready backup runner writes JSON reports and can alert on
      failures.
- [x] Vault export/import uses safe dry-run and CAS supersede.
- [x] Vault CLI export/import supports manifest checksums and HMAC signatures.
- [x] Qdrant/vector indexing is async and fail-soft.
- [x] Memory LLM is separate from embedding endpoint.
- [x] Qwen/Spark `.10` memory LLM defaults are documented.
- [x] Qwen/Spark memory LLM live regression runner exists and writes JSON
      release evidence.
- [x] OpenClaw and Hermes integration guides exist.
- [x] OpenClaw/Hermes soak runner exists and writes JSON release evidence.
- [x] CI workflow validates lint, tests, web build, and compose configs.
- [x] Enterprise readiness check script exists.
- [x] Branch-protection verifier exists for the `main` release gate.
- [x] Production gap audit exists and forbids over-claiming readiness.

## Required before calling it full production

- [ ] Put the deployed API behind the real TLS reverse proxy/VPN boundary and
      verify direct backend `6798` is not externally reachable.
- [ ] Move bearer secrets to an external secret manager for larger deployments.
- [ ] Add audit retention schedule, immutable storage, and external signing-key
      custody for regulated environments.
- [ ] Install environment-level backup schedule and immutable artifact storage.
- [ ] Wire metrics and scheduled-backup reports into deployment dashboards/alerts.
- [ ] Add optional row-level encryption for selected memory scopes.
- [ ] Enforce signed vault import manifests in production operating procedure
      and keep signing keys outside the repository.
- [ ] Enforce GitHub branch protection and PR-only merges to `main`.
- [ ] Run real OpenClaw/Hermes soak tests against the `.14` agents and preserve
      the generated report.
- [ ] Run live embedding/Qwen regression tests against DGX Spark `.10` and
      preserve the generated reports.

## Production interpretation

The project is production-shaped for a trusted local/team deployment. The right
target is not SaaS; it is a self-hosted, operator-owned memory appliance:
private by default, observable, recoverable, and easy for native agent plugins to
use. Full production requires the unchecked items above plus the audit gates.
