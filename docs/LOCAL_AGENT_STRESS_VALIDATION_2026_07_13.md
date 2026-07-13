# Local agent-contract stress validation — 2026-07-13

## Scope

This is a redacted, local server-side stress check against the self-hosted
Obelisk appliance.  It used isolated `agent-soak` identities and the configured
real embedding provider.  No production conversation text, API key, or memory
body is stored in this document.

It validates the server contract expected by the OpenClaw and Hermes native
adapters.  It does **not** claim that their processes/plugins were running for
the entire test; native lifecycle evidence and a multi-hour target soak remain
separate gates.

## Result

| Property | Result |
| --- | --- |
| Run identifier | `local-20260713-stress` |
| Agent profiles | isolated OpenClaw and Hermes scopes |
| Rounds / parallelism | 20 / 4 |
| Checks | 125 passed |
| OpenClaw contract checks | 60/60 passed |
| Hermes contract checks | 60/60 passed |
| Auth rejection | passed |
| Idempotent retry | passed for every round |
| Cross-workspace leakage probe | passed |
| Release identity | verified from live `/ready` |

The evaluator provisions only its named soak scopes, writes synthetic markers,
retries the exact idempotency key, recalls only the appropriate workspace, and
then checks that the other workspace cannot retrieve that marker.  Its JSON
report is intentionally retained as a local operational artifact rather than
committed to the public repository.

## Interpretation

This raises confidence in PostgreSQL CAS/outbox, Qdrant-backed retrieval,
authorization scope enforcement, and the API contract under modest concurrent
load.  It is not a capacity benchmark, a chaos test, or multi-hour native
OpenClaw/Hermes evidence.  The remaining native-soak gate requires repeated
turns through both actual deployed plugins on `.14`, plus retained evidence of
their lifecycle hooks and worker health.
