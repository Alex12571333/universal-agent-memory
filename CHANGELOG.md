# Changelog

## Unreleased

- Added `EmbeddingService` to coordinate generation and indexing of dense memory representations (WP-04).
- Added `EmbeddingClient` port protocol and `FakeEmbeddingClient` adapter for test-driven embedding workflows.
- Added an executable `embedding-worker` daemon to process retention events and generate vector embeddings asynchronously.
- Added support for full workspace reindexing via `EmbeddingService.reindex_all`.

- Added deterministic Markdown/PDF extraction with binary checksum and page provenance.
- Added a base64 document-ingestion endpoint with stable retry identities.
- Added optional constant-time bearer API-key protection with public health checks.
- Added dependency-free Python and platform-fetch TypeScript SDKs.
- Added stable retain idempotency keys, bounded retries and typed SDK errors.
- Added leased PostgreSQL outbox delivery to NATS JetStream.
- Added retry/dead-letter state and durable per-consumer event deduplication.
- Added a standalone `outbox-relay` Docker service and end-to-end coverage.
- Added idempotent forward-only migrations for existing Docker volumes.
- Added Qdrant dense+sparse `CandidateSource` adapter with hybrid search, upsert,
  delete and full reindex (WP-02).
- Bootstrap optionally connects Qdrant via `UAM_QDRANT_URL` / `UAM_EMBEDDING_DIM`.
- Docker Compose forwards Qdrant env vars to `memory-server`.
- Reframed the project as a self-hosted Docker memory server rather than a SaaS.
- Added the `memory-server` image, durable runtime composition and standalone API defaults.
- Added GitHub issue claiming, live agent status and auto-merge collaboration scripts.
- Added PostgreSQL lexical recall for the default Docker profile.
- Implemented PostgreSQL canonical memory, provenance, idempotency and transactional outbox.
- Forced tenant RLS even for table owners and added PostgreSQL integration coverage.
- Replaced the split ledger/event retention boundary with one atomic port.

## 0.1.0

- Создан модульный memory-plane foundation.
- Добавлены модели восьми слоёв памяти и scope-модель.
- Реализованы retain, recall, context compile и reflection.
- Добавлены in-memory adapters, REST API, SQL/RLS и тесты.
