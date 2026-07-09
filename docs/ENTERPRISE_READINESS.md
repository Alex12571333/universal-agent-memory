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
- [x] API key auth can protect API, docs, metrics, and UI.
- [x] Scoped API keys can separate operator, agent, read, and write access.
- [x] HTTP responses include baseline browser/API security headers.
- [x] Health checks exist for API, PostgreSQL, and NATS.
- [x] Metrics endpoint exists.
- [x] Privacy guard redacts common secrets and high-risk PII.
- [x] Backup script exists.
- [x] Vault export/import uses safe dry-run and CAS supersede.
- [x] Qdrant/vector indexing is async and fail-soft.
- [x] Memory LLM is separate from embedding endpoint.
- [x] Qwen/Spark `.10` memory LLM defaults are documented.
- [x] OpenClaw and Hermes integration guides exist.
- [x] CI workflow validates lint, tests, web build, and compose configs.
- [x] Enterprise readiness check script exists.
- [x] Production gap audit exists and forbids over-claiming readiness.

## Required before calling it full production

- [ ] Put the API behind a real TLS reverse proxy.
- [ ] Add persistent key registry with rotation metadata and last-used audit.
- [ ] Add audit log export and retention policy for regulated environments.
- [ ] Add automated scheduled backups and restore drills.
- [ ] Add dashboards/alerts for outbox lag, dead letters, Qdrant failures, and
      embedding latency.
- [ ] Add optional row-level encryption for selected memory scopes.
- [ ] Add signed vault import manifests.
- [ ] Enforce GitHub branch protection and PR-only merges to `main`.
- [ ] Run real OpenClaw/Hermes soak tests against the `.14` agents.
- [ ] Run live embedding/Qwen regression tests against DGX Spark `.10`.

## Production interpretation

The project is production-shaped for a trusted local/team deployment. The right
target is not SaaS; it is a self-hosted, operator-owned memory appliance:
private by default, observable, recoverable, and easy for native agent plugins to
use. Full production requires the unchecked items above plus the audit gates.
