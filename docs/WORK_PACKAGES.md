# Work packages standalone memory server

Актуальные владельцы и статусы находятся в GitHub Issues. Этот файл определяет
roadmap и acceptance criteria, но не используется как lock.

| ID | Результат | Зависит от | Acceptance tests |
|---|---|---|---|
| WP-01a ✅ | Psycopg ledger + transactional outbox | schema, ports | rollback, RLS, idempotency |
| WP-01b ✅ | Optimistic revision/CAS | WP-01a | stale revision, concurrent supersede |
| WP-02 | Qdrant dense+sparse adapter | `CandidateSource` | project filter, fusion, reindex |
| WP-03 ✅ | Outbox relay + dedupe consumer | events | crash/replay, poison event |
| WP-04 | Embedding worker | WP-02/03 | model version, dimensions, reindex |
| WP-05 ✅ | Markdown/PDF ingestion | ledger | provenance, stable chunks, retry |
| WP-06 | Checkpoints/working blocks | ledger | CAS, compaction, replay |
| WP-07 ✅ | Python/TypeScript SDK | OpenAPI | retries, typed errors |
| WP-08 ✅ | Reflection v2 | reflection service | conflict/time/entity fixtures |
| WP-09 ✅ | API key for trusted LAN exposure | server | deny invalid key, health allowed |
| WP-10 ✅ | Metrics + backup/restore | boundaries | outbox lag, restore drill |

Не входят в roadmap: billing, organizations/customers, Kubernetes control plane,
SSO, SaaS quotas и hosted multi-region operation.

Порядок foundation: WP-01 → WP-03 → WP-02/WP-04 → WP-06/WP-09 → SDK и качество.

## Phase 2

Следующий этап описан в [ROADMAP_PHASE_2.md](ROADMAP_PHASE_2.md). Он переводит
проект от working memory server к production-grade “вечной памяти”:

- Obsidian/vault mode для human-readable контроля;
- реальные embedding providers;
- conflict resolver и review inbox;
- web UI для человека;
- native plugin/runtime интеграции для OpenClaw и Hermes;
- secrets/PII guard;
- temporal lifecycle/status policies;
- graph layer;
- scheduled maintenance jobs;
- production ops hardening.
