# Hardening audit — 2026-07-07

Goal: make Universal Agent Memory safer as a production-grade, Docker-first,
long-lived memory server and verify the native OpenClaw/Hermes integrations
against the real `.14` agent host.

## What was tested

- Python server/domain suite: `PYTHONPATH=src .venv/bin/pytest -q`.
- Python SDK suite: `PYTHONPATH=sdk/python .venv/bin/pytest sdk/python/tests -q`.
- TypeScript SDK suite: `npm test --prefix sdk/typescript`.
- Lint: `.venv/bin/ruff check . agent-integrations`.
- Types:
  - `.venv/bin/mypy src sdk/python/uam_client scripts/backup.py scripts/restore.py scripts/export_vault.py scripts/import_vault.py`
  - `PYTHONPATH=agent-integrations .venv/bin/mypy agent-integrations tests/test_agent_integrations.py`
- Compose syntax: `docker compose config --quiet`.
- OpenClaw plugin syntax: `node --check agent-integrations/openclaw/plugin/index.js`.
- `.14` agent host smoke:
  - OpenClaw `2026.6.11` is installed and running.
  - Hermes `0.17.0` is installed.
  - Hermes provider imports, initializes, reports availability, and exposes
    `universal_agent_memory_search` and `universal_agent_memory_add`.
  - OpenClaw plugin imports in an OpenClaw-compatible module tree and registers
    `agent_turn_prepare`, `after_tool_call`, and `agent_end`.

Docker image build was attempted, but the local Docker daemon was unavailable:
`Cannot connect to the Docker daemon at unix:///Users/aleksandrbogdanov/.docker/run/docker.sock`.
The compose file itself validates.

## Bug found and fixed

The OpenClaw plugin used `definePluginEntry` from `openclaw/plugin-sdk`.
The real `.14` OpenClaw runtime does not export that symbol from
`openclaw/plugin-sdk`, so the plugin passed syntax checks but failed at runtime
import.

Fix:

- export a plain default plugin entry object with `register(api)`;
- update OpenClaw adapter documentation;
- add a regression assertion so `definePluginEntry` is not reintroduced.

## Current production-readiness status

The core server, SDKs, memory lifecycle, graph edges, privacy guard, conflict
review flow, local vault UI, backups, and native OpenClaw/Hermes adapters are in
place and covered by automated tests.

The remaining production gap is less about core correctness and more about
operations: repeatable live deployment, live adapter installation, restoration
drills, observability budgets, and chaos/load testing.

## Next hardening backlog

### P0 — live Docker/server confidence

1. Add a `scripts/smoke_docker.sh` that starts the compose stack, waits for
   `/health`, writes one memory, recalls it, exports the vault, and shuts down.
2. Add a CI job that runs compose smoke with Postgres + Qdrant + NATS.
3. Add a restore drill test: backup a non-empty server, restore into a clean
   volume, assert memory/checkpoint/edge counts match.
4. Add health checks for Qdrant collection readiness and NATS stream readiness,
   not only process readiness.

### P0 — real agent installation path

1. Add `scripts/install_openclaw_plugin.sh` with dry-run and rollback mode.
2. Add `scripts/install_hermes_provider.sh` with dry-run and rollback mode.
3. Add `.14`-style smoke docs: install into a test profile, run one agent turn,
   assert a memory is retained and recall context is injected on the next turn.
4. Add plugin compatibility checks for future OpenClaw SDK changes: import
   plugin, inspect exported entry, fake-register hooks.

### P1 — eternal memory quality

1. Add a scheduled memory gardener:
   - detect stale memories;
   - propose supersession instead of deleting;
   - merge duplicates only when evidence and timestamps agree;
   - keep audit trails for every status transition.
2. Add temporal conflict scoring: recent factual updates should outrank older
   contradicting memories without destroying history.
3. Add graph-aware recall expansion: after vector recall, include linked
   decisions, superseded facts, source documents, and related tasks within a
   configurable token budget.
4. Add confidence/evidence fields to memory records and show them in `/ui`.

### P1 — Obsidian-like editing/review

1. Add edit/retire/supersede controls to the local `/ui`.
2. Add graph/backlink view for related memories.
3. Add inbox filters by agent, workspace, layer, status, conflict severity, and
   privacy state.
4. Add a “why recalled?” trace view that explains retrieval source, vector
   score, graph expansion, freshness, and status policy.

### P1 — privacy and safety

1. Add configurable secret detectors per workspace.
2. Add irreversible redaction flow with audit entry and backup implications.
3. Add “quarantine” status for suspicious memories until a human approves them.
4. Add privacy regression fixtures with realistic keys/tokens/URLs.

### P2 — scale and operations

1. Add load tests for concurrent agents writing/recalling in the same workspace.
2. Add retention/compaction metrics: ingest latency, recall latency, conflict
   queue length, graph edge count, embedding lag.
3. Add rolling migration tests against realistic old schemas.
4. Add multi-node deployment notes for Postgres, Qdrant, NATS, and server
   replicas.

