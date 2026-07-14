# Changelog

## Unreleased

- Signed, content-addressed release evidence v2 with source commit, immutable
  image digest, deployment identity, artifact SHA-256 checks, safe paths,
  freshness enforcement and operator HMAC verification.
- Provider-neutral OpenAI-compatible embedding and memory-LLM configuration.
- Canonical production-readiness audit with explicit runtime blockers and
  required target-environment evidence.

### Added

- Deterministic RU/EN automatic-recall gate for shared, OpenClaw and Hermes
  integrations with `off`, `adaptive` and `always` modes.
- Text-free in-process metrics for gate decisions/reasons, injected token totals
  and recall latency.
- Explicit configurable research tier and per-request context-per-layer bound.
- Untrusted-reference prompt framing for every automatic memory injection.

### Changed

- Automatic recall defaults are now `top_k=6`, 1200 tokens, 3 records per layer
  and minimum score 0.45.
- The explicit research tier defaults to `top_k=10`, 2500 tokens and 6 records
  per layer.

## 0.1.0

- Self-hosted FastAPI memory server with PostgreSQL canonical storage,
  transactional outbox and tenant RLS.
- Qdrant vector indexing, NATS JetStream relay and asynchronous embedding
  worker.
- Append-only retain/supersede, provenance, raw conversation ledger, explicit
  curation, proposals, conflicts, graph edges, reflection and checkpoints.
- React operator dashboard, human-readable Markdown vault and safe CAS import.
- Native OpenClaw and Hermes integration adapters plus Python/TypeScript SDKs.
- Privacy redaction, scoped API keys, audit export/retention, backup/restore,
  observability templates and production preflight tooling.
