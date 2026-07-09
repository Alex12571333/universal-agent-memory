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
run complete      → retain summary and optionally trigger reflection/reindex
```

MCP can remain as a compatibility bridge, but it should not be the primary
memory integration for agents that support plugins/runtime hooks.

## Layout

```text
agent-integrations/
  shared/      runtime-agnostic lifecycle contract
  openclaw/    installable OpenClaw ESM plugin
  hermes/      Hermes MemoryProvider adapter
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
UAM_MEMORY_RECALL_TOP_K=8
UAM_CONTEXT_BUDGET_TOKENS=131072
UAM_CONTEXT_PER_LAYER_LIMIT=1000
UAM_REFLECT_ON_RUN_COMPLETE=false
```

If IDs are omitted, adapters derive stable UUIDv5-style IDs from the runtime
name/session/workspace so local testing works without SaaS-style onboarding.
