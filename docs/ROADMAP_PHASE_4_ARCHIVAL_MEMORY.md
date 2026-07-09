# Phase 4 roadmap: archival-grade eternal memory

This phase turns Obelisk Memory from a strong self-hosted memory server
into an archival memory plane for long-running agents and agent swarms.

The ideas below were informed by the public `ArchiveOfHeresy` architecture in
`AdmiralPinguin/shushunya`, but they are intentionally adapted to this project:
Docker-first deployment, Postgres/Qdrant/NATS, explicit permissions, revisions,
human-readable vault projections and deep OpenClaw/Hermes integrations.

We should not copy ArchiveOfHeresy names or file-based implementation. The
Obelisk Memory roles are:

- **Навигатор памяти** (`Memory Navigator`) — pre-answer recall planner. It
  reads candidate memory layers and returns a compact, grounded context bundle.
- **Куратор памяти** (`Memory Curator`) — post-answer maintenance worker. It
  classifies, consolidates, supersedes and schedules memory updates.
- **Шлюз памяти** (`Memory Gateway`) — safe read/proposal contract for agents.
  Agents do not directly mutate durable memory.
- **Смотритель графа** (`Graph Steward`) — graph evidence and relation
  maintenance worker.
- **Хранитель фокуса** (`Focus Keeper`) — manages active working topics and
  short-lived task context.

Memory LLM requirement: Навигатор памяти, Куратор памяти and future graph
extraction workers must use Qwen through the DGX Spark `.10` OpenAI-compatible
endpoint configured by `UAM_MEMORY_LLM_*`. See
[DGX_SPARK_MEMORY_LLM.md](DGX_SPARK_MEMORY_LLM.md). This is separate from the
Jina embedding endpoint on `.10:8002`. The runtime adapter is
`MemoryLLMClient` in `src/memory_plane/adapters/llm.py`; workers should call it
through `chat_json()` when they need machine-checkable curation decisions.

## Design principles

1. **Do not inject raw memory by default.** Vector and graph results are
   candidate evidence, not prompt text. The Навигатор must compact, rank and
   cite them before an agent sees them.
2. **Memory writes are proposals.** OpenClaw, Hermes and future agents submit
   proposed changes with evidence. The server validates, audits and decides how
   to store them.
3. **Current facts beat old facts.** Every durable memory record needs status,
   revision, provenance and conflict/supersession links.
4. **Namespaces are safety boundaries.** Agent work, user preferences, project
   facts, experiments and system diagnostics must not collapse into one bucket.
5. **Graph edges require evidence.** A beautiful graph without source evidence
   creates false confidence.
6. **Fail soft.** If embeddings, graph extraction or the Навигатор fail, the
   agent request should continue with reduced memory support where possible.
7. **Database is canonical; vault is projection.** Markdown/vault views are for
   humans. Editing them must create revisions and reindex under the hood.

## WP-22 Memory Navigator

Build a first-class pre-answer recall planner.

Planned endpoint:

```http
POST /v1/recall/plan
```

Responsibilities:

- collect candidates from focus, durable memory, vector search, graph neighbors
  and recent session context;
- apply budgets by layer, namespace and agent type;
- return compact context, source IDs, confidence and warnings;
- run a grounding check: if generated context is not supported by candidate
  sources, drop or demote it;
- preserve provenance so the UI can show “why this was recalled”;
- support request flags such as `focus_enabled`, `vector_enabled`,
  `graph_enabled`, `include_archived`, `max_context_tokens`;
- default to no raw vector/graph prompt injection.

Example output:

```json
{
  "context": "User prefers Russian UI. OpenClaw and Hermes should use the shared memory server through plugin-level lifecycle hooks.",
  "confidence": 0.86,
  "sources": [
    {"memory_id": "mem_...", "status": "active", "score": 0.91},
    {"edge_id": "edge_...", "evidence_memory_id": "mem_..."}
  ],
  "warnings": []
}
```

## WP-23 Memory Gateway and proposals

Add a safe gateway for deep agent integrations.

Initial implementation note: the project now has `POST /v1/memory/proposals`
and `GET /v1/memory/proposals`. Proposals are sanitized by PrivacyGuard, stored
with namespace/requester/evidence/confidence/importance, and remain separate
from recallable `MemoryItem` records until curated. It also has
`POST /v1/memory/proposals/{proposal_id}/accept` and `/reject`: accept creates a
normal append-only memory item with `proposal://{proposal_id}` provenance, reject
stores the review decision only.

Read endpoints:

```http
GET /v1/memory/catalog
POST /v1/memory/search
GET /v1/memory/{id}
GET /v1/memory/events
```

Write endpoint:

```http
POST /v1/memory/proposals
```

Proposal schema:

```json
{
  "tenant_id": "00000000-0000-0000-0000-000000000000",
  "workspace_id": "00000000-0000-0000-0000-000000000000",
  "namespace": "openclaw",
  "agent_id": "openclaw-01",
  "target": "auto",
  "proposal": "User prefers Russian interface labels.",
  "evidence": "User explicitly complained that the UI was not in Russian.",
  "confidence": 0.91,
  "importance": 4
}
```

Rules:

- proposals are stored as auditable events;
- no agent receives direct durable-write access;
- oversized payloads are trimmed with metadata;
- secrets and PII guards run before storage;
- accepted proposals become normal memory revisions;
- ambiguous proposals enter conflict review.

## WP-23A Conversation Ledger

Add an explicit raw conversation ledger for runtimes that want complete
transcript retention.

Current behavior is selective: the server stores only what enters
`/v1/memory/retain`, `/v1/ingest/*`, vault import or agent integration hooks.
It does not automatically intercept every user/assistant message.

Archival mode should support two separate paths:

1. **Raw transcript ledger** — immutable append-only turns, useful for audit,
   replay and future reprocessing.
2. **Curated memory** — distilled durable facts, preferences, decisions,
   summaries and graph evidence.

Planned endpoint:

```http
POST /v1/conversations/turns
```

Suggested schema:

```json
{
  "tenant_id": "00000000-0000-0000-0000-000000000000",
  "workspace_id": "00000000-0000-0000-0000-000000000000",
  "namespace": "hermes",
  "agent_id": "agent uuid",
  "thread_id": "thread uuid",
  "messages": [
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."}
  ],
  "retention_policy": "raw_and_curated",
  "source_kind": "hermes-memory-provider"
}
```

Rules:

- raw ledger entries are not injected into prompts directly;
- secrets/PII policy applies before long-term retention;
- raw turns can be reprocessed later by the Куратор;
- operators can disable raw transcript retention per namespace;
- curated memories keep their normal status/revision/provenance model.

## WP-24 Namespaces and visibility policy

Extend memory scope beyond tenant/workspace with explicit namespaces.

Suggested fields:

- `tenant_id`
- `workspace_id`
- `namespace`
- `agent_id`
- `session_id`
- `source`
- `visibility`

Initial namespaces:

- `user` — user preferences and durable personal operating rules;
- `project` — durable project facts and decisions;
- `openclaw` — OpenClaw-specific tool/plugin/task memory;
- `hermes` — Hermes-specific workflow memory;
- `coding` — codebase and implementation memory;
- `research` — external research, papers, audits and benchmarks;
- `system` — deployment, health and operational state;
- `sandbox` — experimental or temporary memories.

Default visibility examples:

- OpenClaw sees `user`, `project`, `openclaw`, `coding`;
- Hermes sees `user`, `project`, `hermes`, `research`;
- generic agents see `user`, `project`;
- maintenance workers can read broader scopes but must write with provenance.

## WP-25 Focus Keeper

Add a working-memory layer for active topics.

The focus layer is not eternal memory. It is a bounded current-topic context
that helps agents continue work without replaying the whole archive.

Suggested storage:

- `focus_sessions`
- `focus_items`
- `focus_memberships`

Capabilities:

- create, pause, resume and close focus sessions;
- bind focus to namespace/agent/project;
- keep a bounded number of active focus sessions;
- surface active focus in the UI;
- let the Навигатор read focus before vector/graph expansion;
- expire stale focus unless pinned.

## WP-26 Memory Curator

Add an asynchronous post-answer worker.

The Куратор should consume outbox/NATS events and maintain durable memory after
agent/user turns.

Initial implementation note: the project now has an explicit deterministic
curation bridge, `POST /v1/conversations/turns/{turn_id}/curate`, that converts
one raw transcript turn into a recallable memory item with
`conversation://{turn_id}` provenance. The full WP-26 worker should build on
this path instead of inventing a second write pipeline.

Responsibilities:

- classify each exchange with epistemic labels:
  - `fact`
  - `preference`
  - `opinion`
  - `joke`
  - `mistake`
  - `task`
  - `decision`
  - `unverified`
- detect whether a new item supersedes an older item;
- create conflict cases when two active memories disagree;
- promote stable facts into durable memory;
- demote stale or noisy memories;
- schedule embedding reindex;
- emit audit events for every maintenance action.

This must be asynchronous. Do not introduce a single in-process request lock.

## WP-27 Durable wiki layer

Add a curated durable-memory projection for stable project knowledge.

This is conceptually similar to a wiki, but the canonical source remains the
database. Markdown/vault files are projections.

Durable pages should include:

- current facts;
- active decisions;
- superseded decisions;
- open questions;
- next steps;
- linked memories and evidence.

When a user edits a vault file through the UI:

1. create a new memory revision;
2. preserve the old revision;
3. recompute embeddings;
4. update graph links;
5. mark superseded records where needed;
6. update the markdown projection.

## WP-28 Evidence-backed graph

Upgrade graph memory from visualization to a trustworthy reasoning layer.

Every node and edge should have:

- `source_memory_id`
- `evidence_text`
- `confidence`
- `created_by`
- `verified_by`
- `status`
- `valid_from`
- `valid_until`

Core relation types:

- `supports`
- `contradicts`
- `supersedes`
- `derived_from`
- `same_entity`
- `owned_by_agent`
- `uses_tool`
- `belongs_to_project`

Graph retrieval rules:

- graph edges without evidence are hidden from default recall;
- low-confidence edges are visualization-only until reviewed;
- conflicting edges are shown in conflict review;
- UI graph nodes must link back to source memories.

## WP-29 Agent lifecycle integration

Deep OpenClaw/Hermes integration should be plugin-level, not a loose MCP/skill
call.

Lifecycle endpoints:

```http
POST /v1/agent/session/start
POST /v1/agent/context
POST /v1/agent/recall
POST /v1/agent/events
POST /v1/agent/propose-change
POST /v1/agent/session/end
```

Session start returns:

- active focus;
- namespace policy;
- compact project context;
- user preferences relevant to the agent;
- write/proposal permissions;
- memory health warnings.

Session end submits:

- completed task summary;
- changed files/artifacts;
- errors and limitations;
- proposed durable updates;
- graph relation candidates.

## WP-30 Memory quality reports

Add periodic reports that evaluate whether memory helped or harmed.

Report should answer:

- which recalls were used;
- which recalls were noisy;
- which memories are stale;
- which conflicts remain unresolved;
- which namespaces are growing too fast;
- which graph edges have weak evidence;
- which agent generated the most rejected proposals.

Outputs:

- UI dashboard card;
- markdown report under vault projection;
- machine-readable JSON for tests/ops.

## WP-31 Security and operations hardening for archival mode

Additional requirements before calling this production-grade:

- namespace-aware API keys;
- per-agent scopes;
- audit trail for every read and proposal;
- local-only default bind address;
- reverse-proxy/TLS examples;
- key rotation docs;
- memory export/import with redaction;
- restore drill for Postgres, Qdrant and vault projection;
- rate limits for proposal spam;
- poison-memory tests;
- prompt-injection tests for Навигатор and Куратор.

## Implementation order

Recommended order:

1. WP-23 Memory Gateway and proposal API.
2. WP-23A Conversation Ledger.
3. WP-24 Namespaces and visibility policy.
4. WP-22 Memory Navigator with grounded compact recall.
5. WP-26 Memory Curator with epistemic labels.
6. WP-25 Focus Keeper.
7. WP-28 Evidence-backed graph.
8. WP-29 OpenClaw/Hermes lifecycle integration.
9. WP-27 Durable wiki/vault projection.
10. WP-30 Memory quality reports.
11. WP-31 archival security and operations.

## Explicit non-goals

- Do not copy ArchiveOfHeresy persona-specific prompts, model paths or local
  Shushunya/TG/Warmaster assumptions.
- Do not use hashed token embeddings as the primary semantic layer.
- Do not add a global request lock.
- Do not allow direct agent mutation of durable memory records.
- Do not treat graph edges as facts without evidence.
- Do not make Markdown files the canonical storage layer.
