# Target validation — durable worker heartbeat readiness

Date: 2026-07-14

Target: local LAN node `192.168.0.14`

Validated code tree: `279503f`

## Scope

The target run cloned the published hardening branch into a temporary
directory, started a fresh isolated PostgreSQL 17 container, applied every
migration through `017_worker_heartbeats.sql`, provisioned the restricted
`memory_runtime` role and ran both the repository integration test and a real
FastAPI `/ready` probe through that role.

The running OpenClaw/Hermes agents, their extensions, workspaces, normal
Obelisk database and model endpoints were not used or modified.

## Result

`PASS`

The target run proved:

- the database connection used by the repository and API probes was a
  non-superuser runtime role;
- all three absent production roles made `/ready` return HTTP 503;
- fresh heartbeats for `outbox-relay`, `embedding-worker` and
  `maintenance-worker` made `/ready` return HTTP 200;
- a graceful `stopping` heartbeat immediately made the affected role unready
  and `/ready` returned HTTP 503 again;
- the API response did not expose worker IDs;
- freshness used the PostgreSQL clock, a two-minute-old record was stale and a
  record older than 24 hours was pruned;
- the temporary PostgreSQL container, clone and virtual environment were
  removed after the test.

Target summary:

```text
target_commit=279503f
target_postgres_role=non_superuser
target_missing_gate=PASS
target_ready_recovery=PASS
target_stopping_gate=PASS
target_worker_identity_redaction=PASS
worker_heartbeat_target=PASS
target_cleanup=PASS
```

## Remaining boundary

A heartbeat proves recent process liveness, not successful completion of every
queued job. Release monitoring must therefore retain both heartbeat gates and
the existing outbox lag, dead-letter, embedding failure and retrieval-source
alerts. A database outage makes readiness fail closed because the canonical
heartbeat snapshot cannot be read.
