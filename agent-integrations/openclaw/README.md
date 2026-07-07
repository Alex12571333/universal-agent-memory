# OpenClaw native integration

Goal: integrate Universal Agent Memory as an OpenClaw runtime/plugin extension,
not as a skill and not only as an MCP tool.

The installable plugin lives in `plugin/` and matches the OpenClaw extension
shape verified against the `.14` runtime:

- `package.json` exposes `openclaw.extensions: ["./index.js"]`;
- `index.js` exports a default plugin entry object with `register(api)`;
- hooks are registered with OpenClaw `api.registerHook(...)`.

Implemented behavior:

1. Load UAM server URL/API key from OpenClaw plugin config or environment.
2. `agent_turn_prepare`: recall project/core/working memory and prepend a
   budgeted context package.
3. `after_tool_call`: retain successful tool traces or tool errors.
4. `agent_end`: retain final run summary.
5. Optional reflection after successful runs via `UAM_REFLECT_ON_RUN_COMPLETE`.

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
UAM_URL=http://localhost:8080
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
