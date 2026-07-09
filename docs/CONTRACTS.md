# Контракты API и данных

## Server/project identity и scope

Wire-поля `tenant_id` и `workspace_id` используются как стабильные идентификаторы
локального deployment и проекта: `tenant_id` означает `server_id`, а
`workspace_id` означает `project_id`. API подставляет fixed defaults, поэтому
обычный standalone-клиент их не передаёт. `agent_id` и `thread_id` разделяют
память агентов внутри проекта.

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

`ConversationLedger.append_turn` хранит raw transcript turn отдельно от
curated memory. Запись в `/v1/conversations/turns` не создаёт `MemoryItem`, не
попадает в `/v1/memory/recall` напрямую и не должна инжектиться в prompt без
отдельной обработки. Это audit/replay/reprocessing слой: Куратор памяти может
позже превратить transcript в curated facts через обычный append-only memory
pipeline.

`ConversationCurator.curate_turn` является первым deterministic мостом из raw
ledger в recallable memory. Он создаёт `MemoryItem` с provenance
`conversation://{turn_id}` через обычный `RetentionService`, поэтому embedding,
outbox, graph/reflection jobs и idempotency работают тем же путём, что и для
ручного `/v1/memory/retain`.

`MemoryProposalService.submit` хранит предлагаемое изменение памяти отдельно от
`MemoryItem`. Запись в `/v1/memory/proposals` не попадает в recall и не запускает
embedding jobs. Это входной шлюз для агентов: proposal + evidence + confidence
сначала проходят privacy guard и review/curation, а уже затем могут стать
append-only memory.

`MemoryProposalService.accept` создаёт `MemoryItem` через обычный
`RetentionService` с provenance `proposal://{proposal_id}` и идемпотентным ключом
`accept-proposal:{proposal_id}`. Повторный accept не создаёт дубль. Reject
обновляет только proposal status и никогда не создаёт recallable memory.

`AuditLogService.record` пишет append-only `AuditEvent` для operator/agent
действий: retain, supersede, proposal review, conflict decision, vault import,
vault archive и model-settings changes. Audit export доступен через
`GET /v1/audit/events` только ключам `operator`/`admin`. PostgreSQL хранит
audit rows под тем же tenant RLS boundary; audit не заменяет outbox events,
потому что outbox описывает async work, а audit описывает human/agent action.

`ApiKeyRegistryService` хранит metadata scoped API keys без bearer secrets:
`secret_fingerprint`, scopes, created/last-used/revoked timestamps. Env secrets
остаются в `.env.production` или external secret manager; registry нужен для
operator review и incident response. Revoked fingerprints отклоняются middleware
до route execution, даже если старый secret ещё присутствует в env.

Reflection v2 остаётся deterministic и offline-safe: сервис извлекает простые
слоты `subject/predicate/value`, создаёт observations только для повторов или
конфликтов и помечает устаревшие значения `stale=true`. Повторный запуск с тем
же evidence не создаёт дубликаты. Будущая LLM/embedding-reflection реализация
должна сохранить эти свойства: audit через `evidence_ids`, отсутствие мутаций
raw memory и transport-neutral `stale` для конфликтов.

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
