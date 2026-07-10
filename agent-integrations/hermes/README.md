# Hermes native integration

Goal: integrate Obelisk Memory as a Hermes runtime/plugin extension,
not as a skill and not only as an MCP tool.

> **Production setup:** provision the stable Hermes agent and thread IDs with an
> operator key, bind the `hermes` agent principal to the same tenant, workspace
> and agent UUIDs, then enable strict identity bindings. Unprovisioned or
> mismatched identities are rejected intentionally.

The installable provider lives in `universal_agent_memory/` and implements the
Hermes memory provider contract:

- provider directory contains `__init__.py` and `plugin.yaml`;
- Hermes discovers user providers from `$HERMES_HOME/plugins/<name>/`;
- provider implements `initialize`, `system_prompt_block`, `prefetch`,
  `sync_turn`, `on_session_end`, `get_tool_schemas`, `handle_tool_call`.

Implemented behavior:

1. Load UAM server URL/API key from Hermes plugin config or environment.
2. `prefetch`: recall and inject relevant cross-agent memory before turns.
3. `sync_turn`: retain completed user/assistant turns.
4. `on_session_end`: retain a compact session summary.
5. Tools:
   - `universal_agent_memory_search`
   - `universal_agent_memory_add`

Install outline:

```bash
mkdir -p "$HERMES_HOME/plugins"
cp -R agent-integrations/hermes/universal_agent_memory "$HERMES_HOME/plugins/"
```

Then set Hermes config:

```yaml
memory:
  provider: universal_agent_memory
```

Suggested env:

```text
UAM_URL=http://localhost:6798
UAM_API_KEY=...
UAM_AGENT_INTEGRATION=hermes
UAM_MEMORY_ENABLED=true
```

Optional explicit identities:

```text
UAM_TENANT_ID=00000000-0000-0000-0000-000000000001
UAM_WORKSPACE_ID=00000000-0000-0000-0000-000000000002
UAM_AGENT_ID=00000000-0000-0000-0000-000000000003
```

Without those values, the provider derives stable local UUIDs from the Hermes
session/runtime context.
