# Production readiness audit

Obelisk Memory is a self-hosted memory server foundation. Full production
requires runtime correctness, isolation, recoverability, supply-chain controls
and preserved target-deployment evidence. Static repository checks confirm that
artifacts exist; they do not prove a fresh deployment works or remains safe
under failure.

## Current honest status

| Area | Current state | Production verdict |
|---|---|---|
| Architecture | PostgreSQL source of truth, Qdrant index, outbox, NATS workers, vault, API, UI | Good foundation |
| Docker | Local self-hosted compose keeps infra ports internal in production topology; database secrets use dedicated mounts; deployment preflight writes boundary evidence | Fresh role provisioning still needs clean target-boot evidence |
| API auth | Bearer keys, route capabilities, strict principal bindings, env validation and non-secret key registry exist; `/health` is public | Identity isolation is implemented and locally proven; target-deployment evidence remains required |
| Audit trail | `audit_events`, export/signing and retention tools exist; retain, CAS supersede and accepted proposal audit records share their canonical write transaction | Coverage is incomplete for conversations, graph, reflection, reindex and checkpoints |
| Browser/API hardening | Security headers are enforced by middleware and tests | Baseline present |
| Data model | Append-only memory, CAS supersede, atomic conflict-winner revisions, canonical active-head recall, provenance, statuses and pgcrypto for canonical text, raw conversations and proposals exist | Sensitive-table, legacy-data and backup encryption remain incomplete |
| Conversation capture | Raw ledger, proposal-first curation, `curated_only` purge, bounded staging TTL, stable identities and live pipeline runner exist | The purge schedule and installed agent hooks still require target evidence |
| Embeddings | Provider-neutral endpoints, async worker, fail-soft recall, scoped sync and immutable collection identity exist | Target outage/migration evidence remains required |
| Memory LLM | Provider-neutral OpenAI-compatible contract, deterministic fallback, proposal-first curation and live runner exist | Target failure/concurrency evidence and quality evaluation remain required; generated content never becomes recallable until an operator accepts its proposal |
| OpenClaw/Hermes | Native adapters and a standalone live soak evaluator | OpenClaw native hook and Hermes/Qwen one-shot recall were revalidated on `.14`; Hermes requires the documented Qwen `enable_thinking=false` profile setting. Multi-hour native-runtime soak evidence remains required. |
| UI | React dashboard supports signed HttpOnly operator sessions, CSRF, memory/vault editing, conflict decisions, exact-origin model probe policy and a JSON walkthrough runner | Target HTTPS session/egress evidence and secret-manager integration evidence remain required |
| Testing | Unit/API tests, live PostgreSQL/Qdrant isolation/failure tests, benchmark scripts, web build and load runner exist | Target chaos/security evidence is still missing |
| Release process | PR flow, release reports and a signed content-addressed evidence manifest exist | OCI build provenance, SBOM, scanning and image signing are still required |
| Operations | Runbook, backup/restore scripts, isolated restore drill, schedule/observability preflights, signed vault/audit bundles and release evidence verifier | Needs target evidence and the runtime blockers below resolved |

## Runtime blockers

The following items block a production claim even when unit tests and the
static readiness script are green.

### P0 — correctness and isolation

1. **Fresh production database provisioning needs target proof.**
   The repository no longer ships the `memory_app/memory` login. The migration
   runner now creates or rotates the configured application role with safe
   identifier composition, rejects administrator/reserved identities, and
   reapplies runtime grants. Production Compose mounts separate administrator
   and application password files, while API/workers/backup assemble escaped
   DSNs from explicit components. Unit and Compose-config tests cover this
   contract. An isolated local PostgreSQL 17 clean boot passed all 13 ledger
   integration scenarios, migration rerun and password rotation (old rejected,
   new accepted). A separate production-Compose smoke also initialized a fresh
   volume and completed all migrations through the two mounted Docker secrets.
   The same proof must still be executed and preserved on the target Docker
   runtime before this gate is closed.

2. **Identity provisioning and binding exist, but installer bootstrap needs longer target proof.**
   An operator-only, audited and idempotent endpoint now provisions an agent and
   optional owned thread atomically, refuses cross-scope ID reuse, and has API,
   service and optional PostgreSQL retain coverage. Operator-only workspace
   provisioning now creates the required scope before agent registration,
   without broadening the runtime database ACL. A real `.14` target evaluator
   successfully provisioned isolated OpenClaw/Hermes soak identities then
   completed retain/retry/recall and cross-workspace exclusion. Agent keys
   intentionally cannot self-provision arbitrary identities. Native installers
   still need longer-running evidence before safe automatic first-use
   registration is claimed.

3. **Identity-bound authorization needs target evidence.**
   Production configuration can now require each `agent` principal to map to a
   fixed tenant/workspace/agent UUID. Startup rejects missing bindings; request
   middleware rejects forged identity fields and foreign threads; administrative
   memory, graph, vault, conflict, settings and review routes require operator
   scope. Private recall is owner-filtered independently by PostgreSQL,
   in-memory storage, Qdrant and retrieval fusion. API tests cover two competing
   agent principals and the control-plane matrix. A live local PostgreSQL 17 and
   Qdrant run passed 21/21 scenarios, including cross-agent private-vector and
   thread-ownership isolation. The same tests and native-agent soak must be
   preserved from the target deployment before this release gate is closed.

4. **Conflict-winner transactions need target evidence.**
   Supersede/archive active-head semantics are now enforced in PostgreSQL and
   in-memory recall. Qdrant batches candidate IDs through the PostgreSQL
   active-head check, so an old vector cannot leak during worker lag; after a
   successful replacement upsert the worker deletes the superseded point. The
   policy has unit coverage plus clean PostgreSQL 17 (13/13) and live Qdrant
   (6/6) integration evidence. Accepted/overridden reviews now atomically
   archive every recallable loser root, restore a historical winner when
   necessary, append all outbox events, and persist the immutable review with
   `applied_memory_id`; any stale CAS rolls back the entire operation. The same
   multi-root rollback/idempotency proof must be preserved on the target
   deployment before closing the release gate.

5. **Encryption-at-rest coverage is incomplete.**
   pgcrypto now protects new `memory_items.text` rows, raw conversation content,
   proposals, proposal evidence, provenance quotes, observation summaries,
   checkpoint state and audit metadata. `scripts/reencrypt_legacy_pgcrypto.py`
   now converts the corresponding legacy plaintext rows in restart-safe,
   administrator-only batches and emits evidence reports. It must be run after
   every writer uses the same pgcrypto key; unprotected JSON metadata outside
   those fields can still contain plaintext.
   New scheduled PostgreSQL artifacts use authenticated AES-256-GCM,
   but direct `backup.py` is intentionally a low-level raw-dump primitive and
   must not be used for a production schedule. Production documentation must
   not describe `UAM_MEMORY_TEXT_ENCRYPTION_SCOPES=all` as full-database
   encryption.

6. **Fail-soft recall is implemented; broader reliability evidence remains required.**
   PostgreSQL is the required canonical candidate source. Qdrant connection and
   query failures are isolated per source, recorded without endpoint/credential
   text, and recall falls back to PostgreSQL lexical results. `/health` remains
   liveness-only while public `/ready` actively checks canonical storage,
   returns `503 not_ready` for PostgreSQL failure and `200 degraded` for an
   optional vector outage. The embedding worker requires Qdrant and fails fast
   so JetStream retry semantics remain intact. Unit/API tests cover optional,
   required and recovery transitions. A local PostgreSQL 17 runtime test passed
   both API fallback and worker fail-fast scenarios against an intentionally
   unreachable Qdrant endpoint. A controlled appliance Qdrant stop/restart on
   2026-07-12 then confirmed canonical PostgreSQL recall during `degraded`
   readiness and recovery to `ready` after a real hybrid recall. Multi-replica
   failure evidence remains part of the broader reliability gate.

7. **Workspace-safe reindex needs target concurrency evidence.**
   `EmbeddingService.reindex_all()` now computes and validates every embedding
   before touching Qdrant, serializes concurrent work per workspace, upserts the
   complete replacement set, then deletes only stale IDs captured inside the
   same tenant/workspace boundary. Empty canonical workspaces clear only their
   own vectors. Other workspaces are never deleted, and provider/upsert failure
   cannot empty the previous set. Unit tests cover cross-workspace preservation,
   empty sync, boundary rejection and provider failure. Live Qdrant passed 11/11,
   including scoped replacement and failed-upsert preservation. Multi-replica
   target concurrency and crash-during-partial-upsert evidence are still needed.
   Collection startup now enforces dense dimension and immutable model metadata;
   incompatible configuration fails before serving recall. The migration runner
   builds a separately named collection, verifies exact workspace point count,
   preserves the source for rollback and emits an activation report. A local
   PostgreSQL/Qdrant migration indexed and verified 19/19 active points.

8. **PostgreSQL checkpoint CAS needs target concurrency evidence.**
   The invalid aggregate `FOR UPDATE` query has been replaced by a
   tenant/thread-scoped transaction advisory lock followed by an ordered head
   read. First saves now use the same compare-and-swap path with expected head
   zero, preventing the unique-violation race. Unit coverage and an optional
   two-writer PostgreSQL integration test exist; that test passed on the
   isolated PostgreSQL 17 clean boot and again against the live local runtime
   role on 2026-07-12 (one first writer saved, one received a stale conflict).
   Multi-replica and crash-during-write evidence remain required before a
   broader release claim.

9. **Browser session auth needs target evidence.**
   `/ui` can render its login shell without exposing operator data. The React
   client exchanges an operator/admin key once for a signed HttpOnly
   `SameSite=Strict` cookie, keeps the original key out of browser storage,
   bootstraps a per-session CSRF token, and attaches it to every mutation.
   Sessions expire, require `Secure` cookies in production and are invalidated
   by key revocation/rotation. API tests cover login scope, cookie flags, CSRF,
   logout and revocation. A real HTTPS browser walkthrough and target session
   expiry/rotation evidence remain required.

10. **Model endpoint boundary needs target egress evidence.**
    Model saves and probes enforce `UAM_MODEL_ENDPOINT_ALLOWLIST` as normalized
    exact origins; without it only loopback endpoints are accepted. Credentials,
    query strings and fragments in base URLs are rejected, and both embedding
    and LLM HTTP clients refuse redirects. Desired settings are written
    atomically with mode `0600` and never persist provider API keys; legacy
    plaintext keys are removed on load. Production validation requires every
    configured embedding/LLM origin to be allowlisted. A container/cluster
    egress-deny policy and target probe evidence remain required defense in
    depth.

11. **Conversation staging needs longer-running scheduler evidence.**
    `raw_only` rejects curation, `raw_and_curated` preserves the source, and
    `curated_only` now purges message text immediately after successful
   idempotent curation while retaining audit identity. However, `curated_only`
   stores raw staging text until curation runs. Abandoned turns now receive a
   bounded TTL and an operator-only batch purge endpoint. The local Mac
   appliance now installs a daily `launchd` purge job and a real smoke run
   completed with exit `0`; longer-running evidence is still needed to prove a
   backlog or worker failure cannot retain staging transcripts indefinitely.

12. **Proposal/LLM boundary needs target failure evidence.**
    `ConversationCurator` now emits an evidence-linked proposal and never makes
    LLM output recallable by itself. PostgreSQL proposal acceptance now locks
    the proposal and writes canonical memory, idempotency, outbox and accepted
    status in one transaction. A live target concurrent-accept check on
    2026-07-12 returned the same memory ID to both requests with exactly one
    creation. A live runtime-role PostgreSQL failure-injection test then forced
    outbox insertion to fail and proved that the transaction left the proposal
    open with no proposal-derived memory or outbox event. Multi-replica
    reliability evidence remains part of the broader release gate.

### P1 — reliability, scale and operations

1. Embedding and deterministic reflection jobs have deployed workers. Dedupe
   and graph extraction still need an evidence-grounded policy and graph is not
   yet a recall source.
2. Outbox retry uses capped exponential backoff before an event is
   dead-lettered. JetStream delivery is bounded with exponential NAK delays,
   durable DLQ records and an operator-selected replay command. Stream
   size/age limits and authenticated NATS remain to be installed for a long
   running appliance.
3. PostgreSQL uses an explicit bounded `psycopg_pool` connection pool per
   process. The deployment still has one API process and single-node
   PostgreSQL/Qdrant/NATS volumes, with no HA or safe horizontal-scaling
   design.
4. Backup covers PostgreSQL only. The scheduled runner encrypts new dumps with
   AES-256-GCM and verifies decryption through the restore drill. A local LAN
   target drill on 2026-07-12 passed source/restore row-count parity, forced
   tenant RLS, a separate restored-ledger reindex of 35 active records and
   `qdrant_hybrid` semantic recall in a fresh Qdrant collection. The scheduled
   runner now emits and immediately verifies an optional signed bundle manifest
   over the encrypted dump and audit manifest. The target still needs a
   separately held signing key, retained signed artifacts and a multi-run
   schedule report.
5. Worker embedding metrics are now exposed privately at
   `embedding-worker:9091/metrics`; release evidence must demonstrate that the
   monitoring target scrapes this endpoint and its alerts are routed.
6. Plaintext PostgreSQL lexical recall now filters active, scope-authorized FTS
   candidates in SQL before decoding them in Python. The pgcrypto mode keeps a
   correctness-first post-decryption fallback because ciphertext cannot use the
   normal FTS index; a protected searchable index and production pagination for
   several list/export paths remain required.
7. `memory.retain` and `memory.supersede` audit records are now committed in
   the same PostgreSQL transaction as their canonical item, idempotency key and
   outbox event. Failure injection proves an audit insert error rolls back the
   memory replacement and outbox event. Proposal acceptance now also commits
   the proposal review, canonical memory, outbox and `proposal.accept` audit
   record together; audit failure injection rolls all of them back. Denied
   requests, raw conversations, graph writes, reflect, reindex and checkpoints still need
   complete status-aware and, where applicable, transactional audit coverage.
8. The application role is now granted `SELECT/INSERT` by default, with only
   explicit operational mutations (outbox, idempotency, staging, proposal,
   agent/thread, review and checkpoint retention) re-granted. It cannot update
   or delete `memory_items` or `audit_events`; the server fails closed when
   `UAM_ENFORCE_RUNTIME_DB_ACL=true` detects stale broad ACLs. CAS supersede
   uses a transaction-scoped advisory lock rather than `SELECT FOR UPDATE`, so
   it remains operational with that least-privilege role; PostgreSQL integration
   tests cover both CAS/idempotency and audit-insert rollback. The migration job
   must still be rerun during every upgrade and a target privilege report must
   be retained. Database-enforced immutable audit triggers and migration
   checksums remain future hardening.
9. Memory, conversation and proposal idempotency keys are namespaced by
   workspace inside each adapter. A future schema migration can make this
   namespace queryable for operations, but independent workspace retries no
   longer collide.
10. Conversation staging, audit windows and checkpoint compaction have bounded
    retention paths. An admin-only, dry-run-first maintenance job now ages out
    processed/dead-lettered outbox records and idempotency keys in bounded
    batches while SQL-excluding pending outbox events. It still needs an
    installed target schedule, TTL policy approval, and preserved target report;
    raw conversations and proposals remain governed by their explicit retention
    policies rather than blanket deletion.
11. Qdrant retrieval is dense-only in the actual worker path and multi-layer
    filter handling is incomplete. Model/dimension identity is now enforced,
    but collection migration still needs target release evidence.
12. Conflict/reflection extraction is mostly deterministic English-pattern
    matching. Russian paraphrases and temporal facts need evaluated multilingual
    extraction with provenance and operator-decision precedence.
13. Runtime model settings are persisted only when an optional host path is
    configured; otherwise the UI reports a desired config that disappears on
    restart. Provider keys are intentionally not persisted, so an operator
    must supply them via environment or a secret manager after restart.
14. Recall exposes a conservative `index_stale` flag when canonical outbox work
    is pending or freshness cannot be checked. Workspace-level vector lag and
    exact affected-memory counts still need a durable index-state ledger.
15. Worker-specific Prometheus metrics are served privately by the embedding
    worker. Structured worker logs plus NATS/worker state in readiness are not
    yet an operational readiness gate.

### P2 — deployment hardening and supply chain

- enforce request-body limits, rate limits and pagination budgets;
- add container resource limits, read-only filesystems where possible, dropped
  capabilities, `no-new-privileges`, log rotation and graceful shutdown;
- pin GitHub Actions and container base images by immutable digest;
- lock dependency graphs and run SAST, secret, dependency and image scans;
- generate SBOM/provenance, publish OCI images by digest and sign them;
- add a tag/release workflow that verifies the signed release evidence bundle;
- remove unused MinIO from the default production surface until durable artifact
  storage is wired to it.

## Test coverage required for these blockers

- fresh production boot with generated application credentials;
- real PostgreSQL agent/thread retain and concurrent checkpoint CAS;
- cross-agent private/workspace authorization and vault/conflict permissions;
- target evidence for atomic multi-root conflict winner application and rollback;
- Qdrant/embedding outage fallback and separate `/ready` dependency checks;
- multi-workspace, failure-safe reindex;
- encrypted-data and encrypted-backup inspection;
- worker failure metrics, poison-message replay and queue-retention tests;
- restore data parity followed by full reindex and semantic recall;
- authenticated browser UI and model-endpoint SSRF policy tests;
- retention-policy deletion/expiry tests plus evidence-grounded proposal-only
  LLM curation and atomic proposal acceptance tests;
- restart-persistent secret-safe model settings and computed index-staleness
  tests.

## What “full production level” means for this project

Full production for Obelisk Memory does not mean SaaS. It means a self-hosted
memory appliance that can run for months without silently corrupting memory,
leaking secrets, or letting agents poison each other.

Required gates:

1. **Security gate**
   - A local-only or trusted-LAN network boundary around the API. The
     repository ships `scripts/deployment_preflight.py`; a domain, VPN, or
     reverse proxy is not a requirement for the self-hosted local appliance,
     not a public backend `6798`.
   - Long random master key plus scoped per-agent/operator keys.
   - Key rotation record: owner, scope, created time, last used, revoked time.
     Baseline registry exists. Application code supports `*_FILE` reads for
     API/model/signing/encryption secrets and complete database URLs. Production
     Compose also mounts separate database password files and assembles DSNs
     without exposing password values through Compose interpolation. The
     `scripts/secret_files_preflight.py` writes release evidence that raw secret
     env values are empty. The target still needs an external secret manager,
     a rotation procedure and clean-boot evidence.
   - `.env.production` must pass `scripts/validate_production_env.py` with
     strict production flags before deployment.
   - Audit log export for write, supersede, conflict-decision, vault-import,
     settings-change and model-change events. Durable storage plus optional
     HMAC-signed paginated export bundles exist. The retention runner exports
     and verifies old windows before pruning; regulated environments still need
     signing-key custody, an installed schedule and immutable storage.
   - Unchanged vault integrity bundles must use checksum verification and signed
     manifests. Editable vault exports are currently manifest-free because the
     CLI cannot re-sign reviewed edits; signed human-edit workflows remain a
     production gap. Signing keys must stay outside the repository.
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
     from the actual OpenClaw/Hermes deployment.

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
   - Release checklist includes manual UI walk-through and live embedding
     regression evidence.
   - Release evidence manifest verifies saved deployment preflight,
     secret-files preflight, ops schedule preflight, observability preflight,
     agent, LLM, UI walkthrough, metrics, backup, signed vault import and
     branch-protection JSON reports before a full-production claim. The
     generator keeps artifact keys aligned with verifier requirements.
   - Versioned changelog and rollback instructions exist.

## Things that must not be claimed yet

- “Fully enterprise production-ready” — not until the P0 runtime blockers are
  fixed and target restore, alerting, audit-retention and real native-agent
  evidence are preserved.
- “Remembers all conversations automatically” — the raw ledger can store full
  turns and release evidence can prove capture→curate→recall on a target
  server, but automatic capture still depends on agent/plugin hooks being
  installed.
- “Semantic recall is production quality” — only true when a real embedding
  model is configured, indexed, monitored, and regression-tested.
- “GraphRAG is authoritative” — graph edges and LLM extraction need evidence and
  review, otherwise the graph can make false links look trustworthy.

## Highest-priority next work

1. Preserve fresh-production smoke evidence for each release. The repository
   runner now proves isolated startup and application-role login with generated
   credentials; target password-rotation evidence is still required.
2. Prove agent/thread provisioning on PostgreSQL and wire the operator bootstrap
   into OpenClaw/Hermes installation; then add real checkpoint concurrency
   coverage.
3. Preserve identity-bound authorization and native-agent soak evidence from
   the target deployment.
4. Preserve target evidence for atomic accepted/overridden conflict winners,
   stale-CAS rollback and Qdrant precedence; the implementation and local live
   coverage are complete.
5. Preserve target evidence for Qdrant outage/recovery, workspace reindex and
   the verified model/dimension collection migration.
6. Complete remaining sensitive-table/legacy-data encryption and signed backup
   coverage, then preserve target evidence for the authenticated UI session and
   model-egress boundaries.
7. Install schedules, immutable storage, monitoring and alert routing; then run
   native-agent, conversation, load, UI, embedding and LLM target gates.
8. Seal every release with the signed content-addressed v2 evidence manifest,
   then add OCI SBOM, provenance, vulnerability scanning and image signing.
