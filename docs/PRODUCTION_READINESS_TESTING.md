# Production readiness testing

Production verification is divided into repository, live-service,
target-environment and signed-release layers. No single layer is sufficient for
a production claim.

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
  --embedding-base-url "$UAM_EMBEDDING_BASE_URL" \
  --embedding-model "$UAM_EMBEDDING_MODEL" \
  --embedding-dim "$UAM_EMBEDDING_DIM"
```

Checks:

- concurrent idempotent retains;
- CAS supersede race behavior;
- tenant/status recall isolation;
- secret redaction before storage;
- reflection/conflict inbox and vault dry-run import;
- semantic recall using the configured live OpenAI-compatible embedding endpoint.

For the Qdrant-backed live memory flow, use the provider-neutral profile unless
you intentionally need the OpenAI-hosted embedding profile:

```bash
PYTHONPATH=src .venv/bin/python scripts/real_memory_flow_eval.py \
  --provider openai-compatible \
  --base-url "$UAM_EMBEDDING_BASE_URL" \
  --model "$UAM_EMBEDDING_MODEL" \
  --dimension "$UAM_EMBEDDING_DIM"
```

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

For full production evidence, run it through the deployed OpenClaw/Hermes
runtime path after the plugins are installed, then preserve
`ops/agent-soak.json` with the release artifacts.

## 5. Live OpenAI-compatible memory LLM eval

Run this against the endpoint used by memory workers:

```bash
.venv/bin/python scripts/real_memory_llm_eval.py \
  --base-url "$UAM_MEMORY_LLM_BASE_URL" \
  --model "$UAM_MEMORY_LLM_MODEL" \
  --json-report ./ops/memory-llm.json
```

Checks:

- OpenAI-compatible `/chat/completions` returns final content;
- JSON-object mode works for memory curation;
- the model selects newer explicit evidence and excludes the superseded value
  from the curated proposal;
- JSON evidence is suitable for release review.

## 6. Live embedding regression

Run `scripts/real_embedding_eval.py` against the exact provider/model/dimension
used by the embedding worker and preserve `ops/embedding.json`. This tests
semantic distinctions only. Do not use an embedding model to decide which
contradictory fact is current: run `scripts/real_memory_flow_eval.py` as well.
It proves the actual safety boundary with the real provider: retain → CAS
supersede → index → recall of the active head only.

## 7. Target behavior gates

Run and preserve:

- `scripts/conversation_pipeline_eval.py`;
- `scripts/load_smoke_eval.py`;
- `scripts/ui_walkthrough_eval.py`.

These validate raw→curated memory behavior, concurrent correctness/latency and
operator workflows against the release server.

## 8. Operations gates

Deployment boundary, mounted secrets, schedules, monitoring, backup/restore,
audit retention, branch protection and signed vault import each produce a JSON
report described in [RELEASE_EVIDENCE.md](RELEASE_EVIDENCE.md).

## 9. Signed release bundle

After all reports exist, seal them with
`scripts/generate_release_evidence_manifest.py` and verify the v2 manifest with
`scripts/verify_release_evidence.py`. The verifier binds reports to one commit,
image digest and deployment, and rejects stale, modified or cross-target
evidence.

The remaining runtime blockers and missing integration tests are listed in
[PRODUCTION_GAP_AUDIT_2026_07_10.md](PRODUCTION_GAP_AUDIT_2026_07_10.md).
