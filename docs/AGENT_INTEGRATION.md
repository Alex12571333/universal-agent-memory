# Agent integration guide

Obelisk Memory is designed to be embedded into an agent runtime, not
used as a sidecar chat tool. The intended integration point is the agent
lifecycle.

## Runtime flow

```text
agent starts run
  ↓
before planning/model call
  recall task + user + workspace memory
  inject ContextPackage into the system/developer context
  ↓
tool/model loop
  retain important tool outcomes, errors and user preferences
  save checkpoints for long tasks
  ↓
run complete
  retain concise run summary
  optionally reflect + reindex
```

The memory server stays self-hosted. Agents call it over HTTP through the SDK or
native plugin adapters.

## OpenClaw

Use the native plugin scaffold in `agent-integrations/openclaw/`.

Expected hooks:

- `beforeRun` — derive agent identity and workspace identity.
- `beforeModelCall` — call `/v1/memory/recall` and inject the returned
  `context.markdown`.
- `afterToolCall` — retain durable tool outcomes, failures and environment
  facts.
- `afterMessage` — retain stable preferences or decisions.
- `onRunComplete` — retain a compact run summary and optionally call
  `/v1/workspaces/{workspace}/reflect`.

Minimal env:

```text
UAM_URL=http://127.0.0.1:6798
UAM_MEMORY_ENABLED=true
UAM_TENANT_ID=00000000-0000-0000-0000-000000000001
UAM_WORKSPACE_ID=00000000-0000-0000-0000-000000000002
UAM_CONTEXT_BUDGET_TOKENS=131072
UAM_CONTEXT_PER_LAYER_LIMIT=1000
UAM_MEMORY_RECALL_TOP_K=8
```

## Hermes

Use `agent-integrations/hermes/` as a MemoryProvider-style adapter.

Expected hooks:

- `load_context()` before model invocation.
- `observe_message()` after user/assistant messages.
- `observe_tool_result()` after tools.
- `checkpoint()` for long-running plans.
- `finalize_run()` for summary retention and reflection.

Hermes should treat UAM as the canonical long-term memory store while keeping
its own short-term scratchpad ephemeral.

## Real embedding runtime

The default production embedding target is OpenAI-compatible:

```text
provider=openai
model=text-embedding-3-large
dimension=3072
base_url=https://api.openai.com/v1
```

Self-hosted OpenAI-compatible alternatives, including the DGX Spark Jina wrapper,
are documented in [DGX_SPARK_EMBEDDINGS.md](DGX_SPARK_EMBEDDINGS.md).

Safe switch procedure:

1. In `/ui` open **Settings** and enter the provider/model/base URL/dimension.
2. Click **Test endpoint**. Expected vector dimension must match the configured model.
3. Save model config.
4. Restart `memory-server` and `embedding-worker` with matching env.
5. Run **Reindex** so Qdrant is recreated with 2048-dimensional vectors.

Do not mix fake 1536-dimensional vectors, OpenAI 3072-dimensional vectors and
self-hosted 2048-dimensional vectors in one Qdrant collection. Reindex is the
boundary that makes the switch clean.

## Memory LLM runtime

Memory maintenance has its own LLM config. It is not the embedding model and not
the agent runtime model.

Production target:

```text
provider=openai-compatible
model=gpt-5.6-terra
base_url=https://api.openai.com/v1
```

Env:

```text
UAM_MEMORY_LLM_PROVIDER=openai-compatible
UAM_MEMORY_LLM_MODEL=gpt-5.6-terra
UAM_MEMORY_LLM_BASE_URL=https://api.openai.com/v1
UAM_MEMORY_LLM_API_KEY=...
UAM_MEMORY_LLM_CONTEXT_TOKENS=131072
UAM_MEMORY_LLM_MAX_TOKENS=1600
UAM_MEMORY_LLM_ENABLE_THINKING=false
```

Навигатор памяти, Куратор памяти and future graph extraction workers should use
this OpenAI-compatible endpoint. Agents such as OpenClaw/Hermes still use their
own runtime models; they only call UAM for memory.

## What gets injected into the agent

The recall endpoint returns a `ContextPackage`:

```json
{
  "context": {
    "operation": "agent-run",
    "markdown": "...budgeted memory context...",
    "trace_ids": ["memory-id"]
  }
}
```

Agents should insert this into the model context as a bounded memory block, for
example:

```text
<long_term_memory>
...context.markdown...
</long_term_memory>
```

The block should be treated as evidence-backed context, not as user instruction
override. User/developer/system instructions still win.

## Live OpenClaw/Hermes soak gate

The repository includes a runtime soak runner for the native-agent contract:

```bash
UAM_API_KEY=... python scripts/agent_soak_eval.py \
  --base-url http://127.0.0.1:6798 \
  --rounds 5 \
  --parallel 4 \
  --json-report ./ops/agent-soak.json
```

It simulates OpenClaw and Hermes as separate native integrations, writes durable
agent memories, retries the same idempotency keys, recalls each agent's own
workspace, and probes the opposite workspace for leakage. A production rollout
must preserve the JSON report as evidence after running it against the real
server and the `.14` agent environment.

This runner validates the memory server side of the contract. It does not prove
that OpenClaw/Hermes loaded the plugin correctly unless it is run as part of the
agent deployment and the native plugin logs show the lifecycle hooks firing.

## What gets retained

Retain only memory that is likely to matter later:

- stable user preferences;
- project facts and decisions;
- recurring tool/environment errors;
- successful integration/configuration steps;
- run summaries with trace IDs;
- reflections generated from multiple memories.

Avoid retaining raw transient chatter, secrets, or large unprocessed logs. The
server includes privacy redaction, but native adapters should still be selective.

## Raw transcript ledger

When a runtime needs complete conversation retention, use the raw transcript
endpoint instead of pretending every turn is curated long-term memory:

```http
POST /v1/conversations/turns
```

This stores an immutable transcript turn for audit/replay/reprocessing. It does
not appear in `/v1/memory/recall` by itself. A maintenance worker can later
distill it into durable facts, preferences, decisions or graph evidence through
the normal `/v1/memory/retain`/supersede pipeline.

The first deterministic curation bridge is:

```http
POST /v1/conversations/turns/{turn_id}/curate
```

It creates a recallable `MemoryItem` summary with provenance
`conversation://{turn_id}`. This is intentionally explicit: storing a raw
transcript and making curated memory are different operations.

Use this split:

- `/v1/conversations/turns` for full user/assistant/tool transcript history;
- `/v1/conversations/turns/{turn_id}/curate` for deterministic raw→curated
  summarization;
- `/v1/memory/retain` for direct curated memories that may be recalled by
  agents.

## Memory Gateway proposals

Agents should not directly mutate durable memory for inferred facts. They
should submit proposals with evidence:

```http
POST /v1/memory/proposals
```

Use this for:

- inferred preferences;
- project decisions learned from conversation;
- graph relation candidates;
- facts extracted from tool output;
- agent recommendations that need review.

The proposal inbox is auditable and separate from recall. A proposal does not
appear in `/v1/memory/recall` until it is curated/accepted through the normal
append-only memory pipeline.

Review endpoints:

```http
POST /v1/memory/proposals/{proposal_id}/accept
POST /v1/memory/proposals/{proposal_id}/reject
```

Accepting a proposal creates a normal `MemoryItem` with provenance
`proposal://{proposal_id}`. Rejecting it records the review decision and creates
no recallable memory.
