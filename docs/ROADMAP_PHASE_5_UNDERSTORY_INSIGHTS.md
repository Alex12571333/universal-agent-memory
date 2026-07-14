# Phase 5 — vault health and explainable memory traversal

## Purpose

This phase adopts a small, safe subset of the ideas reviewed in
[`thecodacus/understory`](https://github.com/thecodacus/understory).  Obelisk
remains a local, self-hosted memory server whose canonical state is PostgreSQL.
The Markdown vault stays a human-readable projection and an explicitly
controlled editing interface; it is not the system of record.

The implementation review was refreshed on 2026-07-14 against upstream commit
`0a387c3c68d29253fdb74378390eea7edf0e3137`.  The reviewed repository still has
no source-code license, so this phase copies no code, icons, text, or assets.
It implements independently only the architectural behaviours listed below.

The phase has two goals:

1. make a vault's structural health visible without an LLM; and
2. make recall and mutations explainable from existing, tenant-scoped audit
   evidence.

## Adopted principles

### Deterministic conformance before model judgement

Structural checks must run from canonical records and typed graph edges.  A
model may later propose a repair, but it must never invent an edge or write a
durable fact merely to make a graph look complete.

The first implementation is a read-only, tenant/workspace-scoped vault-health
report.  It detects:

- graph edges whose endpoints or provenance item no longer exist in the same
  workspace;
- reflection observations whose evidence items are missing or outside the
  workspace;
- recallable memory heads without a typed graph edge or active observation
  evidence (an *unlinked* item, not an error);
- revision-chain inconsistencies visible from the canonical ledger.

`unlinked` is diagnostic only.  It is deliberately not an automatic repair
queue: isolated facts can be correct and useful.

### Explainable traversal, not a parallel trace store

Recall replay is now backed by Obelisk's existing tenant-scoped audit trail and
canonical trace IDs.  A successful recall stores a redacted request fingerprint,
candidate/context metrics, sources and selected IDs, then returns `replay_id`.
The operator endpoint resolves those IDs against the canonical ledger without
returning prompt text, memory text or transcripts.  The remaining extension is
to connect approved proposal/mutation decisions to the same lineage.

### Bounded seed overview for integrations

An opt-in seed endpoint now returns a compact deterministic inventory from
shared recallable heads in the `core`, `working` and `procedural` layers. It is
budgeted to 128–4096 estimated tokens, excludes private/thread data, and never
replaces a scoped `recall` call. Recent-change summaries remain a later feature.

### Targeted Markdown edits remain CAS operations

The vault editor should ultimately support an explicit section/field patch.
Every edit still becomes an append-only superseding revision, carries original
provenance, emits an outbox event, and is re-embedded asynchronously.  It must
not write directly into the exported vault folder.

## Explicit non-adoptions

- No in-process mutation queue: multi-agent concurrency remains PostgreSQL CAS,
  row/advisory locks, transactional outbox, and idempotent consumers.
- No file-scan search: hybrid lexical + Qdrant dense retrieval remains the
  retrieval path.
- No Markdown-as-canonical-state migration.
- No LLM-only contradiction handling, auto-linking, or automatic acceptance of
  model-authored durable facts.
- No unauthenticated server or cross-tenant filesystem browsing.

## Provenance and licensing boundary

This is an independent design inspired by a public repository.  At review time
the upstream repository did not include a `LICENSE` file, so Obelisk must not
copy its source code.  Only the general architectural ideas described here are
used.

## Delivery sequence

1. Tenant-scoped read-only vault-health lint and API, with in-memory and
   PostgreSQL coverage.
2. Operator UI health summary and links to actionable diagnostics.
3. Audit-backed recall replay API and integration contract. Mutation lineage UI
   remains a later operator feature.
4. Opt-in bounded integration seed, evaluated against context budgets.
5. CAS-backed targeted editor patches, including concurrency and reindex tests.

## Upstream-to-Obelisk decision matrix

| Understory behaviour | Obelisk decision | Production boundary |
| --- | --- | --- |
| deterministic orphan/broken-link lint | adopted as canonical vault health | PostgreSQL IDs, typed edges, provenance and tenant isolation replace Markdown link scanning |
| query-path replay | adopted through redacted audit replay | no prompt, transcript, API key or memory body is stored in replay metadata |
| session-start memory seed | adopted as an opt-in bounded seed | only shared recallable heads; private/thread data is excluded and scoped recall remains mandatory |
| targeted section/frontmatter patch | adopt next | CAS append-only revision, provenance preservation, outbox and asynchronous re-embedding are mandatory |
| force-directed graph and replay overlay | UI idea retained | graph data comes from canonical typed edges and remains tenant/workspace scoped |
| Markdown as canonical state | rejected | Markdown remains a human-readable projection; PostgreSQL is authoritative |
| in-process mutation queue | rejected | PostgreSQL CAS, transactional outbox and idempotent consumers support concurrent agents |
| model-driven auto-link/repair | rejected | an LLM can propose a repair but deterministic validation and evidence policy decide durability |
| literal file scan | rejected | lexical PostgreSQL search plus dense Qdrant retrieval remains the supported path |
| MCP-only integration | rejected as primary integration | OpenAI-compatible/agent-native hooks and SDKs remain primary; MCP is optional interoperability |

## Implementation status — 2026-07-14

All five delivery items are implemented behind tenant/workspace-scoped
operator APIs and covered by unit or API tests:

- deterministic vault health is served at
  `GET /v1/workspaces/{workspace_id}/vault/health`;
- every successful recall has an audit-backed, redacted `replay_id` and a
  scoped replay endpoint;
- the bounded `seed` endpoint is available for a new agent session and does
  not replace task-scoped recall;
- the vault UI/API exposes `editable_content` rather than vectors, Qdrant
  payloads, tenant IDs, or provenance sections; saving still becomes a CAS
  superseding revision and background reindex;
- targeted body/section/confidence patches now use a dedicated CAS endpoint,
  reject system-managed sections and stale revisions, derive an idempotency key,
  and queue only the new revision through the transactional outbox.

The live operator walkthrough additionally proves the complete local flow:
retain and recall a test note, persist a conflict decision, select a real
editable memory note (never the README preview), archive it through the API,
probe the configured embedding endpoint, request reindexing, and read metrics.
The walkthrough treats an `embedding` field in an editable memory note as a
failure.  This is a release gate, not a promise that all browser usability or
multi-node production concerns are complete.

The full vault-import path remains available for signed bulk/offline workflows.
The web editor uses the narrower targeted endpoint for applied edits and keeps
the import planner only for explicit dry-run validation.

Real PostgreSQL 17 CAS/idempotency behaviour was also validated in an isolated
container on the `.14` agent node; see
[target vault patch validation](TARGET_VAULT_PATCH_VALIDATION_2026_07_14.md).

## Automated curation delivery note

Raw conversation turns now emit a redacted
`conversation.turn.appended.v1` transactional-outbox event.  The maintenance
worker consumes it asynchronously and runs the existing evidence-bound curator.
This keeps the agent request path short, survives an API restart between write
and processing, and never injects raw transcript text into ordinary recall.
Every consumer that reads the raw turn must receive the same pgcrypto settings
as the API and relay; the local advanced Compose profile enforces that parity.

The curator may use the configured bounded OpenAI-compatible maintenance model,
but its output remains an evidence-linked proposal by default.  Automatic
acceptance is intentionally limited to the existing high-confidence,
source-quoted, non-temporal policy.  A model-produced statement such as a
changed preference therefore cannot silently overwrite a durable fact.  The
outbox event and curation result are observable through worker logs/audit and
are suitable for a target-runtime lifecycle probe.

## Acceptance criteria

- A health request cannot inspect another tenant or workspace.
- It is deterministic and invokes no LLM, embedding model, or graph extractor.
- Broken canonical references are errors; unlinked memory is a warning.
- The report does not expose protected raw conversation content or vector data.
- The existing vault export/import, CAS supersede and outbox/indexing behaviour
  remain unchanged and covered by regression tests.

## Second-pass source audit — 2026-07-14

The follow-up review inspected the implementation rather than relying only on
the upstream README. The inspected upstream tree was commit
`0a387c3c68d29253fdb74378390eea7edf0e3137`, including:

- `packages/core/src/okf/bundle.ts`, `knowledge-base.ts`, `graph.ts`,
  `lint.ts`, `search.ts` and `validate.ts`;
- `packages/core/src/agent/trace.ts`, `tools.ts`, `agent.ts` and
  `system-prompt.ts`;
- `packages/server/src/mcp/seed.ts`, `mcp/server.ts`, `mcp/http.ts`,
  `api/browse.ts` and `index.ts`;
- the React graph/traversal UI and the repository tests.

This review confirms that the useful ideas are architectural, not code to copy.
The upstream repository still exposes no source-code license file. Obelisk
therefore keeps a clean-room implementation boundary.

### What Understory does well

1. One deterministic write boundary enforces path confinement, required
   frontmatter, generated indexes and an append-only human log after every
   mutation.
2. A compact session seed gives an agent a reason to query memory without
   dumping the entire knowledge base into its prompt.
3. Query traces record the actual sequence of search/read/write operations and
   make the path visible in the graph UI.
4. Graph lint distinguishes broken links from orphaned concepts and makes
   maintenance measurable.
5. Targeted section patches reduce accidental full-document rewrites.

### What must not be copied into Obelisk production

1. The default HTTP server has no tenant/authentication middleware around its
   browse, chat and MCP routes and reflects request origins through CORS.
2. All mutations share one in-process promise queue. It cannot coordinate
   multiple replicas and does not replace database CAS/transactions.
3. Search is a full Markdown scan with literal term scoring. It is unsuitable
   for large multilingual memory and is explicitly weaker than Obelisk's
   PostgreSQL/Qdrant hybrid retrieval.
4. Graph repair and contradiction handling depend on an LLM prompt. A model can
   suggest changes, but Obelisk must require canonical evidence and keep the
   result non-recallable until deterministic policy or review accepts it.
5. Best-effort Git autocommit is not an atomic durability boundary: a memory
   write can succeed while its commit fails.
6. File-backed traces are useful telemetry but are not tenant-isolated,
   release-bound durable audit evidence.

### Verified overlap in Obelisk

| Capability | Obelisk status | Stronger production property |
| --- | --- | --- |
| plain Markdown inspection | implemented | PostgreSQL remains authoritative; signed export and CAS import |
| graph lint | implemented | typed edges, evidence IDs, revision-chain checks and tenant RLS |
| session seed | implemented | opt-in token budget and private/thread exclusion |
| targeted edits | implemented | append-only supersede, expected revision, outbox and re-embedding |
| query replay | partially implemented | durable redacted audit event instead of raw query/answer files |
| create-vs-enrich policy | implemented in proposal/curation boundary | evidence-linked proposal is not recallable before acceptance |
| concurrent mutations | implemented | PostgreSQL CAS, idempotency, transactional outbox and worker leases |

## Phase 5.1 implementation backlog

### P1 — redacted retrieval traversal

Extend the existing recall replay with ordered, bounded steps:

1. each candidate source attempted;
2. number returned by the source;
3. number surviving tenant/workspace/scope/status/temporal policy;
4. optional dependency failure type without endpoint/error text;
5. unique candidates entering fusion, candidates above threshold and final
   selected count.

The trace must never store the raw query, candidate text, prompt, answer,
credential, endpoint or worker identity. It is persisted inside the existing
tenant-scoped audit event and returned from the operator replay endpoint. The
web recall panel should show the same compact pipeline for the current request.

### P1 — evidence-bound graph maintenance proposals

Vault health may produce a deterministic repair *plan*, but not a repair. An
unlinked item is not necessarily wrong. Any LLM-generated relationship must be
stored as a non-recallable `graph` proposal containing source/destination IDs,
typed relation, provenance ID and rationale. Acceptance must revalidate both
endpoints and evidence in one tenant/workspace before writing the edge.

### P2 — portable knowledge projection

An optional OKF-compatible export can improve portability, but it remains a
projection. Stable Obelisk IDs, revision/status/provenance and signed manifest
must survive round trips. `index.md` and activity logs may be generated during
export; they must never become a second source of truth.

### P2 — traversal overlay and maintenance UX

The force-directed graph can overlay an approved replay by highlighting
selected canonical IDs and showing numbered aggregate stages. It must not infer
or draw relationships merely because two memories appeared in one recall.

## Phase 5.1 acceptance criteria

- retrieval traversal is deterministic, ordered and bounded;
- optional source failure is visible without error text or endpoint data;
- tenant/workspace/scope/status filters are applied before counts enter fusion;
- replay remains readable after memory supersession without copying old text;
- the browser renders the current trace in Russian and remains functional when
  an older server omits the optional field;
- no Understory code, assets, prompts or icons are copied.

## Phase 5.1 implementation status — 2026-07-14

The redacted retrieval traversal is implemented. `RecallResult` now carries a
bounded ordered trace for successful and degraded candidate sources plus the
final weighted-fusion stage. The API persists it inside the existing durable
recall audit event, the scoped replay endpoint decodes only the fixed telemetry
schema, and the React recall panel renders the current path in Russian. Tests
prove that an optional dependency's exception message is not retained and that
the replay still contains no raw query or memory text.

The PostgreSQL 17 target round trip through the non-superuser runtime role is
recorded in
[target retrieval traversal validation](TARGET_RETRIEVAL_TRAVERSAL_VALIDATION_2026_07_14.md).

Evidence-bound graph maintenance proposals, the optional portable projection
and the graph replay overlay remain separate follow-up work. They are not
claimed as implemented by this phase.

The first graph-maintenance safety boundary is now enforced: a proposal whose
target is `graph` cannot pass through the generic memory-acceptance endpoint and
become recallable prose. It remains open and non-recallable until the dedicated
path can atomically validate source, destination, typed relation and provenance
before writing a canonical edge. This closes the unsafe compatibility fallback
without pretending that graph proposal acceptance is complete.
