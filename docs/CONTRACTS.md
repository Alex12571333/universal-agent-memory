# Контракты совместимости

## Identity и scope

Каждый command, query, event и persisted row содержит `tenant_id`. Всё
workspace-scoped также содержит `workspace_id`. `agent_id` и `thread_id`
опциональны только там, где память действительно shared.

## Event envelope

```json
{
  "id": "uuid",
  "name": "memory.retained.v1",
  "tenant_id": "uuid",
  "workspace_id": "uuid",
  "correlation_id": "uuid",
  "occurred_at": "RFC3339",
  "payload": {}
}
```

- Delivery: at least once.
- Ordering: только внутри выбранного workspace/partition key.
- Consumer обязан дедуплицировать по `event.id`.
- Поле можно добавлять обратно совместимо; удаление/смена смысла требует `v2`.

## Adapter semantics

`MemoryLedger.append` атомарно связывает memory item и idempotency key.
Production implementation объединяет item, provenance и outbox в одну
транзакцию.

`CandidateSource.search` не имеет права возвращать другой tenant. Retrieval
service повторно проверяет boundary как defense in depth.

`ObservationRepository` хранит только derived data. Удаление observation не
удаляет evidence.

## Error taxonomy

Планируемые transport-neutral коды:

- `invalid_argument`;
- `unauthenticated`;
- `permission_denied`;
- `not_found`;
- `revision_conflict`;
- `quota_exceeded`;
- `dependency_unavailable`;
- `index_stale`.

Adapter-specific exceptions не должны выходить через API.
