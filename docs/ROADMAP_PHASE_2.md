# Phase 2 roadmap: production-grade eternal memory

Этот документ описывает следующий этап после foundation work packages. Цель —
довести Obelisk Memory до уровня условно вечной памяти для разных
агентов: устойчивой, проверяемой человеком, глубоко интегрируемой в agent
runtimes и не завязанной на один UI или один LLM provider.

## Product target

Система должна быть не просто HTTP API с embeddings, а memory operating layer:

```text
ingestion → normalization → storage → indexing → reflection
→ conflict resolution → retrieval → human review → vault/export → backup
```

Ключевой принцип: вечная память не обязана помнить всё одинаково. Она должна
уметь различать актуальные знания, историю, гипотезы, ошибки, preferences,
инструкции, evidence и устаревшие утверждения.

## Memory invariants

- Raw evidence is append-only.
- Исправления создаются через `supersede`, а не destructive overwrite.
- Любой derived fact обязан иметь `evidence_ids` или provenance.
- Retrieval по умолчанию отдаёт active/non-rejected knowledge, но история
  остаётся доступной.
- Человек должен иметь читаемый способ увидеть и поправить память.
- Индексы Qdrant/graph/FTS перестраиваемы; PostgreSQL остаётся source of truth.
- Смена embedding model не должна ломать старую память: индексы версионируются.
- Secrets/PII не должны попадать в вечную память без policy decision.

## WP-11 Obsidian/vault mode — complete

Сделать human-readable слой поверх PostgreSQL: export/import/sync в Markdown
vault, открываемый в Obsidian или любом редакторе.

Предлагаемая структура:

```text
vault/
  core/
  semantic/
  episodic/
  procedural/
  social/
  reflections/
  conflicts/
  agents/
  threads/
  documents/
```

Каждая memory/observation — Markdown файл с frontmatter:

```md
---
id: mem_...
layer: semantic
kind: fact
status: active
confidence: 0.84
valid_from: 2026-07-06
valid_to:
supersedes:
superseded_by:
source_kind: api
agent_id:
thread_id:
labels: [project, architecture]
---

Основной язык проекта — Python.

## Evidence
- [[document-...]]
- quote: ...

## Related
- [[mem-...]]
```

Acceptance:

- deterministic export from PostgreSQL to vault; ✅
- stable file names; ✅ `mem-<uuid>.md` / `obs-<uuid>.md`
- backlinks for `supersedes`, evidence, conflicts and related memories; partial
- frontmatter round-trip parser; ✅
- safe import path that creates revisions, not destructive overwrites; ✅
- docs for opening the vault in Obsidian. ✅

## WP-12 Real embedding providers — complete

The embedding pipeline supports deterministic test embeddings for CI and real
providers for deployment. Production deployments must use a real provider and
must keep the vector dimension consistent across Qdrant, the worker, and the
configured embedding endpoint.

Providers:

- OpenAI embeddings;
- Ollama embeddings;
- local sentence-transformers or TEI/vLLM-compatible HTTP endpoint.

Required config:

```dotenv
UAM_EMBEDDING_PROVIDER=openai|ollama|tei|fake
UAM_EMBEDDING_MODEL=...
UAM_EMBEDDING_DIM=...
UAM_EMBEDDING_BASE_URL=...
UAM_QDRANT_PAYLOAD_TEXT=false
```

Acceptance:

- provider selected by env; ✅
- model/dimension validation before indexing; ✅
- embedding metadata stored with indexed payload; ✅
- full reindex job after model change; ✅ existing `/reindex`
- tests for provider selection and dimension mismatch; ✅
- Qdrant can store only vectors/filter metadata while hydrating text from the
  PostgreSQL ledger. ✅

## WP-13 Conflict resolver and review inbox — complete

Reflection v2 can mark stale/conflicting observations. The next step is an
explicit conflict case model.

Conflict case shape:

```text
subject: Alpha release
predicate: date
candidates:
  - value: July 15
    evidence: [...]
    status: stale
  - value: July 16
    evidence: [...]
    status: active
decision:
  winner: July 16
  reason: newer explicit supersede + higher-trust source
```

Resolution policy examples:

- user-confirmed > agent-inferred;
- direct document quote > model summary;
- explicit supersede > older fact;
- core approved memory > low-confidence episodic memory;
- newest is not automatically correct when confidence/source is weak.

Acceptance:

- conflict inbox API; ✅ `GET /v1/workspaces/{id}/conflicts`
- deterministic resolver with inspectable reason strings; ✅
- human override endpoint; ✅ `PUT /v1/workspaces/{id}/conflicts/{case_id}/decision`
- review status persisted; ✅ `conflict_reviews` table / in-memory repository
- retrieval can demote unresolved/disputed memories. pending retrieval policy hook

Current behavior:

- raw memory evidence remains append-only;
- conflict cases are derived from semantic memories grouped by subject/predicate;
- candidates are marked `active`/`stale` based on newest evidence;
- accepted/overridden/dismissed decisions hide cases from the default inbox;
- `include_resolved=true` shows historical reviewed cases.

## WP-14 Human memory UI — complete

Add a simple local web UI. It does not need to become SaaS; it is an operator
console for a self-hosted Docker deployment.

Views:

- memory list/search;
- filters by layer/kind/status/agent/thread/label;
- memory detail with provenance and revision chain;
- conflict inbox;
- approve/reject/promote to core;
- mark stale/rejected;
- trigger reflect/reindex/backup;
- graph view.

Acceptance:

- local UI served by the memory server or companion container; ✅ `/ui`
- API-key protected; ✅ inherited by global middleware
- edits use `supersede`; ✅ UI does not perform destructive writes
- never performs direct destructive update of memory rows. ✅

Current operator console:

- list memory rows by workspace/layer;
- semantic recall preview;
- conflict inbox view;
- reflect/reindex triggers;
- no separate SaaS/frontend build pipeline.

## WP-15 Native OpenClaw/Hermes integrations — complete

Goal: integrate as a plugin/runtime extension, not merely as a skill or MCP
server. MCP remains useful as an optional bridge, but deep integration should hook
into the agent lifecycle.

### Why plugin-level integration

Skill/tool/MCP integration usually means the agent must decide to call memory.
Production memory should be present before, during and after agent execution:

```text
before run:
  load identity/core/task/thread context
during run:
  record observations, tool traces, decisions, errors
after run:
  summarize, retain, update checkpoints, emit reflections
background:
  dedupe, embed, resolve conflicts, export vault
```

This is deeper than “call a tool named memory_recall”.

### Integration architecture

Create a small `agent-integrations/` area:

```text
agent-integrations/
  openclaw/
    plugin/
    README.md
  hermes/
    universal_agent_memory/
    README.md
  shared/
    lifecycle.py
    config.py
    client.py
    identity.py
```

Shared lifecycle hooks:

- `before_agent_run(context) -> ContextPackage`
- `after_agent_message(message, trace) -> RetainCommand[]`
- `after_tool_call(tool_name, args, result, error) -> RetainCommand[]`
- `on_checkpoint(thread_state) -> Checkpoint`
- `on_error(error, trace) -> error memory`
- `on_run_complete(summary) -> reflection/reindex trigger`

Memory scopes:

- `core`: identity, policies, durable project rules;
- `working`: current task/checkpoint/open loops;
- `episodic`: run events and tool traces;
- `semantic`: extracted facts;
- `procedural`: successful recipes;
- `error`: failed actions and anti-patterns;
- `social`: agent roles, trust, ownership.

Acceptance:

- shared runtime-agnostic lifecycle contract; ✅ initial skeleton
- stable identity resolver for local/non-SaaS deployments; ✅
- OpenClaw plugin loads UAM config and injects recalled context before the model
  call or planning step; ✅ `agent_turn_prepare`
- Hermes plugin does the same for its runtime lifecycle; ✅ `prefetch`
- both plugins retain run summaries and tool/error memories after execution; ✅
- both use the same Obelisk Memory server and HTTP API; ✅
- each plugin can be disabled with one env/config flag; ✅ `UAM_MEMORY_ENABLED`
- plugins never require a hosted SaaS service; ✅
- MCP bridge remains optional, not the primary integration. ✅

Runtime APIs verified against `.14`:

- OpenClaw native plugin: `openclaw.extensions`, default plugin entry object,
  `api.registerHook("agent_turn_prepare" | "after_tool_call" | "agent_end")`.
- Hermes memory provider: `$HERMES_HOME/plugins/<name>/`, `MemoryProvider`
  methods `initialize`, `prefetch`, `sync_turn`, `on_session_end`, tool schemas.

Next hardening step: install these adapters into the live `.14` runtimes and run
end-to-end smoke tests against a local UAM server.

## WP-16 Secrets and PII guard — complete

Add a pre-ingest policy gate.

Detection targets:

- API keys and tokens;
- private keys;
- cookies;
- passwords;
- high-risk PII;
- credentials in logs/tool output.

Actions:

- reject;
- redact and store;
- store only metadata;
- store encrypted/private scoped memory;
- require human approval.

Acceptance:

- deterministic regex detectors for common secrets; ✅
- configurable policy; ✅ `UAM_PRIVACY_ENABLED`, `UAM_PRIVACY_ACTION`
- audit trail for redaction decisions; ✅ metadata `privacy`
- tests with representative secret fixtures. ✅

Implemented actions:

- `redact` — default, stores sanitized text;
- `reject` — refuses retention/supersede;
- `metadata_only` — stores only a placeholder plus audit metadata;
- `allow` — stores text and records audit metadata when enabled.

See also [ROADMAP_PHASE_3.md](ROADMAP_PHASE_3.md) for follow-up hardening.

## WP-17 Temporal lifecycle and decay policies — partial complete

Add explicit memory lifecycle fields and retrieval behavior:

```text
status: active|stale|deprecated|disputed|hypothesis|rejected|archived|pinned
observed_at
valid_from
valid_to
expires_at
last_confirmed_at
last_used_at
decay_policy
```

Acceptance:

- status is first-class in API and retrieval; ✅
- default recall excludes rejected/archived; ✅
- working memory can expire; pending scheduled maintenance
- core memory can be pinned; ✅ domain invariant
- stale history remains auditable. ✅ stored rows remain listable by status

Implemented statuses:

```text
active|stale|deprecated|disputed|hypothesis|rejected|archived|pinned
```

Retrieval policy:

- `rejected` and `archived` are excluded from recall;
- `disputed` and `hypothesis` are demoted;
- `stale` and `deprecated` are strongly demoted;
- `pinned` boosts core memory.

## WP-18 Graph layer — complete

Use existing `memory_edges` as the first production graph layer before adopting
any external graph DB.

Edges:

- `supersedes`;
- `supports`;
- `contradicts`;
- `derived_from`;
- `same_entity`;
- `caused_by`;
- `owned_by_agent`;
- `from_thread`.

Acceptance:

- edge write/read APIs; ✅ `POST /v1/graph/edges`, `GET /v1/memory/{id}/neighbors`
- graph neighbors can feed retrieval; pending retrieval expansion
- vault export renders backlinks; pending vault graph rendering
- conflict resolver uses `contradicts` edges. pending automatic edge emission

Implemented edge types:

```text
supports|contradicts|derived_from|same_entity|caused_by|owned_by_agent|from_thread|supersedes
```

## WP-19 Maintenance jobs

Add background “memory gardener” jobs:

- dedupe scan;
- conflict scan;
- stale detection;
- summarize old threads;
- promote important memories;
- demote low-confidence unused memories;
- reindex by model/version;
- verify backup;
- export vault.

Acceptance:

- job registry;
- per-job metrics;
- safe retry/idempotency;
- CLI or API trigger;
- Docker profile for scheduled runner.

## WP-20 Production ops hardening

Hardening for long-running self-hosted deployments:

- readiness endpoint separate from `/health`;
- structured JSON logs;
- OpenTelemetry traces;
- rate limits;
- stricter API-key/role model;
- TLS reverse-proxy guide;
- backup manifest/checksum;
- restore drill command;
- migration rollback guidance where possible;
- dashboard examples.

Acceptance:

- production deployment checklist;
- metrics dashboard document;
- restore drill documented and tested;
- failure mode playbook.

## Priority recommendation

Recommended order:

1. WP-11 Obsidian/vault mode.
2. WP-12 Real embedding providers.
3. WP-15 Native OpenClaw/Hermes integrations.
4. WP-13 Conflict resolver/review inbox.
5. WP-16 Secrets/PII guard.
6. WP-14 Human UI.
7. WP-17 Temporal lifecycle.
8. WP-18 Graph layer.
9. WP-19 Maintenance jobs.
10. WP-20 Production ops hardening.

The strongest next product step is WP-11. It turns memory from a black-box agent
database into a human-readable knowledge vault, which is essential for trust,
debugging and long-term ownership.
