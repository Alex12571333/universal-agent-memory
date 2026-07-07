# Phase 3 roadmap: hardened eternal memory

Latest hardening audit: [HARDENING_AUDIT_2026_07_07.md](HARDENING_AUDIT_2026_07_07.md).
DGX Spark embedding notes: [DGX_SPARK_EMBEDDINGS.md](DGX_SPARK_EMBEDDINGS.md).
Production readiness tests: [PRODUCTION_READINESS_TESTING.md](PRODUCTION_READINESS_TESTING.md).

Phase 2 turned the project into a self-hosted memory server with native agent
integrations, vault import/export, conflict review and a local operator UI.
Phase 3 is about making the memory layer safer, more autonomous and easier to
operate for months or years without corrupting itself.

## WP-16 Secrets/PII guard

Prevent accidental long-term storage of credentials and high-risk personal
data.

- deterministic detectors for API keys, bearer tokens, passwords, private keys,
  SSNs and payment-card-like values;
- configurable action: `redact`, `reject`, `metadata_only`, `allow`;
- audit metadata that records finding kinds/counts without storing the raw
  secret;
- tests with representative fixtures.

## WP-17 Retrieval demotion and memory lifecycle

Make status first-class in retrieval instead of treating all memory equally.

- statuses: active, stale, disputed, rejected, archived, pinned;
- default recall excludes rejected/archived and demotes disputed/unreviewed
  conflict cases;
- working memory expiration;
- pinned core memory;
- audit history stays readable.

## WP-18 Live OpenClaw/Hermes smoke tests on `.14`

Install the adapters into the real OpenClaw/Hermes runtimes and run an
end-to-end test against the Docker memory server.

- install scripts;
- health checks for adapters;
- turn-level recall/retain smoke;
- rollback instructions.

## WP-19 UI edit workflows

The current `/ui` is intentionally read/review oriented. Add safe editing.

- supersede memory from UI;
- approve/reject conflict case;
- promote to core;
- redaction review queue;
- no destructive direct update.

## WP-20 Graph and maintenance jobs

Use graph edges and scheduled maintenance to keep memory coherent.

- `supports`, `contradicts`, `derived_from`, `same_entity`, `owned_by_agent`;
- graph-backed neighbor retrieval;
- scheduled dedupe/conflict/stale scans;
- backup verification;
- automatic vault export.

## WP-21 Production ops hardening

Make deployment boring.

- ✅ Docker advanced profile runs API, Postgres, NATS, outbox relay, embedding
  worker, Qdrant and MinIO together;
- ✅ readiness/E2E scripts cover retain, recall, CAS, conflict review, vault
  export, reindex, `/ui` and `/metrics`;
- ✅ migration smoke test verifies every numbered SQL migration is registered;
- ✅ Qdrant client dependency is pinned to the Docker server line;
- TLS/reverse-proxy example;
- key rotation;
- retention policies;
- structured logs;
- restore drill docs;
- migration smoke test in CI.
