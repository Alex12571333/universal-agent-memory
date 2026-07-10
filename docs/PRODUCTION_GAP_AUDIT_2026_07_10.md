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
| Docker | Dev/prod compose plus Caddy TLS proxy example exist; prod hides internal infra ports; deployment preflight writes boundary evidence | Good for trusted local/team deployment; real TLS boundary must be installed and verified per release |
| API auth | Bearer key, scoped keys, env validator, secret-files preflight and non-secret key registry with last-used/revoked state exist; `/health` public | Strong local/team baseline; still not enterprise IAM |
| Audit trail | Append-only `audit_events` table, RLS, operator export API, signed paginated JSONL bundle, safe retention runner, metrics, tests | Strong baseline; environment schedule, key custody and immutable storage still needed |
| Browser/API hardening | Security headers are enforced by middleware and tests | Baseline present |
| Data model | Append-only memory, CAS supersede, provenance, statuses, optional pgcrypto ciphertext for all or selected memory scopes | Strong foundation |
| Conversation capture | Raw conversation ledger exists, but curation remains explicit/manual or hook-driven | Not “automatically remembers everything” yet |
| Embeddings | Real provider support exists; Qdrant can redact raw text payloads and hydrate recall from PostgreSQL; fake remains available for CI/emergency | Production depends on real endpoint, `UAM_QDRANT_PAYLOAD_TEXT=false`, and reindex discipline |
| Memory LLM | Provider-neutral OpenAI-compatible contract, fail-soft adapter and live regression runner exist | Needs saved live endpoint regression evidence before autonomy |
| OpenClaw/Hermes | Native adapter scaffolds, tests and live soak runner exist | Needs saved real runtime soak evidence from `.14` |
| UI | React dashboard and fallback `/ui` support real memory/vault editing, actionable conflict decisions and a JSON UI walkthrough runner | Operator-grade baseline; still needs preserved live walkthrough evidence per release |
| Testing | Unit, integration-style, benchmark scripts, web build, concurrent load smoke runner | Needs preserved live load/chaos/security evidence from the target deployment |
| Release process | `main` branch protection requires PR flow, strict `python`/`web` checks, conversation resolution, admin enforcement, and machine-readable release notes/rollback evidence | Release gate baseline is now proven by `scripts/check_branch_protection.py` and `scripts/generate_release_notes.py`; keep verifying before releases |
| Operations | Runbook, backup/restore scripts, isolated restore-drill script, scheduler-ready backup runner, ops schedule preflight, observability preflight, signed vault manifests with import evidence, metrics health evaluator with JSON report/webhook, Grafana/Prometheus templates, release checklist, release notes, release evidence generator and verifier | Needs target-environment evidence for schedules, monitoring import and alert routing |

## What “full production level” means for this project

Full production for Obelisk Memory does not mean SaaS. It means a self-hosted
memory appliance that can run for months without silently corrupting memory,
leaking secrets, or letting agents poison each other.

Required gates:

1. **Security gate**
   - TLS or VPN/reverse proxy in front of any non-local deployment. The
     repository ships a Caddy example and `scripts/deployment_preflight.py`;
     production evidence requires the deployed host to expose HTTPS/proxy only,
     not a public backend `6798`.
   - Long random master key plus scoped per-agent/operator keys.
   - Key rotation record: owner, scope, created time, last used, revoked time.
     Baseline registry exists. Runtime supports mounted `*_FILE` secrets for
     API/model/signing/encryption/database secrets, and
     `scripts/secret_files_preflight.py` writes release evidence that raw secret
     env values are empty. The target deployment still needs an actual external
     secret manager and rotation procedure.
   - `.env.production` must pass `scripts/validate_production_env.py` with
     strict production flags before deployment.
   - Audit log export for write, supersede, conflict-decision, vault-import,
     settings-change and model-change events. Durable storage plus optional
     HMAC-signed paginated export bundles exist. The retention runner exports
     and verifies old windows before pruning; regulated environments still need
     signing-key custody, an installed schedule and immutable storage.
   - Vault import bundles must use manifest checksum verification and signed
     manifests for production operator workflows. CLI support and JSON release
     evidence exist; the deployment must keep signing keys outside the
     repository.
   - Qdrant payload text redaction must stay enabled for production vector
     stores.
   - `UAM_MEMORY_TEXT_ENCRYPTION=pgcrypto` must be enabled for production
     PostgreSQL storage, with the key held outside the repository. Use
     `UAM_MEMORY_TEXT_ENCRYPTION_SCOPES=all` by default or a documented
     selective scope policy such as `private,thread`.
   - Security headers and CSP stay covered by tests.

2. **Reliability gate**
   - PostgreSQL backup schedule and tested restore drill. A scheduler-ready
     runner with restore drill, JSON report and failure webhook exists;
     `scripts/ops_schedule_preflight.py` verifies installed schedule evidence,
     alert routes and durable artifact roots for the target deployment.
   - Migration rehearsal against a copy of a real volume.
   - Outbox dead-letter/lag and embedding failure/latency monitoring with
     alerts. Metrics health evaluator, embedding counters and
     `scripts/observability_preflight.py` verify dashboard/alert coverage for
     the target monitoring artifacts.
   - Worker restart and poison-event behavior tested.
   - Graceful degradation when embeddings, Qdrant, or the configured memory LLM are down.

3. **Memory-quality gate**
   - Real embedding endpoint is mandatory outside CI/emergency mode.
   - Qdrant collection dimension must match the active embedding model.
   - Reindex plan is required for model/dimension changes.
   - Conflict inbox must show evidence, winner, stale/superseded chain, and
     operator action history.
   - LLM-derived memories must be proposals or evidence-grounded observations,
     not unverified truth.
   - Memory LLM endpoint/model changes must pass `scripts/real_memory_llm_eval.py`
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
   - Conflict resolution is actionable from UI and persists audited operator
     decisions.
   - Graph is movable, zoomable, and reflects real nodes/edges.
   - Model settings explain restart/reindex impact before applying.

6. **Release gate**
   - `main` protected: PR required, green CI required, no direct pushes.
   - CI runs lint, tests, web build, compose config, static readiness, and
     in-process production readiness.
   - Release checklist includes manual UI walk-through and live embedding probe.
   - Release evidence manifest verifies saved deployment preflight,
     secret-files preflight, ops schedule preflight, observability preflight,
     agent, LLM, UI walkthrough, metrics, backup, signed vault import and
     branch-protection JSON reports before a full-production claim. The
     generator keeps artifact keys aligned with verifier requirements.
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

1. Install audit retention schedule, external signing-key custody, scheduled
   immutable storage, and deployment verification that signed range exports are
   preserved before pruning.
2. Install environment-level backup schedule, immutable artifact storage, and
   alert routing for `scheduled_backup.py` reports.
3. Run `scripts/agent_soak_eval.py` from the `.14` OpenClaw/Hermes deployment
   path and preserve the JSON report as release evidence.
4. Run `scripts/load_smoke_eval.py` against the target release server and
   preserve the JSON report as release evidence.
5. Install the provided metrics dashboard/alert rules and scheduled-backup
   health reports into the deployment alerting stack.
6. Run `scripts/ui_walkthrough_eval.py` against the release server and preserve
   the report showing conflict decision, vault editable text/archive, model
   settings probe, reindex and metrics.
7. Preserve and verify the release evidence manifest in every release bundle.
8. Install and verify the target environment secret manager using the supported
   `*_FILE` runtime secret paths.
