# Target fail-soft validation — 2026-07-12

Target: the local self-hosted Docker appliance. The test used only the private
local service at `127.0.0.1:6798`; no domain, VPN, reverse proxy, or external
service was involved.

## Controlled Qdrant outage and recovery

One unique synthetic workspace-scoped marker was written before the test. The
Qdrant container was then stopped briefly while PostgreSQL and the memory API
remained running.

| Check | Observed result |
| --- | --- |
| Recall while Qdrant stopped | The synthetic marker was returned. |
| Recall sources while Qdrant stopped | `postgres_lexical` only. |
| `/ready` while Qdrant stopped | `degraded`; PostgreSQL canonical source stayed `healthy`, Qdrant source was `degraded`. |
| After Qdrant start and a real recall | The synthetic marker was returned; sources were `postgres_lexical,qdrant_hybrid`. |
| `/ready` after recovery | `ready`; Qdrant source was `healthy`. |

This verifies the intended fail-soft boundary: an optional vector outage must
not hide canonical memory or make the appliance unavailable. Readiness reflects
the most recent dependency operation, so the recovery check deliberately made
a real recall after Qdrant was reachable again rather than treating a running
container as proof of retrieval recovery.
