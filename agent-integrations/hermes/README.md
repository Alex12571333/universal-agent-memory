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
2. `prefetch`: run the deterministic recall gate and inject cross-agent memory
   only for turns that need historical context.
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
UAM_RECALL_MODE=adaptive
UAM_MEMORY_RECALL_TOP_K=6
UAM_CONTEXT_BUDGET_TOKENS=1200
UAM_CONTEXT_PER_LAYER_LIMIT=3
UAM_RECALL_MINIMUM_SCORE=0.45
```

`off` disables automatic prefetch but keeps the explicit search tool. `always`
or `UAM_FORCE_FULL_RECALL=true` selects the configurable research tier (10
records, 2500 tokens, 6 per layer by default). `recall_gate_metrics()` exposes
text-free decision/reason, token and latency counters for Hermes diagnostics.
Injected records are marked as untrusted reference data rather than replayed
conversation or instructions.

Optional explicit identities:

```text
UAM_TENANT_ID=00000000-0000-0000-0000-000000000001
UAM_WORKSPACE_ID=00000000-0000-0000-0000-000000000002
UAM_AGENT_ID=00000000-0000-0000-0000-000000000003
```

Without those values, the provider derives stable local UUIDs from the Hermes
session/runtime context.

## Qwen 3 through llama.cpp or vLLM

For a Qwen 3 OpenAI-compatible endpoint, disable thinking in the Hermes custom
provider profile. This deployment requires a final `content` response for
memory recall and tool loops; otherwise Qwen can return only reasoning.

```bash
hermes config set custom_providers.0.model nvidia/Qwen3.6-35B-A3B-NVFP4
hermes config set custom_providers.0.extra_body.chat_template_kwargs.enable_thinking false
```

Use the index of the custom provider which has the Qwen endpoint. The setting
becomes this YAML shape:

```yaml
custom_providers:
  - model: nvidia/Qwen3.6-35B-A3B-NVFP4
    extra_body:
      chat_template_kwargs:
        enable_thinking: false
```

Verify it with a unique synthetic fact: after the fact is accepted through the
proposal workflow, a fresh `hermes -z` invocation must return that fact from
Obelisk Memory. Do not treat a provider merely being installed as proof that
the model is consuming recalled context.
