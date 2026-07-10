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
| Docker | Dev/prod compose plus Caddy TLS proxy example exist; prod hides internal infra ports; database secrets use dedicated mounts; deployment preflight writes boundary evidence | Fresh role provisioning is implemented but still needs clean target-boot evidence and a verified TLS boundary |
| API auth | Bearer keys, coarse scopes, env validator and non-secret key registry exist; `/health` is public | Basic authentication only; tenant/workspace/agent authorization is a P0 blocker |
| Audit trail | `audit_events`, export/signing and retention tools exist | Coverage is incomplete and most audit writes are not atomic with the operation they describe |
| Browser/API hardening | Security headers are enforced by middleware and tests | Baseline present |
| Data model | Append-only memory, CAS supersede, provenance, statuses and optional ciphertext for `memory_items.text` exist | Active-head semantics and sensitive-table encryption are incomplete |
| Conversation capture | Raw ledger, explicit curation endpoint and live pipeline runner exist | Retention policy semantics, identity provisioning and installed agent hooks are incomplete |
| Embeddings | Provider-neutral endpoints, async worker and Qdrant hydration exist | Dependency isolation, collection identity and multi-workspace reindex are P0 blockers |
| Memory LLM | Provider-neutral OpenAI-compatible contract, deterministic fallback and live runner exist | LLM output currently bypasses proposal/review and cannot be autonomous in production |
| OpenClaw/Hermes | Native adapter scaffolds, tests and live soak runner exist | Needs saved soak evidence from the deployed runtime versions |
| UI | React dashboard supports memory/vault editing, conflict decisions and a JSON walkthrough runner | Authenticated browser flow, endpoint egress policy and durable settings are incomplete |
| Testing | Unit/API tests, optional integrations, benchmark scripts, web build and load runner exist | PostgreSQL concurrency, failure isolation, authorization and target chaos/security evidence are missing |
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
   contract. An isolated local PostgreSQL 17 clean boot passed all 11 ledger
   integration scenarios, migration rerun and password rotation (old rejected,
   new accepted). A separate production-Compose smoke also initialized a fresh
   volume and completed all migrations through the two mounted Docker secrets.
   The same proof must still be executed and preserved on the target Docker
   runtime before this gate is closed.

2. **Identity provisioning exists, but identity-bound bootstrap is incomplete.**
   An operator-only, audited and idempotent endpoint now provisions an agent and
   optional owned thread atomically, refuses cross-scope ID reuse, and has API,
   service and optional PostgreSQL retain coverage. Agent keys intentionally
   cannot self-provision arbitrary identities. Native OpenClaw/Hermes installers
   still need an operator bootstrap step, and the remaining API-key binding
   blocker must be closed before safe automatic first-use registration.

3. **API keys are not bound to an identity boundary.**
   Authentication scopes are read/write/operator labels only; they are not
   bound to tenant, workspace or agent IDs. A client chooses those IDs in the
   request. `private`, `team` and `organization` visibility is not enforced as
   an authorization policy, so agent keys do not yet provide memory isolation.

4. **Superseded and archived heads can remain recallable.**
   Supersede appends a replacement without atomically demoting the previous
   head. PostgreSQL fallback and existing Qdrant points can return both values.
   Conflict review records an operator decision but retrieval does not apply
   it. Production needs one active-head policy across every candidate source.

5. **Encryption-at-rest coverage is incomplete.**
   pgcrypto protects `memory_items.text`, but provenance quotes, raw
   conversations, proposals/evidence, observations, checkpoints, audit metadata
   and PostgreSQL dumps can still contain plaintext. Production documentation
   must not describe `UAM_MEMORY_TEXT_ENCRYPTION_SCOPES=all` as full-database or
   backup encryption.

6. **Fail-soft recall is not implemented end to end.**
   Bootstrap synchronously connects Qdrant and retrieval calls each source
   without source-level failure isolation. A Qdrant or embedding outage can
   fail startup/recall instead of falling back to PostgreSQL. `/health` reports
   process liveness only and cannot prove PostgreSQL/Qdrant/NATS readiness.

7. **Workspace reindex is destructive outside its scope.**
   `EmbeddingService.reindex_all()` selects one workspace, then the Qdrant
   adapter deletes and recreates the shared collection. This removes vectors
   for other workspaces and leaves an empty index if reinsertion fails.

8. **PostgreSQL checkpoint CAS needs target concurrency evidence.**
   The invalid aggregate `FOR UPDATE` query has been replaced by a
   tenant/thread-scoped transaction advisory lock followed by an ordered head
   read. First saves now use the same compare-and-swap path with expected head
   zero, preventing the unique-violation race. Unit coverage and an optional
   two-writer PostgreSQL integration test exist; that test passed on the
   isolated PostgreSQL 17 clean boot. The real target run remains a required
   release artifact.

9. **Production UI authentication flow is incomplete.**
   `/ui` requires a bearer token when auth is enabled, but normal browser
   navigation and the React API client do not supply it. The shipped Caddy
   config does not establish an authenticated UI session or inject credentials.

10. **Model endpoint testing creates an SSRF boundary.**
    An operator can submit an arbitrary model base URL, which the server probes.
    Production needs an explicit endpoint allowlist/network egress policy and
    secrets must not be persisted as plaintext model settings JSON.

11. **Conversation retention policies do not implement their names.**
    Every call first appends a raw turn, including `curated_only`; there is no
    automatic expiry or deletion after curation. A privacy-sensitive caller
    cannot rely on the selected policy to prevent or bound raw transcript
    storage.

12. **LLM curation bypasses the proposal/evidence safety boundary.**
    `ConversationCurator` converts model JSON directly into a recallable memory
    item and uses generated summary text as its provenance quote. It should
    create an evidence-linked proposal, then require policy or operator
    acceptance before becoming durable truth. Proposal acceptance itself is a
    retention write followed by a separate status write, so a failure can leave
    durable memory while the proposal remains pending.

### P1 — reliability, scale and operations

1. Only embedding jobs have a deployed worker even though retain events request
   embedding, dedupe, graph and reflection work. Graph/reflection maintenance is
   not an automatic production pipeline and graph is not a recall source.
2. Outbox retry has no exponential backoff; brief outages can exhaust attempts
   rapidly. NATS poison messages have no bounded delivery/DLQ policy, stream
   size/age limits, authentication, TLS or replay workflow.
3. PostgreSQL opens a new connection per operation. The deployment has one API
   process and single-node PostgreSQL/Qdrant/NATS volumes, with no HA or safe
   horizontal-scaling design.
4. Backup covers PostgreSQL only and is not encrypted or signed. Restore drill
   checks schema presence, not source/restore row-count parity, decryption,
   recall, RLS or required Qdrant reindex after restore.
5. Embedding metrics exposed by the API describe the API process, while actual
   embedding work runs in another container. Worker failures can remain absent
   from the metrics used by alerts.
6. PostgreSQL lexical recall loads and decrypts the full workspace in Python
   instead of using the existing FTS/trigram indexes. Several list/export paths
   also lack production pagination.
7. Audit events are incomplete and separate from the transaction they describe.
   Denied requests, raw conversations, proposals, graph writes, reflect,
   reindex and checkpoints need complete status-aware audit coverage.
8. The application role has update/delete privileges over canonical and audit
   tables. Database-enforced append-only/tamper controls and migration checksums
   are required.
9. Idempotency keys are tenant-wide rather than workspace/operation scoped;
   identical keys can collide across independent agent workflows.
10. Published outbox events, processed-event IDs, raw conversations, proposals,
    idempotency records and checkpoint revisions do not have an installed data
    lifecycle policy.
11. Qdrant retrieval is dense-only in the actual worker path, multi-layer filter
    handling is incomplete, and existing collection model/dimension identity is
    not verified before use.
12. Conflict/reflection extraction is mostly deterministic English-pattern
    matching. Russian paraphrases and temporal facts need evaluated multilingual
    extraction with provenance and operator-decision precedence.
13. Runtime model settings are persisted only when an optional host path is
    configured; otherwise the UI reports a desired config that disappears on
    restart. Persisted settings contain provider keys in plaintext.
14. `index_stale` is not computed from outbox/index state or exposed as an API
    invariant, so agents cannot distinguish complete recall from a lagging
    vector index.
15. Worker logs are unstructured, worker-specific Prometheus metrics are not
    served by the worker, and readiness does not expose dependency state or
    build/deployment identity validation as an operational gate.

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
- supersede/archive/conflict active-head recall in PostgreSQL and Qdrant;
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
   - TLS or VPN/reverse proxy in front of any non-local deployment. The
     repository ships a Caddy example and `scripts/deployment_preflight.py`;
     production evidence requires the deployed host to expose HTTPS/proxy only,
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

1. Prove the implemented application-role provisioning on a fresh strict
   production boot, including login and password rotation.
2. Prove agent/thread provisioning on PostgreSQL and wire the operator bootstrap
   into OpenClaw/Hermes installation; then add real checkpoint concurrency
   coverage.
3. Bind API principals to tenant/workspace/agent visibility policy.
4. Enforce active-head recall semantics for supersede/archive/conflict review in
   PostgreSQL and Qdrant.
5. Implement source-isolated fail-soft recall, dependency readiness and safe
   multi-workspace reindex.
6. Complete encryption/backup coverage and close the authenticated UI/SSRF
   boundaries.
7. Install schedules, immutable storage, monitoring and alert routing; then run
   native-agent, conversation, load, UI, embedding and LLM target gates.
8. Seal every release with the signed content-addressed v2 evidence manifest,
   then add OCI SBOM, provenance, vulnerability scanning and image signing.
