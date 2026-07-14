# Agent integration guide

Obelisk Memory is designed to be embedded into an agent runtime, not
used as a sidecar chat tool. The intended integration point is the agent
lifecycle.

Stable workspace and agent/thread identities must be registered once by an
operator before an adapter starts retaining durable PostgreSQL records.
Agent-scoped keys cannot create arbitrary identities. Production also requires
a server-enforced tenant/workspace/agent binding for each agent principal.

For an additional workspace (for example an isolated agent or a release soak),
register its stable UUID first. The tenant must already exist; this endpoint
cannot create arbitrary tenants.

```bash
curl -X POST "$UAM_URL/v1/workspaces/provision" \
  -H "Authorization: Bearer $UAM_OPERATOR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "tenant_id":"00000000-0000-0000-0000-000000000001",
    "workspace_id":"00000000-0000-0000-0000-000000000014",
    "workspace_name":"OpenClaw release soak"
  }'
```

```bash
curl -X POST "$UAM_URL/v1/identities/provision" \
  -H "Authorization: Bearer $UAM_OPERATOR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "tenant_id":"00000000-0000-0000-0000-000000000001",
    "workspace_id":"00000000-0000-0000-0000-000000000002",
    "agent_id":"00000000-0000-0000-0000-000000000010",
    "agent_name":"OpenClaw primary",
    "agent_role":"openclaw",
    "agent_config":{"namespace":"openclaw/default"},
    "thread_id":"00000000-0000-0000-0000-000000000011"
  }'
```

The operation is idempotent and may update display metadata/status, but it
refuses to move an existing agent or thread ID into another scope. Give the
adapter its own `agent`-scoped API key only after this operator step.

Bind that key name to the provisioned identity and enable strict startup:

```dotenv
UAM_API_KEYS=openclaw:<random-key>:agent,operator:<random-key>:operator
UAM_API_PRINCIPAL_BINDINGS_JSON={"openclaw":{"tenant_id":"00000000-0000-0000-0000-000000000001","workspace_id":"00000000-0000-0000-0000-000000000002","agent_id":"00000000-0000-0000-0000-000000000010"}}
UAM_REQUIRE_IDENTITY_BINDINGS=true
```

An agent request cannot switch those UUIDs or use a thread owned by another
agent. Agent-only keys cannot open the operator control plane. Private memories
are returned only when the recall `agent_id` matches the stored owner; the same
filter is enforced by the canonical store, vector source and fusion layer.

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
operator scheduler/UI
  reflect + reindex with an operator-scoped key
```

The memory server stays self-hosted. Agents call it over HTTP through the SDK or
native plugin adapters.

For an opt-in orientation at the beginning of a new session, an agent may call
`GET /v1/workspaces/{workspace_id}/seed?tenant_id=...&budget_tokens=512`.
It returns only active/pinned recallable heads from the shared `core`,
`working` and `procedural` layers, never private/thread data. The response is
bounded to 128–4096 estimated tokens and is deliberately not a replacement for
task-scoped `POST /v1/memory/recall`.

## Recall audit and operator replay

Every successful `POST /v1/memory/recall` now returns a `replay_id`.  It is an
audit-event UUID, not a second copy of the prompt or a transcript.  The durable
record contains the query's SHA-256 fingerprint and character count, candidate
and context-budget metrics, source names, and canonical selected memory IDs.
It never contains the raw recall query or copied memory text.

An operator can inspect the exact scoped, redacted explanation later:

```bash
curl "$UAM_URL/v1/workspaces/$UAM_WORKSPACE_ID/replays/$REPLAY_ID?tenant_id=$UAM_TENANT_ID" \
  -H "Authorization: Bearer $UAM_OPERATOR_API_KEY"
```

The endpoint is operator-scoped and tenant/workspace-bound.  It resolves each
saved trace ID against the current canonical ledger and returns only its ID,
layer, status and revision.  This makes an old recall explainable without
turning the audit trail into a second unprotected conversation archive.

The replay also contains `retrieval_traversal`: ordered, bounded telemetry for
each candidate source and the final fusion stage. It records counts before and
after policy filtering, the selected count and an optional dependency exception
class. It never stores exception messages, endpoint URLs, query text, candidate
text or model prompts. The operator UI shows this compact path in Russian for
the current recall and remains compatible with older servers that omit it.

## OpenClaw

Use the native plugin scaffold in `agent-integrations/openclaw/`.

Registered hooks:

- `agent_turn_prepare` — derive runtime identity, call `/v1/memory/recall` and
  prepend the returned `context.markdown` before the model turn.
- `after_tool_call` — retain durable tool outcomes, failures and environment
  facts.
- `agent_end` — retain a compact run summary. Reflection stays in the operator
  control plane.

Minimal env:

```text
UAM_URL=http://127.0.0.1:6798
UAM_MEMORY_ENABLED=true
UAM_API_KEY=<openclaw-agent-scoped-key>
UAM_TENANT_ID=00000000-0000-0000-0000-000000000001
UAM_WORKSPACE_ID=00000000-0000-0000-0000-000000000002
UAM_AGENT_ID=00000000-0000-0000-0000-000000000010
UAM_CONTEXT_BUDGET_TOKENS=8192
UAM_CONTEXT_PER_LAYER_LIMIT=1000
UAM_MEMORY_RECALL_TOP_K=8
```

## Hermes

Use `agent-integrations/hermes/` as a MemoryProvider-style adapter.

Provider lifecycle:

- `prefetch()` before model invocation;
- `sync_turn()` after a completed user/assistant turn;
- `on_session_end()` for summary retention;
- `system_prompt_block()`, `get_tool_schemas()` and `handle_tool_call()` for
  native Hermes context/tool integration.

Hermes should treat UAM as the canonical long-term memory store while keeping
its own short-term scratchpad ephemeral.

## Real embedding runtime

The production embedding target is OpenAI-compatible. That means the wire
contract, not provider lock-in. Configure the endpoint selected for the target
deployment:

```text
provider=openai-compatible
model=<provider-embedding-model-id>
dimension=<actual-output-dimension>
base_url=https://embedding-gateway.example/v1
send_dimensions=false
```

Self-hosted OpenAI-compatible alternatives, including the DGX Spark Jina wrapper,
are documented in [DGX_SPARK_EMBEDDINGS.md](DGX_SPARK_EMBEDDINGS.md). Use
`provider=openai` only when you specifically want the OpenAI-hosted profile with
required API key and OpenAI's optional `dimensions` request field.

Safe switch procedure:

1. In `/ui` open **Settings** and enter the provider/model/base URL/dimension.
2. Click **Test endpoint**. Expected vector dimension must match the configured model.
3. Save model config.
4. Restart `memory-server` and `embedding-worker` with matching env.
5. Run the controlled collection migration for a model or dimension change;
   ordinary workspace reindex never recreates the shared collection.

Do not mix vectors from different models or dimensions in one Qdrant
collection. The server rejects incompatible collection dimensions; a model
change requires an explicitly verified collection migration rather than an
in-place workspace reindex.

## Memory LLM runtime

Memory maintenance has its own LLM config. It is not the embedding model and not
the agent runtime model.

Provider-neutral target shape:

```text
provider=openai-compatible
model=<provider-model-id>
base_url=https://llm-gateway.example/v1
```

Env:

```text
UAM_MEMORY_LLM_PROVIDER=openai-compatible
UAM_MEMORY_LLM_MODEL=<provider-model-id>
UAM_MEMORY_LLM_BASE_URL=https://llm-gateway.example/v1
UAM_MEMORY_LLM_API_KEY=...
UAM_MEMORY_LLM_CONTEXT_TOKENS=8192
UAM_MEMORY_LLM_MAX_TOKENS=1200
UAM_MEMORY_LLM_EXTRA_BODY_JSON={}
```

Навигатор памяти, Куратор памяти and future graph extraction workers should use
this OpenAI-compatible contract. The base URL/model can point at OpenAI,
OpenRouter, LiteLLM, vLLM, llama.cpp, or another compatible gateway.
Agents such as OpenClaw/Hermes still use their own runtime models; they only
call UAM for memory.

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
must preserve the JSON report as evidence after running it through the deployed
OpenClaw and Hermes runtime hooks against the release server.

The runner obtains immutable release identity from public `GET /ready`, so it
does not need access to operator process telemetry at `GET /v1/system/status`.
The key still needs permission for the workspaces selected by the soak run;
provisioning new soak identities remains an explicit operator action.

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

Choose the retention policy deliberately:

- `raw_only` keeps the transcript and rejects curation;
- `raw_and_curated` keeps the transcript after explicit curation;
- `curated_only` keeps raw text only as staging and replaces it with
  `[PURGED_AFTER_CURATION]` immediately after successful curation.

For abandoned `curated_only` turns, the server assigns a bounded expiry from
`UAM_CONVERSATION_CURATED_ONLY_TTL_SECONDS` (default: 24 hours). Schedule the
operator maintenance endpoint at least hourly:

```http
POST /v1/workspaces/{workspace_id}/conversations/purge-expired?tenant_id={tenant_id}
```

`curated_only` is not a promise that plaintext never reaches the ledger: the
turn exists until the curation call completes. Deployments requiring that
stronger boundary must curate before sending data or disable raw capture.

The first deterministic curation bridge is:

```http
POST /v1/conversations/turns/{turn_id}/curate
```

It first creates an evidence-backed memory proposal. By default, Obelisk's
narrow auto-policy may accept only a high-confidence, source-quoted,
non-temporal preference, decision, task, or procedure; every other proposal
stays outside recall until reviewed through
`/v1/memory/proposals/{proposal_id}/accept`. Send `auto_accept: false` to keep
all curated output review-only. This keeps model output from becoming durable
truth without an evidence boundary.

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
