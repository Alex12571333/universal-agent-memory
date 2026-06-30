# Независимые work packages

Каждый пакет можно отдать отдельному агенту. Зависимости указаны явно.

| ID | Результат | Зависит от | Основные acceptance tests |
|---|---|---|---|
| WP-01a ✅ | Psycopg `MemoryLedger` + transactional outbox | schema, ports | rollback, RLS, idempotency |
| WP-01b | Optimistic revision/CAS workflow | WP-01a | stale revision, concurrent supersede |
| WP-02 | Qdrant named dense+sparse adapter | `CandidateSource` | tenant filter, fusion fixture, delete/reindex |
| WP-03 | NATS outbox relay + dedupe consumer | events | crash/replay, poison message, DLQ |
| WP-04 | S3/MinIO object adapter | provenance conventions | checksum, prefix isolation, presigned URL |
| WP-05 | Text/Markdown/PDF ingestion | object + ledger | exact provenance, chunk stability, retry |
| WP-06 | Embedding worker | events + Qdrant | model version, dimensions, reindex |
| WP-07 | Entity/temporal graph worker | events + graph port RFC | validity windows, provenance, traversal ACL |
| WP-08 | Checkpoints/working blocks | ledger | optimistic revision, compaction, replay |
| WP-09 | Policy/auth | API + tenant model | deny-by-default, private/team/thread matrices |
| WP-10 | Python/TypeScript SDKs | OpenAPI | retries, idempotency header, typed errors |
| WP-11 | Reflection v2 | reflection service | negation/time/entity conflict fixtures |
| WP-12 | Observability | all boundaries | trace propagation, outbox/index lag metrics |
| WP-13 | Backup/restore drills | infrastructure | PITR, Qdrant rebuild, object manifest |
| WP-14 | Kubernetes/Helm | Compose semantics | probes, secrets, PVC, disruption tests |

## Общий порядок интеграции

WP-01 → WP-03 → WP-02/WP-04 → WP-05/WP-06 → WP-08/WP-09 → остальные.
Параллельно WP-10 может работать от API contract, а WP-11 — от in-memory ports.
