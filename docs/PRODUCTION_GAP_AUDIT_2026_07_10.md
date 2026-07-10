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
| Docker | Dev and prod compose exist; prod hides internal infra ports | Good for trusted local/team deployment |
| API auth | Bearer key, scoped keys and non-secret key registry with last-used/revoked state exist; `/health` public | Strong local/team baseline; still not enterprise IAM |
| Audit trail | Append-only `audit_events` table, RLS, operator export API, tamper-evident JSONL bundle, metrics, tests | Strong baseline; retention schedule and private-key signing still needed |
| Browser/API hardening | Security headers are enforced by middleware and tests | Baseline present |
| Data model | Append-only memory, CAS supersede, provenance, statuses | Strong foundation |
| Conversation capture | Raw conversation ledger exists, but curation remains explicit/manual or hook-driven | Not “automatically remembers everything” yet |
| Embeddings | Real provider support exists; fake remains available for CI/emergency | Production depends on real endpoint and reindex discipline |
| Memory LLM | Qwen/Spark `.10` config exists and fails soft | Needs live quality evaluation before autonomy |
| OpenClaw/Hermes | Native adapter scaffolds and tests exist | Needs real runtime soak tests on `.14` |
| UI | React dashboard exists and is improving | Operator-grade, not yet admin-console complete |
| Testing | Unit, integration-style, benchmark scripts, web build | Needs load/chaos/restore/security tests |
| Release process | `AGENTS.md` describes issue/PR workflow | Main branch protection and PR-only enforcement are not proven |
| Operations | Runbook, backup/restore scripts, isolated restore-drill script, release checklist | Needs scheduled backup automation and alerts |

## What “full production level” means for this project

Full production for Obelisk Memory does not mean SaaS. It means a self-hosted
memory appliance that can run for months without silently corrupting memory,
leaking secrets, or letting agents poison each other.

Required gates:

1. **Security gate**
   - TLS or VPN/reverse proxy in front of any non-local deployment.
   - Long random master key plus scoped per-agent/operator keys.
   - Key rotation record: owner, scope, created time, last used, revoked time.
     Baseline registry exists; external secret manager integration is still
     recommended for larger deployments.
   - Audit log export for write, supersede, conflict-decision, vault-import,
     settings-change and model-change events. Durable storage and a checksum
     bundle exist; retention schedule and private-key-signed exports are still
     required for regulated environments.
   - Optional row-level encryption for high-risk scopes.
   - Security headers and CSP stay covered by tests.

2. **Reliability gate**
   - PostgreSQL backup schedule and tested restore drill. Manual isolated
     restore drill exists; automated scheduling/alerting is still required.
   - Migration rehearsal against a copy of a real volume.
   - Outbox/NATS/Qdrant dead-letter monitoring with alerts.
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

4. **Agent-integration gate**
   - OpenClaw and Hermes run real lifecycle hooks:
     before-run recall, before-model compact context, after-message retain,
     after-tool retain, checkpoint, run-complete reflection.
   - Each agent uses its own scoped key and namespace.
   - Failed memory calls are fail-soft and visible in logs/metrics.
   - Soak test with parallel agents verifies no cross-project leakage.

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

- “Fully enterprise production-ready” — not until branch protection, automated
  restore drills, alerting, signed audit retention, and real agent soak tests
  are proven.
- “Remembers all conversations automatically” — the raw ledger can store full
  turns, but automatic capture depends on agent/plugin hooks being installed.
- “Semantic recall is production quality” — only true when a real embedding
  model is configured, indexed, monitored, and regression-tested.
- “GraphRAG is authoritative” — graph edges and LLM extraction need evidence and
  review, otherwise the graph can make false links look trustworthy.

## Highest-priority next work

1. Add audit retention policy, scheduled immutable storage, and private-key
   signatures for audit bundles.
2. Add automated scheduled backup execution and restore-drill alerting.
3. Add live `.14` OpenClaw/Hermes soak test script.
4. Add worker/outbox/embedding alert metrics and dashboard panel.
5. Add UI conflict-resolution flow with accept/supersede/reject actions.
6. Add branch-protection/PR-only release policy in GitHub settings.
7. Add optional external secret-manager integration.
