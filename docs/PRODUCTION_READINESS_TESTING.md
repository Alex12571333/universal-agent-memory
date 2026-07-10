# Production readiness testing

This project now has five repeatable validation layers beyond ordinary unit
tests.

## 1. Fast unit/API regression

```bash
PYTHONPATH=src .venv/bin/pytest -q
.venv/bin/ruff check src tests scripts
```

This covers the core domain, API boundaries, vault import/export, Qdrant adapter
API contracts, embeddings, privacy redaction, checkpoints, and agent integration
helpers.

## 2. In-process production-readiness eval

```bash
PYTHONPATH=src .venv/bin/python scripts/production_readiness_eval.py \
  --embedding-base-url https://api.openai.com/v1 \
  --embedding-model text-embedding-3-large \
  --embedding-dim 3072
```

Checks:

- concurrent idempotent retains;
- CAS supersede race behavior;
- tenant/status recall isolation;
- secret redaction before storage;
- reflection/conflict inbox and vault dry-run import;
- semantic recall using the configured live OpenAI-compatible embedding endpoint.

## 3. Docker advanced E2E

Start the full local stack with Postgres, Qdrant, NATS, outbox relay and the
embedding worker:

```bash
UAM_QDRANT_URL=http://qdrant:6333 docker compose --profile advanced up -d
```

Then run:

```bash
.venv/bin/python scripts/api_e2e_eval.py --base-url http://127.0.0.1:6798
```

Checks:

- liveness;
- retain idempotency;
- controlled unknown-tenant boundary (`422`, not `500`);
- recall through the API with Qdrant enabled;
- supersede CAS (`201` winner, `409` stale writer);
- conflict inbox;
- Obsidian-style vault export;
- synchronous reindex into Qdrant;
- Russian operator UI;
- Prometheus metrics.

Useful post-check:

```bash
curl -sS http://127.0.0.1:6799/collections/memory_items
```

Expected: collection `memory_items` is green and has points after API writes or
reindexing.

## 4. Live OpenClaw/Hermes soak eval

Run this against the server used by the real agent hosts:

```bash
UAM_API_KEY=... .venv/bin/python scripts/agent_soak_eval.py \
  --base-url http://127.0.0.1:6798 \
  --rounds 5 \
  --parallel 4 \
  --json-report ./ops/agent-soak.json
```

Checks:

- OpenClaw-style retain and recall lifecycle;
- Hermes-style retain and recall lifecycle;
- idempotent retry behavior under parallel execution;
- cross-workspace leakage probes;
- JSON evidence suitable for release review.

For full production evidence, run it from the `.14` OpenClaw/Hermes deployment
path or immediately after those plugins are installed, then preserve
`ops/agent-soak.json` with the release artifacts.

## 5. Live OpenAI-compatible memory LLM eval

Run this against the endpoint used by memory workers:

```bash
.venv/bin/python scripts/real_memory_llm_eval.py \
  --base-url https://api.openai.com/v1 \
  --model gpt-5.6-terra \
  --json-report ./ops/memory-llm.json
```

Checks:

- OpenAI-compatible `/chat/completions` returns final content;
- JSON-object mode works for memory curation;
- the model keeps the current OpenAI-compatible embedding endpoint memory and
  rejects the obsolete fake embeddings claim;
- JSON evidence is suitable for release review.

## Bugs caught by this layer

- Docker image lacked `qdrant-client` while advanced compose advertised Qdrant.
- Advanced compose lacked an embedding worker service.
- Migration runner skipped newer migrations, leaving Docker Postgres without
  `memory_items.status`.
- Unknown tenants caused raw PostgreSQL FK failures and API `500`s.
- `qdrant-client` 1.18 used a different query API than older adapter code.
- `qdrant-client` was not version-pinned to the bundled Qdrant server.
