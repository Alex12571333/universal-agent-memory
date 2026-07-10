# Production gap audit — 2026-07-10

This audit replaces the earlier “green report means production” interpretation.
Obelisk Memory is a serious self-hosted memory server foundation, but full
production level requires evidence across runtime, operations, security, release
process, and live agent behavior. A repository-level static check is useful, but
it is not enough.

## Current honest status

| Area | Current state | Production verdict |
|---|---|---|
| Architecture | PostgreSQL source of truth, Qdrant index, outbox, NATS workers, vault, API, UI | Good foundation |
| Docker | Dev/prod compose plus Caddy TLS proxy example exist; prod hides internal infra ports | Good for trusted local/team deployment; real TLS boundary must be installed |
| API auth | Bearer key, scoped keys, env validator and non-secret key registry with last-used/revoked state exist; `/health` public | Strong local/team baseline; still not enterprise IAM |
| Audit trail | Append-only `audit_events` table, RLS, operator export API, signed paginated JSONL bundle, metrics, tests | Strong baseline; retention schedule, key custody and immutable storage still needed |
| Browser/API hardening | Security headers are enforced by middleware and tests | Baseline present |
| Data model | Append-only memory, CAS supersede, provenance, statuses, optional pgcrypto ciphertext for canonical memory text | Strong foundation |
| Conversation capture | Raw conversation ledger exists, but curation remains explicit/manual or hook-driven | Not “automatically remembers everything” yet |
| Embeddings | Real provider support exists; Qdrant can redact raw text payloads and hydrate recall from PostgreSQL; fake remains available for CI/emergency | Production depends on real endpoint, `UAM_QDRANT_PAYLOAD_TEXT=false`, and reindex discipline |
| Memory LLM | Qwen/Spark `.10` config, fail-soft adapter and live regression runner exist | Needs saved live `.10` regression evidence before autonomy |
| OpenClaw/Hermes | Native adapter scaffolds, tests and live soak runner exist | Needs saved real runtime soak evidence from `.14` |
| UI | React dashboard exists and is improving | Operator-grade, not yet admin-console complete |
| Testing | Unit, integration-style, benchmark scripts, web build | Needs load/chaos/restore/security tests |
| Release process | `main` branch protection requires PR flow, strict `python`/`web` checks, conversation resolution, and admin enforcement | Release gate baseline is now proven by `scripts/check_branch_protection.py`; keep verifying before releases |
| Operations | Runbook, backup/restore scripts, isolated restore-drill script, scheduler-ready backup runner, signed vault manifests, metrics health evaluator with JSON report/webhook, release checklist | Needs environment scheduler, durable/immutable storage and dashboard wiring |

## What “full production level” means for this project

Full production for Obelisk Memory does not mean SaaS. It means a self-hosted
memory appliance that can run for months without silently corrupting memory,
leaking secrets, or letting agents poison each other.

Required gates:

1. **Security gate**
   - TLS or VPN/reverse proxy in front of any non-local deployment. The
     repository ships a Caddy example; production evidence requires the deployed
     host to expose HTTPS/proxy only, not a public backend `6798`.
   - Long random master key plus scoped per-agent/operator keys.
   - Key rotation record: owner, scope, created time, last used, revoked time.
     Baseline registry exists; external secret manager integration is still
     recommended for larger deployments.
   - `.env.production` must pass `scripts/validate_production_env.py` with
     strict production flags before deployment.
   - Audit log export for write, supersede, conflict-decision, vault-import,
     settings-change and model-change events. Durable storage plus optional
     HMAC-signed paginated export bundles exist; regulated environments still
     need signing-key custody, retention schedule and immutable storage.
   - Vault import bundles must use manifest checksum verification and signed
     manifests for production operator workflows. CLI support exists; the
     deployment must keep signing keys outside the repository.
   - Qdrant payload text redaction must stay enabled for production vector
     stores.
   - `UAM_MEMORY_TEXT_ENCRYPTION=pgcrypto` must be enabled for production
     PostgreSQL storage, with the key held outside the repository.
   - Security headers and CSP stay covered by tests.

2. **Reliability gate**
   - PostgreSQL backup schedule and tested restore drill. A scheduler-ready
     runner with restore drill, JSON report and failure webhook exists; the
     deployment still must install the actual cron/systemd/orchestrator schedule
     and durable storage policy.
   - Migration rehearsal against a copy of a real volume.
   - Outbox dead-letter/lag and embedding failure/latency monitoring with
     alerts. Metrics health evaluator and embedding counters exist; deployment
     still needs dashboard/alert routing.
   - Worker restart and poison-event behavior tested.
   - Graceful degradation when embeddings, Qdrant, or Qwen/Spark are down.

3. **Memory-quality gate**
   - Real embedding endpoint is mandatory outside CI/emergency mode.
   - Qdrant collection dimension must match the active embedding model.
   - Reindex plan is required for model/dimension changes.
   - Conflict inbox must show evidence, winner, stale/superseded chain, and
     operator action history.
   - LLM-derived memories must be proposals or evidence-grounded observations,
     not unverified truth.
   - Qwen/Spark memory LLM changes must pass `scripts/real_memory_llm_eval.py`
     and preserve the JSON report before release.

4. **Agent-integration gate**
   - OpenClaw and Hermes run real lifecycle hooks:
     before-run recall, before-model compact context, after-message retain,
     after-tool retain, checkpoint, run-complete reflection.
   - Each agent uses its own scoped key and namespace.
   - Failed memory calls are fail-soft and visible in logs/metrics.
   - Soak test with parallel agents verifies no cross-project leakage; the
     repository runner exists, but full production requires a preserved report
     from the actual `.14` OpenClaw/Hermes deployment.

5. **UI/operator gate**
   - Dashboard values come from real API state, not fixed mock numbers.
   - Vault editor edits normal text only; embeddings/frontmatter stay internal.
   - Delete/archive is non-destructive and visible in history.
   - Conflict resolution is actionable from UI.
   - Graph is movable, zoomable, and reflects real nodes/edges.
   - Model settings explain restart/reindex impact before applying.

6. **Release gate**
   - `main` protected: PR required, green CI required, no direct pushes.
   - CI runs lint, tests, web build, compose config, static readiness, and
     in-process production readiness.
   - Release checklist includes manual UI walk-through and live embedding probe.
   - Versioned changelog and rollback instructions exist.

## Things that must not be claimed yet

- “Fully enterprise production-ready” — not until automated restore drills,
  alerting, signed audit retention, and real `.14` agent soak reports are
  proven.
- “Remembers all conversations automatically” — the raw ledger can store full
  turns, but automatic capture depends on agent/plugin hooks being installed.
- “Semantic recall is production quality” — only true when a real embedding
  model is configured, indexed, monitored, and regression-tested.
- “GraphRAG is authoritative” — graph edges and LLM extraction need evidence and
  review, otherwise the graph can make false links look trustworthy.

## Highest-priority next work

1. Add audit retention policy, external signing-key custody, scheduled immutable
   storage, and deployment verification that range exports are preserved.
2. Install environment-level backup schedule, immutable artifact storage, and
   alert routing for `scheduled_backup.py` reports.
3. Run `scripts/agent_soak_eval.py` from the `.14` OpenClaw/Hermes deployment
   path and preserve the JSON report as release evidence.
4. Wire metrics and scheduled-backup health reports into the deployment
   dashboard/alerting stack.
5. Add UI conflict-resolution flow with accept/supersede/reject actions.
6. Preserve branch-protection evidence in every release bundle.
7. Add optional external secret-manager integration.
