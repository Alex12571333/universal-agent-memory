import { createHash } from "node:crypto";

const DEFAULT_URL = "http://localhost:6798";

function envBool(name, fallback) {
  const raw = process.env[name];
  if (raw == null || raw === "") return fallback;
  return ["1", "true", "yes", "on"].includes(raw.toLowerCase());
}

function stableUuid(label) {
  const digest = createHash("sha256")
    .update(`universal-agent-memory:${label}`)
    .digest();
  const bytes = Buffer.from(digest.subarray(0, 16));
  bytes[6] = (bytes[6] & 0x0f) | 0x50;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = bytes.toString("hex");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function cfg(pluginConfig = {}) {
  const integration = "openclaw";
  return {
    url: String(pluginConfig.url || process.env.UAM_URL || DEFAULT_URL).replace(/\/+$/, ""),
    apiKey: pluginConfig.apiKey || process.env.UAM_API_KEY || "",
    enabled: envBool("UAM_MEMORY_ENABLED", pluginConfig.enabled ?? true),
    tenantId: pluginConfig.tenantId || process.env.UAM_TENANT_ID || stableUuid("tenant:default"),
    workspaceId:
      pluginConfig.workspaceId ||
      process.env.UAM_WORKSPACE_ID ||
      stableUuid(`workspace:${process.cwd()}`),
    agentId:
      pluginConfig.agentId ||
      process.env.UAM_AGENT_ID ||
      stableUuid(`agent:${integration}:${process.env.USER || "openclaw"}`),
    topK: Number(pluginConfig.topK || process.env.UAM_MEMORY_RECALL_TOP_K || 8),
    contextBudgetTokens: Number(
      pluginConfig.contextBudgetTokens || process.env.UAM_CONTEXT_BUDGET_TOKENS || 131072,
    ),
    retainToolTraces: envBool("UAM_RETAIN_TOOL_TRACES", pluginConfig.retainToolTraces ?? true),
    reflectOnRunComplete: envBool(
      "UAM_REFLECT_ON_RUN_COMPLETE",
      pluginConfig.reflectOnRunComplete ?? false,
    ),
  };
}

async function postJson(config, path, payload) {
  const headers = { "content-type": "application/json" };
  if (config.apiKey) headers.authorization = `Bearer ${config.apiKey}`;
  const response = await fetch(`${config.url}${path}`, {
    method: "POST",
    headers,
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    throw new Error(`UAM ${path} failed: HTTP ${response.status} ${await response.text()}`);
  }
  return response.json();
}

function contextFromHook(config, event, ctx) {
  const session = ctx?.sessionKey || ctx?.sessionId || event?.sessionKey || event?.runId || "openclaw";
  return {
    tenant_id: config.tenantId,
    workspace_id: config.workspaceId,
    agent_id: config.agentId,
    thread_id: stableUuid(`thread:${session}`),
    labels: ["openclaw", ctx?.workspaceDir || process.cwd()].filter(Boolean),
  };
}

function lastMessageText(messages) {
  if (!Array.isArray(messages)) return "";
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const msg = messages[i];
    const content = msg?.content ?? msg?.text ?? msg?.message;
    if (typeof content === "string" && content.trim()) return content.trim();
    if (Array.isArray(content)) {
      const text = content
        .map((part) => (typeof part === "string" ? part : part?.text || ""))
        .filter(Boolean)
        .join("\n")
        .trim();
      if (text) return text;
    }
  }
  return "";
}

function normalizeTranscriptMessages(messages) {
  if (!Array.isArray(messages)) return [];
  return messages
    .map((msg) => {
      const role = String(msg?.role || msg?.type || "message");
      const content = msg?.content ?? msg?.text ?? msg?.message;
      let text = "";
      if (typeof content === "string") {
        text = content.trim();
      } else if (Array.isArray(content)) {
        text = content
          .map((part) => (typeof part === "string" ? part : part?.text || ""))
          .filter(Boolean)
          .join("\n")
          .trim();
      }
      if (!text) return null;
      return { role, content: text };
    })
    .filter(Boolean);
}

function idempotency(prefix, text, ctx) {
  const digest = createHash("sha256").update(text).digest("hex").slice(0, 24);
  return `${prefix}:${ctx?.runId || ctx?.sessionKey || "openclaw"}:${digest}`;
}

async function retain(config, base, body) {
  return postJson(config, "/v1/memory/retain", {
    ...base,
    ...body,
    source_kind: "openclaw-plugin",
  });
}

async function appendConversationTurn(config, base, messages, ctx) {
  if (!messages.length) return undefined;
  const text = JSON.stringify(messages);
  return postJson(config, "/v1/conversations/turns", {
    ...base,
    namespace: "openclaw",
    source_kind: "openclaw-plugin",
    retention_policy: "raw_and_curated",
    messages,
    metadata: {
      runId: ctx?.runId || "",
      sessionKey: ctx?.sessionKey || "",
    },
    idempotency_key: idempotency("openclaw-transcript", text, ctx),
  });
}

export default {
  id: "universal-agent-memory",
  name: "Obelisk Memory",
  description: "Native OpenClaw lifecycle hooks for shared long-term agent memory.",
  configSchema: {
    validate(value) {
      return { ok: typeof value === "object" || value == null };
    },
    jsonSchema: {
      type: "object",
      properties: {
        url: { type: "string", default: DEFAULT_URL },
        apiKey: { type: "string" },
        tenantId: { type: "string" },
        workspaceId: { type: "string" },
        agentId: { type: "string" },
        enabled: { type: "boolean", default: true },
        topK: { type: "number", default: 8 },
        contextBudgetTokens: { type: "number", default: 131072 },
      },
    },
  },
  register(api) {
    const config = cfg(api.pluginConfig || {});

    api.registerHook("agent_turn_prepare", async (event, ctx) => {
      if (!config.enabled) return undefined;
      const base = contextFromHook(config, event, ctx);
      try {
        const data = await postJson(config, "/v1/memory/recall", {
          ...base,
          query: event?.prompt || "Recall relevant memory for this OpenClaw run.",
          operation: "openclaw_agent_turn_prepare",
          top_k: config.topK,
          context_budget_tokens: config.contextBudgetTokens,
        });
        const markdown = data?.context?.markdown || "";
        if (!markdown.trim()) return undefined;
        return {
          prependContext: `# Obelisk Memory\n${markdown}`,
        };
      } catch (error) {
        api.logger.warn(`UAM recall failed: ${error.message}`);
        return undefined;
      }
    });

    api.registerHook("after_tool_call", async (event, ctx) => {
      if (!config.enabled || !config.retainToolTraces) return;
      const base = contextFromHook(config, event, ctx);
      const resultText = JSON.stringify({
        tool: event?.toolName,
        params: event?.params,
        result: event?.result,
        error: event?.error,
        durationMs: event?.durationMs,
      });
      try {
        await retain(config, base, {
          layer: event?.error ? "error" : "procedural",
          scope: "thread",
          kind: event?.error ? "agent_error" : "tool_trace",
          text: resultText,
          idempotency_key: idempotency(`openclaw-tool:${event?.toolName || "tool"}`, resultText, ctx),
        });
      } catch (error) {
        api.logger.warn(`UAM tool retention failed: ${error.message}`);
      }
    });

    api.registerHook("agent_end", async (event, ctx) => {
      if (!config.enabled || !event?.success) return;
      const summary = lastMessageText(event?.messages);
      const transcript = normalizeTranscriptMessages(event?.messages);
      if (!summary && !transcript.length) return;
      const base = contextFromHook(config, event, ctx);
      try {
        await appendConversationTurn(config, base, transcript, ctx);
        if (summary) {
          await retain(config, base, {
            layer: "episodic",
            scope: "thread",
            kind: "run_summary",
            text: summary,
            idempotency_key: idempotency("openclaw-run-summary", summary, ctx),
          });
        }
        if (config.reflectOnRunComplete) {
          await postJson(
            config,
            `/v1/workspaces/${config.workspaceId}/reflect?tenant_id=${config.tenantId}`,
            {},
          );
        }
      } catch (error) {
        api.logger.warn(`UAM run retention failed: ${error.message}`);
      }
    });
  },
};
