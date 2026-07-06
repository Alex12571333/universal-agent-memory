# Контракты совместимости

## Server/project identity и scope

В текущей версии wire-поля `tenant_id` и `workspace_id` сохранены для
совместимости с foundation. В standalone deployment они означают соответственно
`server_id` и `project_id`, а не SaaS customer/account. API подставляет fixed
defaults, поэтому обычный клиент их не передаёт. `agent_id` и `thread_id`
разделяют память агентов внутри проекта.

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
- Relay использует lease; подтверждает outbox только после JetStream publish ack.
- Ошибка освобождает lease, а исчерпание attempts переводит событие в dead letter.
- Consumer lease не обещает exactly-once side effects: handler всё равно должен
  быть идемпотентным на случай падения после side effect и до completion mark.
- Поле можно добавлять обратно совместимо; удаление/смена смысла требует `v2`.

## Adapter semantics

`MemoryLedger.append` атомарно связывает memory item и idempotency key.
Production implementation объединяет item, provenance и outbox в одну
транзакцию.

`MemoryLedger.supersede_if_current` добавляет новую immutable revision только
если указанный `expected_revision` всё ещё является head для цепочки
`supersedes_id`. Успешный supersede публикует обычное `memory.retained.v1`, чтобы
embedding/reflection workers обработали replacement тем же pipeline. Stale write
возвращается через transport-neutral `revision_conflict`.

`CandidateSource.search` не имеет права возвращать другой project/server.
Retrieval service повторно проверяет boundary как defense in depth.

`ObservationRepository` хранит только derived data. Удаление observation не
удаляет evidence.

## Error taxonomy

Планируемые transport-neutral коды:

- `invalid_argument`;
- `unauthenticated`;
- `permission_denied`;
- `not_found`;
- `revision_conflict`;
- `dependency_unavailable`;
- `index_stale`.

Adapter-specific exceptions не должны выходить через API.
