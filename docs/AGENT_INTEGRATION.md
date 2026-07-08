# Agent integration guide

Universal Agent Memory is designed to be embedded into an agent runtime, not
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
UAM_URL=http://127.0.0.1:8080
UAM_MEMORY_ENABLED=true
UAM_TENANT_ID=00000000-0000-0000-0000-000000000001
UAM_WORKSPACE_ID=00000000-0000-0000-0000-000000000002
UAM_CONTEXT_BUDGET_TOKENS=2400
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

The current production local embedding target is:

```text
provider=tei
model=jina-embeddings-v4
dimension=2048
base_url=http://192.168.0.10:8002
```

That endpoint is the DGX Spark wrapper documented in
[DGX_SPARK_EMBEDDINGS.md](DGX_SPARK_EMBEDDINGS.md). It exposes
OpenAI-compatible `/v1/embeddings` and returns one normalized vector per input.

Safe switch procedure:

1. In `/ui` open **Settings → Use DGX preset**.
2. Click **Test endpoint**. Expected vector dimension: `2048`.
3. Save model config.
4. Restart `memory-server` and `embedding-worker` with matching env.
5. Run **Reindex** so Qdrant is recreated with 2048-dimensional vectors.

Do not mix fake 1536-dimensional vectors and Jina 2048-dimensional vectors in
one Qdrant collection. Reindex is the boundary that makes the switch clean.

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
