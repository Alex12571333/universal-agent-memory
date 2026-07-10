import assert from "node:assert/strict";
import test from "node:test";

import {
  InvalidRequestError,
  MemoryClient,
  type RetainRequest,
} from "../src/index.js";

function response(status: number, body: object, headers: HeadersInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

test("retain reuses its generated idempotency key across retry", async () => {
  const responses = [
    response(503, { detail: "busy" }),
    response(201, { id: "memory-1", created: true, queued_event_ids: [] }),
  ];
  const bodies: RetainRequest[] = [];
  const client = new MemoryClient({
    retry: { maxRetries: 1, baseDelayMs: 0 },
    sleep: async () => undefined,
    fetch: async (_input, init) => {
      bodies.push(JSON.parse(String(init?.body)) as RetainRequest);
      return responses.shift()!;
    },
  });

  const result = await client.retain({ text: "Remember safely" });

  assert.equal(result.id, "memory-1");
  assert.ok(bodies[0]?.idempotency_key);
  assert.equal(bodies[0]?.idempotency_key, bodies[1]?.idempotency_key);
});

test("validation response becomes typed error without retry", async () => {
  let calls = 0;
  const client = new MemoryClient({
    fetch: async () => {
      calls += 1;
      return response(422, { detail: "invalid text" });
    },
  });

  await assert.rejects(
    () => client.retain({ text: "", idempotency_key: "stable" }),
    (error) => error instanceof InvalidRequestError && error.status === 422,
  );
  assert.equal(calls, 1);
});

test("Retry-After controls retry delay", async () => {
  const delays: number[] = [];
  const responses = [
    response(429, { detail: "slow down" }, { "Retry-After": "2" }),
    response(201, { id: "memory-1", created: true, queued_event_ids: [] }),
  ];
  const client = new MemoryClient({
    retry: { maxRetries: 1 },
    sleep: async (milliseconds) => {
      delays.push(milliseconds);
    },
    fetch: async () => responses.shift()!,
  });

  await client.retain({ text: "Retry safely", idempotency_key: "stable" });

  assert.deepEqual(delays, [2000]);
});

test("API key is sent as a bearer token", async () => {
  let authorization: string | null = null;
  const client = new MemoryClient({
    apiKey: "secret",
    fetch: async (_input, init) => {
      authorization = new Headers(init?.headers).get("Authorization");
      return response(200, { status: "ok" });
    },
  });

  await client.health();

  assert.equal(authorization, "Bearer secret");
});

test("operator client provisions stable identity", async () => {
  let path = "";
  const client = new MemoryClient({
    fetch: async (input) => {
      path = String(input);
      return response(200, {
        agent: { id: "agent-1", name: "Hermes" },
        thread: { id: "thread-1", owner_agent_id: "agent-1" },
      });
    },
  });

  const result = await client.provisionIdentity({
    agent_id: "agent-1",
    agent_name: "Hermes",
    agent_role: "hermes",
    thread_id: "thread-1",
  });

  assert.equal(path, "http://localhost:8080/v1/identities/provision");
  assert.equal(result.thread?.owner_agent_id, "agent-1");
});
