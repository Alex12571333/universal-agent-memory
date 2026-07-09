# Enterprise readiness checklist

This checklist defines the current production bar for Obelisk Memory.

## Required before production

- [x] API/UI runs in Docker.
- [x] PostgreSQL is the source of truth.
- [x] Migrations are forward-only and run before API startup.
- [x] Production compose exposes only API/UI to the host.
- [x] API key auth can protect API, docs, metrics, and UI.
- [x] Scoped API keys can separate operator, agent, read, and write access.
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

## Still recommended before a multi-user rollout

- [ ] Put the API behind a real TLS reverse proxy.
- [ ] Add persistent key registry with rotation metadata and last-used audit.
- [ ] Add audit log export and retention policy for regulated environments.
- [ ] Add automated scheduled backups and restore drills.
- [ ] Add dashboards/alerts for outbox lag, dead letters, Qdrant failures, and
      embedding latency.
- [ ] Add optional row-level encryption for selected memory scopes.
- [ ] Add signed vault import manifests.

## Production interpretation

The project is now production-shaped for a trusted local/team deployment. It is
not yet a fully managed enterprise SaaS product, and that is intentional. The
right target is a self-hosted, operator-owned memory appliance: private by
default, observable, recoverable, and easy for native agent plugins to use.
