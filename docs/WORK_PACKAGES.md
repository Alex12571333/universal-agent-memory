# Work packages standalone memory server

Актуальные владельцы и статусы находятся в GitHub Issues. Этот файл определяет
roadmap и acceptance criteria, но не используется как lock.

| ID | Результат | Зависит от | Acceptance tests |
|---|---|---|---|
| WP-01a ✅ | Psycopg ledger + transactional outbox | schema, ports | rollback, RLS, idempotency |
| WP-01b ✅ | Optimistic revision/CAS | WP-01a | stale revision, concurrent supersede |
| WP-02 ✅ | Qdrant candidate adapter | `CandidateSource` | project filter, fusion, reindex |
| WP-03 ⚠ | Outbox relay + dedupe consumer | events | bounded poison/DLQ replay remains production work |
| WP-04 ✅ | Embedding worker | WP-02/03 | model version, dimensions, reindex |
| WP-05 ✅ | Markdown/PDF ingestion | ledger | provenance, stable chunks, retry |
| WP-06 ⚠ | Checkpoints/working blocks | ledger | CAS fix + optional concurrent PostgreSQL test exist; target evidence remains |
| WP-07 ✅ | Python/TypeScript SDK | OpenAPI | retries, typed errors |
| WP-08 ✅ | Reflection v2 | reflection service | conflict/time/entity fixtures |
| WP-09 ✅ | Scoped API-key authentication baseline | server | deny invalid key, health allowed |
| WP-10 ✅ | Metrics + backup/restore | boundaries | outbox lag, restore drill |

Не входят в roadmap: billing, organizations/customers, Kubernetes control plane,
SSO, SaaS quotas и hosted multi-region operation.

Порядок foundation: WP-01 → WP-03 → WP-02/WP-04 → WP-06/WP-09 → SDK и качество.

## Production backlog

Единственный канонический список текущих production-блокеров находится в
[PRODUCTION_GAP_AUDIT_2026_07_10.md](PRODUCTION_GAP_AUDIT_2026_07_10.md). В P0
входят target proof для database credentials, identity provisioning и checkpoint
CAS, identity-bound authorization, atomic conflict-winner precedence, полное
шифрование чувствительных данных, fail-soft dependencies/readiness, безопасный
multi-workspace reindex, browser authentication и model-endpoint SSRF policy.

Архивные возможности после закрытия runtime-блокеров описаны отдельно в
[ROADMAP_PHASE_4_ARCHIVAL_MEMORY.md](ROADMAP_PHASE_4_ARCHIVAL_MEMORY.md). Статусы
из этого foundation-файла не должны использоваться для production claim.
