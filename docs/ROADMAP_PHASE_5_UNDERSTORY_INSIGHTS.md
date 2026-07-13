# Phase 5 — vault health and explainable memory traversal

## Purpose

This phase adopts a small, safe subset of the ideas reviewed in
[`thecodacus/understory`](https://github.com/thecodacus/understory).  Obelisk
remains a local, self-hosted memory server whose canonical state is PostgreSQL.
The Markdown vault stays a human-readable projection and an explicitly
controlled editing interface; it is not the system of record.

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

## Implementation status — 2026-07-13

The first four delivery items are implemented behind tenant/workspace-scoped
operator APIs and covered by unit or API tests:

- deterministic vault health is served at
  `GET /v1/workspaces/{workspace_id}/vault/health`;
- every successful recall has an audit-backed, redacted `replay_id` and a
  scoped replay endpoint;
- the bounded `seed` endpoint is available for a new agent session and does
  not replace task-scoped recall;
- the vault UI/API exposes `editable_content` rather than vectors, Qdrant
  payloads, tenant IDs, or provenance sections; saving still becomes a CAS
  superseding revision and background reindex.

The live operator walkthrough additionally proves the complete local flow:
retain and recall a test note, persist a conflict decision, select a real
editable memory note (never the README preview), archive it through the API,
probe the configured embedding endpoint, request reindexing, and read metrics.
The walkthrough treats an `embedding` field in an editable memory note as a
failure.  This is a release gate, not a promise that all browser usability or
multi-node production concerns are complete.

Still pending from this phase is a purpose-built, field-level/section-level
editor patch endpoint.  Until it exists, the supported editing mechanism is a
full note body submitted through the existing CAS vault-import path.

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
