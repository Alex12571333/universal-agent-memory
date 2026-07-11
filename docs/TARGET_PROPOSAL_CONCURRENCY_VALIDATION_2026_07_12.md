# Target proposal concurrency validation — 2026-07-12

Target: the local self-hosted Docker appliance and its runtime PostgreSQL
application role. This check used one synthetic proposal only; no user memory,
secret, or transcript is included in this report.

## Concurrent acceptance

Two authenticated operator requests accepted the same open proposal at the
same time. The observed result was:

| Invariant | Result |
| --- | --- |
| Both review responses | `proposal.status=accepted` |
| Canonical memory IDs | Identical in both responses |
| Creation flags | Exactly one `true`, one `false` |

The PostgreSQL proposal row lock and workspace-namespaced idempotency key thus
produced one immutable canonical memory and one outbox event, rather than two
memories from a concurrent operator retry.

## Remaining boundary evidence

This validates the live concurrent-success path only. The separate
failure-injection test — proving that an insertion/outbox failure leaves the
proposal open and creates no memory — remains a required release gate.
