# OpenClaw native integration

Goal: integrate Obelisk Memory as an OpenClaw runtime/plugin extension,
not as a skill and not only as an MCP tool.

> **Production setup:** provision the stable OpenClaw agent and thread IDs with
> an operator key, bind the `openclaw` agent principal to the same tenant,
> workspace and agent UUIDs, then enable strict identity bindings. Unprovisioned
> or mismatched identities are rejected intentionally.

The installable plugin lives in `plugin/` and uses the OpenClaw extension
contract:

- `package.json` exposes `openclaw.extensions: ["./index.js"]`;
- `openclaw.plugin.json` supplies the manifest required by current OpenClaw
  releases;
- `index.js` exports a default plugin entry object with `register(api)`;
- hooks are registered with OpenClaw `api.registerHook(...)`.

Implemented behavior:

1. Load UAM server URL/API key from OpenClaw plugin config or environment.
2. `agent_turn_prepare`: recall project/core/working memory and prepend a
   budgeted context package.
3. `after_tool_call`: retain successful tool traces or tool errors.
4. `agent_end`: retain final run summary.
5. Reflection/reindex stay in the operator control plane and are not called by
   the agent plugin.

Install outline:

```bash
cd agent-integrations/openclaw/plugin
npm install
openclaw plugins install .
```

If your OpenClaw installation uses a different plugin command, copy this
directory into its plugin path. The important bit is that OpenClaw sees
`package.json` and loads `./index.js`.

Suggested env:

```text
UAM_URL=http://localhost:6798
UAM_API_KEY=...
UAM_AGENT_INTEGRATION=openclaw
UAM_MEMORY_ENABLED=true
```

Optional explicit identities:

```text
UAM_TENANT_ID=00000000-0000-0000-0000-000000000001
UAM_WORKSPACE_ID=00000000-0000-0000-0000-000000000002
UAM_AGENT_ID=00000000-0000-0000-0000-000000000003
```

Without those values, the plugin derives stable local UUIDs.

## CLI bridge

Some OpenClaw releases expose registered hooks in `openclaw hooks list`, but
their embedded `openclaw agent` CLI runner does not dispatch them. For this
execution path use the supplied native bridge instead of calling
`openclaw agent` directly:

```bash
python3 agent-integrations/openclaw/obelisk_openclaw_cli.py \
  --session-key agent:main:my-session \
  --message "Продолжи работу над проектом" \
  --json
```

The bridge reads the existing `universal-agent-memory` plugin configuration
from `~/.openclaw/openclaw.json`, recalls before the run, injects a bounded
reference-only context, then retains the successful final response. Both
memory calls are fail-soft: a temporarily unavailable memory server never
prevents OpenClaw from running. If the API key is deliberately kept out of the
JSON config, it may instead be supplied as `UAM_API_KEY`; on a systemd-managed
OpenClaw gateway the bridge also reads that manager environment locally.
For a non-interactive CLI, a protected local environment file is supported as
well: `~/.config/obelisk-memory/openclaw.env` (mode `0600`). The key is optional
only when the local Obelisk server itself has no API authentication enabled.
For a provisioned long-lived thread, set `threadId` in the plugin configuration
(or `UAM_THREAD_ID`); otherwise the bridge deterministically derives one from
`--session-key`, which must be provisioned before strict retention is enabled.
