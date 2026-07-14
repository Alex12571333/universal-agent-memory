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
- hooks use current OpenClaw `api.on(...)`; a compatibility fallback supports
  older installs exposing `api.registerHook(...)`.

Implemented behavior:

1. Load UAM server URL/API key from OpenClaw plugin config or environment.
2. `agent_turn_prepare`: run a local deterministic recall gate, then recall
   project/core/working memory only when the turn needs historical context.
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
UAM_RECALL_MODE=adaptive
UAM_MEMORY_RECALL_TOP_K=6
UAM_CONTEXT_BUDGET_TOKENS=1200
UAM_CONTEXT_PER_LAYER_LIMIT=3
UAM_RECALL_MINIMUM_SCORE=0.45
```

The gate has `off`, `adaptive` and `always` modes. `always` (or
`forceFullRecall: true`) uses the separately configurable research tier instead
of silently restoring an 8192-token prompt. The plugin exports a text-free
`recallGateMetricsSnapshot()` for host diagnostics and logs only outcome,
bounded reason and tier—never the prompt. Retrieved Markdown is enclosed in an
untrusted-reference wrapper before it reaches the model.

Optional explicit identities:

```text
UAM_TENANT_ID=00000000-0000-0000-0000-000000000001
UAM_WORKSPACE_ID=00000000-0000-0000-0000-000000000002
UAM_AGENT_ID=00000000-0000-0000-0000-000000000003
```

Without those values, the plugin derives stable local UUIDs.

## CLI execution

The native plugin is the primary integration. Current OpenClaw CLI releases
wait for lifecycle hook promises during a one-shot `openclaw agent` run, so the
same recall/retain path works for gateway and CLI execution.

Verified command after installation:

```bash
openclaw agent \
  --session-key agent:main:memory-check \
  --message "Проверь память Obelisk" \
  --json
```

The memory server should record `POST /v1/memory/recall` and a successful
`POST /v1/memory/retain`. A skill is intentionally not used for this: skills
only add instructions to a prompt and cannot guarantee before/after-turn memory
lifecycle work.

`obelisk_openclaw_cli.py` remains an explicit fail-soft compatibility bridge for
an older local CLI that does not dispatch plugin hooks. Use it only after
`openclaw hooks list` or a live HTTP check shows that the installed CLI bypasses
the native plugin:

```bash
python3 agent-integrations/openclaw/obelisk_openclaw_cli.py \
  --session-key agent:main:my-session \
  --message "Продолжи работу над проектом" \
  --json
```

The bridge reads the existing `universal-agent-memory` plugin configuration
from `~/.openclaw/openclaw.json`, recalls before the run, injects a bounded
reference-only context, then retains the successful final response as clean
`Запрос пользователя` / `Ответ агента` text. OpenClaw `--json` metadata is
never written into the memory item. Both
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
