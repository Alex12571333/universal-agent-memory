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
  openclaw/    OpenClaw plugin adapter skeleton
  hermes/      Hermes plugin adapter skeleton
```

The shared contract is deliberately small so OpenClaw/Hermes-specific adapters
can be rewritten when their plugin APIs change without touching the memory
server.
