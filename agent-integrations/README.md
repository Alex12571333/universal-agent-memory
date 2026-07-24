# Native agent integrations

This directory is for runtime/plugin integrations. These are intentionally
deeper than skills or MCP tools: a native integration hooks the agent lifecycle
before, during and after a run.

Target flow:

```text
before run        → recall core/working/task context
before model call → inject a budgeted ContextPackage
after message     → retain useful observations
after tool call   → retain tool trace or error memory
checkpoint        → save working state
run complete      → retain summary; operator worker handles reflection/reindex
```

MCP can remain as an optional bridge, but it should not be the primary
memory integration for agents that support plugins/runtime hooks.

## Layout

```text
agent-integrations/
  shared/      runtime-agnostic lifecycle contract
  openclaw/    installable OpenClaw ESM plugin
  hermes/      Hermes MemoryProvider adapter
  corax/       typed agent.memory/v1 provider (runtime-only, never an LLM tool)
```

The shared contract is deliberately small so OpenClaw/Hermes-specific adapters
can be rewritten when their plugin APIs change without touching the memory
server.

## Identity

Native adapters read these env vars:

```text
UAM_URL=http://localhost:6798
UAM_API_KEY=...
UAM_TENANT_ID=...
UAM_WORKSPACE_ID=...
UAM_AGENT_ID=...
UAM_MEMORY_ENABLED=true
UAM_RECALL_MODE=adaptive
UAM_MEMORY_RECALL_TOP_K=6
UAM_CONTEXT_BUDGET_TOKENS=1200
UAM_CONTEXT_PER_LAYER_LIMIT=3
UAM_RECALL_MINIMUM_SCORE=0.45
UAM_RESEARCH_RECALL_TOP_K=10
UAM_RESEARCH_CONTEXT_BUDGET_TOKENS=2500
UAM_RESEARCH_CONTEXT_PER_LAYER_LIMIT=6
UAM_FORCE_FULL_RECALL=false
```

`adaptive` avoids the memory HTTP call for simple/self-contained turns. `always`
uses the explicit research tier; `off` disables automatic injection while
leaving explicit search tools available. Both adapters expose bounded local
gate metrics (`decisions`, recall count, injected token total and latency sum)
without storing query text.

If IDs are omitted, adapters derive stable UUIDv5-style IDs from the runtime
name/session/workspace so local testing works without SaaS-style onboarding.
Production must provision those exact IDs and bind the agent key before startup.
Reflection and reindex are deliberately absent from agent credentials; run them
from an operator-controlled scheduler or the UI.
