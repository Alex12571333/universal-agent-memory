# Target validation — redacted retrieval traversal

Date: 2026-07-14

Target: local LAN node `192.168.0.14`

Validated code tree: `009d838`

## Scope

The target run cloned the published feature branch, started a fresh isolated
PostgreSQL 17 container, applied all migrations, provisioned the restricted
`memory_runtime` role and exercised retain, recall, durable audit storage and
operator replay through the real FastAPI service graph.

The normal Obelisk database, OpenClaw/Hermes agents, their workspaces and model
endpoints were not used or modified.

## Result

`PASS`

The target run proved:

- the API and audit inspection used a non-superuser runtime role;
- PostgreSQL lexical retrieval produced one source step and weighted fusion
  produced one final selected memory;
- the ordered traversal survived durable JSON storage and replay unchanged;
- neither the raw query nor canonical memory text appeared in the replay or
  stored audit metadata;
- trace rows contained only the fixed aggregate schema;
- the temporary database, checkout and environment were removed afterwards.

Target summary:

```text
target_postgres_role=non_superuser
target_durable_traversal=PASS
target_query_redaction=PASS
target_memory_text_redaction=PASS
target_replay_round_trip=PASS
target_commit=009d838
retrieval_traversal_target=PASS
target_cleanup=PASS
```

## Remaining boundary

Traversal counts explain the retrieval pipeline but do not prove that every
selected memory is true. Canonical provenance, supersession/conflict rules and
proposal acceptance remain the authority for memory correctness. Optional
dependency failures expose only a bounded exception class, never the exception
message or configured endpoint.
