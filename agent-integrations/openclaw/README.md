# OpenClaw native integration skeleton

Goal: integrate Universal Agent Memory as an OpenClaw runtime/plugin extension,
not as a skill and not only as an MCP tool.

Expected behavior:

1. Load UAM server URL/API key from OpenClaw plugin config or environment.
2. Before a run/model call, recall project/core/working memory and inject a
   budgeted context package.
3. After messages/tool calls, retain important decisions, observations, tool
   traces and errors.
4. Save checkpoints for long-running work.
5. Trigger reflection/reindex after successful runs when configured.

The exact OpenClaw plugin API should be verified against the current runtime
before implementing the adapter. The shared lifecycle contract in
`../shared/lifecycle.py` is the stable boundary.

Suggested env:

```text
UAM_URL=http://localhost:8080
UAM_API_KEY=...
UAM_AGENT_INTEGRATION=openclaw
UAM_MEMORY_ENABLED=true
```
