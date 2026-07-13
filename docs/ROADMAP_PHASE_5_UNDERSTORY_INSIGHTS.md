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

The UI should eventually replay a recall/curation path: request, candidate
sources, selected trace IDs, context-package budget, subsequent proposal or
mutation, and audit event IDs.  It will use Obelisk's existing audit trail and
trace IDs with retention and tenant isolation.  It must not add a world-readable
directory of prompts, model outputs, or raw conversation content.

### Bounded seed overview for integrations

A new agent session may receive a small, generated inventory of the workspace
(approved layers, active heads, recent changes) so the agent knows when to call
memory.  It must be opt-in, budgeted, derived from recallable heads only, and
never replace a scoped `recall` call.

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
3. Audit-backed recall/mutation replay UI.
4. Opt-in bounded integration seed, evaluated against context budgets.
5. CAS-backed targeted editor patches, including concurrency and reindex tests.

## Acceptance criteria

- A health request cannot inspect another tenant or workspace.
- It is deterministic and invokes no LLM, embedding model, or graph extractor.
- Broken canonical references are errors; unlinked memory is a warning.
- The report does not expose protected raw conversation content or vector data.
- The existing vault export/import, CAS supersede and outbox/indexing behaviour
  remain unchanged and covered by regression tests.
