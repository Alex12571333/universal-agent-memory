import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import plugin from "./index.js";
import { evaluateRecallGate, recallGateMetrics } from "./recall_gate.js";

const casesUrl = new URL("../../shared/recall_gate_cases.json", import.meta.url);

test("gate matches the shared RU/EN decision contract", async () => {
  const cases = JSON.parse(await readFile(casesUrl, "utf8"));
  for (const item of cases) {
    const decision = evaluateRecallGate(item.query, {
      mode: item.mode,
      hasLiveContext: item.has_live_context,
      forceFullRecall: item.force_full_recall,
    });
    assert.deepEqual(decision, {
      shouldRecall: item.expected.should_recall,
      reason: item.expected.reason,
      tier: item.expected.tier,
    }, item.query);
  }
});

test("OpenClaw skips simple turns and safely injects compact project recall", async () => {
  recallGateMetrics.reset();
  const hooks = new Map();
  const logs = [];
  const api = {
    pluginConfig: {},
    on(name, handler) { hooks.set(name, handler); },
    logger: { debug(value) { logs.push(value); }, warn() {} },
  };
  plugin.register(api);
  const prepare = hooks.get("agent_turn_prepare");
  assert.equal(await prepare({ prompt: "Привет!" }, {}), undefined);

  const previousFetch = globalThis.fetch;
  let payload;
  globalThis.fetch = async (_url, options) => {
    payload = JSON.parse(options.body);
    return {
      ok: true,
      async json() {
        return { context: { markdown: "- durable fact", used_tokens: 42 } };
      },
    };
  };
  try {
    const result = await prepare({ prompt: "Что осталось в нашем проекте?" }, {});
    assert.equal(payload.top_k, 6);
    assert.equal(payload.context_budget_tokens, 1200);
    assert.equal(payload.context_per_layer_limit, 3);
    assert.equal(payload.minimum_score, 0.45);
    assert.match(result.prependContext, /untrusted reference data/);
    assert.match(result.prependContext, /<obelisk_memory_reference>/);
    assert.match(result.prependContext, /durable fact/);
  } finally {
    globalThis.fetch = previousFetch;
  }
  const metrics = recallGateMetrics.snapshot();
  assert.equal(metrics.decisions["skip:greeting:none"], 1);
  assert.equal(metrics.decisions["recall:project_context:compact"], 1);
  assert.equal(metrics.injected_tokens_total, 42);
  assert.ok(logs.every((entry) => !entry.includes("нашем проекте")));
});

test("OpenClaw always mode uses the explicit research tier", async () => {
  const hooks = new Map();
  plugin.register({
    pluginConfig: { recallMode: "always" },
    on(name, handler) { hooks.set(name, handler); },
    logger: { debug() {}, warn() {} },
  });
  const previousFetch = globalThis.fetch;
  let payload;
  globalThis.fetch = async (_url, options) => {
    payload = JSON.parse(options.body);
    return { ok: true, async json() { return { context: { markdown: "- result" } }; } };
  };
  try {
    await hooks.get("agent_turn_prepare")({ prompt: "A self-contained turn" }, {});
  } finally {
    globalThis.fetch = previousFetch;
  }
  assert.equal(payload.top_k, 10);
  assert.equal(payload.context_budget_tokens, 2500);
  assert.equal(payload.context_per_layer_limit, 6);
});
