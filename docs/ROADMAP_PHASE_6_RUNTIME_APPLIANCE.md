# Phase 6 — verified local memory appliance

## Purpose

Phase 6 turns the existing production foundations into repeatable operational
evidence.  The product remains a local, self-hosted Docker appliance: there is
no dependency on a public domain, VPN, Caddy, or a hosted control plane.

The objective is not a new UI feature. It is to prove that the real asynchronous
memory path works under the advanced Compose profile:

```text
retain -> PostgreSQL canonical ledger -> transactional outbox -> NATS
       -> embedding worker -> Qdrant -> hybrid recall
```

The API must also make the state of those private dependencies visible to an
authenticated operator without exposing URLs, credentials, prompt text, or
memory text.

## Design rules

- PostgreSQL remains the canonical source of truth. A worker or Qdrant outage
  may degrade semantic recall but must not make canonical memory disappear.
- Dependency probes are explicitly opt-in. The basic Compose profile has no
  NATS or worker, so it reports `not_configured` rather than falsely failing
  readiness.
- In an appliance or advanced verification profile,
  `UAM_RUNTIME_DEPENDENCY_PROBES=true` enables only fixed internal health URLs.
  The operator sees a state (`healthy`, `unavailable`, `unhealthy`, or
  `misconfigured`), never the endpoint or credentials.
- A successful HTTP process health check is not enough. The acceptance probe
  writes a unique durable record and requires it to return through
  `qdrant_hybrid` retrieval before it passes.
- Release evidence contains timestamps, build identity, counters and outcome;
  it does not contain API keys, canonical memory text, conversation text or
  provider secrets.

## Delivery sequence

1. Pass the runtime-probe switch through local Compose while retaining the
   fail-soft default.
2. Start `docker compose --profile advanced up -d --build` and verify API,
   NATS, outbox relay and embedding worker health as an operator.
3. Run a controlled retain-to-recall lifecycle probe with a unique marker;
   verify that the durable outbox drains and Qdrant hybrid retrieval finds the
   canonical memory.
4. Record a redacted local release-evidence report and add a regression test
   for the Compose configuration contract.
5. Repeat the same probe on the `.14` agent appliance after each release;
   this remains a deployment gate, not a claim that source code alone can
   satisfy.

## Acceptance criteria

- Basic local Compose starts without optional advanced services and its
  operator dependency state is `not_configured`.
- Advanced Compose reports NATS and the embedding worker as `healthy` when
  explicitly enabled.
- A retained marker is recallable after asynchronous processing with
  `qdrant_hybrid` among the used sources.
- If NATS, a worker, or Qdrant fails, canonical PostgreSQL recall remains
  possible and operator diagnostics reveal the failure category without
  leaking configuration.
- The workflow is safe to rerun: every probe uses a unique idempotency key and
  produces only bounded, redacted evidence.
