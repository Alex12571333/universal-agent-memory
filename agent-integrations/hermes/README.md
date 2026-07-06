# Hermes native integration skeleton

Goal: integrate Universal Agent Memory as a Hermes runtime/plugin extension,
not as a skill and not only as an MCP tool.

Expected behavior:

1. Load UAM server URL/API key from Hermes plugin config or environment.
2. Before a run/model call, inject recalled core/working/task memory.
3. After tool calls and errors, retain durable traces and lessons.
4. Persist checkpoints for recovery and multi-agent handoff.
5. Submit run summaries for reflection and conflict detection.

The exact Hermes plugin API should be verified against the current runtime
before implementing the adapter. The shared lifecycle contract in
`../shared/lifecycle.py` is the stable boundary.

Suggested env:

```text
UAM_URL=http://localhost:8080
UAM_API_KEY=...
UAM_AGENT_INTEGRATION=hermes
UAM_MEMORY_ENABLED=true
```
