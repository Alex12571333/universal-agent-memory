# Changelog

## Unreleased

- Signed, content-addressed release evidence v2 with source commit, immutable
  image digest, deployment identity, artifact SHA-256 checks, safe paths,
  freshness enforcement and operator HMAC verification.
- Provider-neutral OpenAI-compatible embedding and memory-LLM configuration.
- Canonical production-readiness audit with explicit runtime blockers and
  required target-environment evidence.

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
