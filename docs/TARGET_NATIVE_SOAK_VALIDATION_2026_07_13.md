# Target native agent soak validation — 2026-07-13

## Scope

This is target-runtime evidence for the native OpenClaw and Hermes integrations.
It complements the bounded burst proof in
[TARGET_NATIVE_BURST_VALIDATION_2026_07_13.md](TARGET_NATIVE_BURST_VALIDATION_2026_07_13.md).
It does not certify high availability, multi-node operation, security review or
full production readiness.

## Native runtime result

The target host `.14` ran 12 paired native invocations over approximately two
hours, with a ten-minute interval between rounds. Each round invoked the real
OpenClaw CLI and the real Hermes runtime, not a direct HTTP substitute.

| Agent | Successful native invocations | Failed invocations | Result |
|---|---:|---:|---|
| OpenClaw | 12 / 12 | 0 | PASS |
| Hermes | 12 / 12 | 0 | PASS |

The final native log recorded `completed=1` at `2026-07-13T14:43:23Z`.

## Canonical lifecycle evidence

The appliance audit was queried after the soak, from the start timestamp
`2026-07-13T12:51:29Z`, using an operator credential. The query returned
aggregated counts only; no prompts, transcript text, memory text, API keys or
provider endpoints were exported.

| Actor | Conversation append | Recall | Retain |
|---|---:|---:|---:|
| `openclaw` | 19 | 20 | 28 |
| `hermes` | 25 | 26 | 0 |

These counts include the earlier bounded native burst in the same time window,
so they are corroborating lifecycle evidence rather than a claim of exactly one
audit event per soak round. The per-round CLI log is the authoritative count of
the 12 paired native invocations.

## Post-soak health

Immediately after the run:

- authenticated metrics-health gate: PASS;
- outbox pending/dead-letter/lag: `0 / 0 / 0`;
- processed-event in-flight count: `0`;
- embedding worker: up, embedding failures `0`, reindex failures `0`.

The local appliance's `ready` endpoint remained healthy, with PostgreSQL as the
canonical store and the Qdrant hybrid source healthy.

## What this closes and what remains

This closes the planned multi-hour, real-runtime native-agent soak evidence
gate for the current OpenClaw/Hermes target configuration. It does not replace
the remaining release gates in the production audit: independent backup
retention, broader chaos/concurrency evidence, monitoring alert routing,
external secret management and a multi-replica design remain separate work.
