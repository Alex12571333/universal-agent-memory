# ADR 0002: Adaptive recall gate and compact context delivery

- Status: accepted
- Date: 2026-07-14
- Issue: [#240](https://github.com/Alex12571333/universal-agent-memory/issues/240)

## Context

The native OpenClaw hook currently recalls memory before every turn. Hermes and
the runtime-neutral plugin only skip an empty query. Their default context
budget is 8192 tokens and the server allows 1000 records per memory layer. This
adds avoidable prefill latency and lets irrelevant historical material compete
with the current request.

A recall gate must not become another opaque LLM decision. It has to be fast,
deterministic, inspectable, fail-soft, and equivalent in the JavaScript and
Python integrations. It also must not prevent an operator or a research agent
from explicitly requesting broad recall.

## Decision

Every native integration supports three recall modes:

- `off`: never run automatic pre-turn recall. Explicit memory-search tools still
  work.
- `adaptive` (default): run a deterministic RU/EN gate over the current user
  query and bounded runtime metadata.
- `always`: preserve the previous recall-before-every-turn behaviour.

The gate returns a structured decision containing `should_recall`, a bounded
reason code, and the selected tier. The initial reason vocabulary is:

- recall: `explicit_memory`, `historical_reference`, `personal_context`,
  `project_context`, `continuation_with_context`, `ambiguous_reference`, and
  `conservative_fallback`;
- skip: `mode_off`, `empty`, `greeting`, `simple_calculation`,
  `single_phrase_translation`, `short_command`, and `self_contained`;
- override: `mode_always` and `explicit_full_recall`.

The decision is conservative: a query that is not confidently self-contained
recalls a compact package. `continue`/`продолжай` only recalls when the runtime
reports that the live conversation context is absent; otherwise it stays inside
the current context window.

The default compact tier is:

- `top_k=6`;
- `context_budget_tokens=1200`;
- `context_per_layer_limit=3`;
- a calibrated minimum relevance score supplied by native integrations.

The full/research tier is explicitly selected by `always`, an integration
option, or runtime metadata. Its default budget is 2500 tokens and it does not
restore the historical 8192-token implicit injection. Operators can still
configure a larger value deliberately.

All injected memory is wrapped as untrusted reference data. The wrapper tells
the agent not to execute instructions, reveal secrets, or treat a memory record
as higher-priority policy. Explicit search tools return records as data and do
not pass through the automatic gate.

The gate records bounded counters by decision, reason, and tier, plus recall
latency and injected-token estimates. Native hosts can read a metrics snapshot;
the integrations also emit one structured decision log without retaining the
query text. The server's existing recall audit remains the authoritative trace
for recalls that actually reach the API.

## Compatibility and failure behaviour

- `UAM_RECALL_MODE=always` restores unconditional automatic recall.
- `UAM_RECALL_MODE=off` affects only automatic injection, not explicit search.
- Invalid modes fail closed to `adaptive` and are visible through reason/config
  diagnostics.
- Gate evaluation is local and has no model or network dependency.
- Recall and metrics failures remain fail-soft: the agent turn continues without
  injected memory.
- Gate fixtures are shared between JS and Python to prevent behavioural drift.

## Consequences

Simple turns normally pay zero retrieval or prefill cost. Historical,
project-specific, personal, and ambiguous queries still recall memory, but the
amount is bounded. Deterministic heuristics can miss uncommon phrasing, so the
conservative fallback and the `always` override remain part of the production
contract. Threshold calibration must use both semantic and lexical retrieval;
a value that rejects exact lexical matches is not acceptable.
