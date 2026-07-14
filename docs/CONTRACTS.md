# Контракты API и данных

## Server/project identity и scope

Wire-поля `tenant_id` и `workspace_id` используются как стабильные идентификаторы
локального deployment и проекта: `tenant_id` означает `server_id`, а
`workspace_id` означает `project_id`. API подставляет fixed defaults, поэтому
обычный standalone-клиент их не передаёт. `agent_id` и `thread_id` разделяют
память агентов внутри проекта.

`POST /v1/identities/provision` является operator/admin boundary для создания
стабильного `agent_id` и опционального `thread_id`. Операция идемпотентно
обновляет display metadata и status, но никогда не переносит существующий ID в
другой tenant/workspace. Agent-scoped ключ не может вызывать этот endpoint.
После provisioning внешние ключи memory/checkpoint/conversation ledger валидны.
В production каждый `agent` principal обязан иметь server-side binding к
tenant/workspace/agent. Middleware отклоняет forged IDs и чужие threads;
private recall дополнительно фильтруется в canonical, vector и fusion layers.
Саморегистрация агентским ключом не допускается.

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

`MemoryLedger.filter_recallable_heads` является canonical batched guard для
retrieval adapters: rejected/archived tombstone и любая revision с дочерним
`supersedes_id` не recallable. Qdrant проверяет найденные IDs этим методом до
fusion, поэтому eventual lag удаления vector не возвращает старый текст.

`ConflictReviewRepository.apply_resolution` — единая transaction boundary для
accepted/overridden review. Она CAS-проверяет все независимые recallable loser
roots, добавляет archived tombstone revisions, при необходимости восстанавливает
выбранный historical winner как active revision, пишет outbox events и review с
`applied_memory_id`. Любой stale parent откатывает всю группу. Применённое
решение immutable; идентичный retry возвращает прежний результат.

`CheckpointStore.save_if_head` использует expected head `0` для первой
ревизии. PostgreSQL сериализует writers transaction advisory lock по
tenant/thread, затем читает ordered head и вставляет следующую revision. Это
защищает как первый save, так и последующие CAS updates от lost update.

`CandidateSource.search` не имеет права возвращать другой project/server.
Retrieval service повторно проверяет boundary/status как defense in depth.

`ObservationRepository` хранит только derived data. Удаление observation не
удаляет evidence.

`ConversationLedger.append_turn` хранит raw transcript turn отдельно от
curated memory. Запись в `/v1/conversations/turns` не создаёт `MemoryItem`, не
попадает в `/v1/memory/recall` напрямую и не должна инжектиться в prompt без
отдельной обработки. Это audit/replay/reprocessing слой: Куратор памяти может
позже превратить transcript в curated facts через обычный append-only memory
pipeline.

Политика `curated_only` использует raw turn только как staging-запись. После
успешного `ConversationCurator.curate_turn` ledger обязан необратимо заменить
тексты сообщений на `[PURGED_AFTER_CURATION]`, сохранив идентификаторы, роли и
retention metadata для audit/idempotency. Если purge не подтверждён, операция
курации возвращает ошибку; повтор с тем же idempotency key завершает cleanup без
создания второго `MemoryItem`. `raw_only` запрещает курацию, а
`raw_and_curated` сохраняет исходный transcript после неё.

Для незавершённого `curated_only` turn сервер ставит `expires_at` из
`UAM_CONVERSATION_CURATED_ONLY_TTL_SECONDS` (по умолчанию 24 часа). Operator
maintenance endpoint `POST /v1/workspaces/{workspace_id}/conversations/purge-expired`
пакетно заменяет просроченный текст на `[PURGED_AFTER_CURATION]` с причиной
`purged_after_expiry`; вызов безопасен для повторов и фиксируется в audit log.
Production scheduler обязан вызывать его чаще заданного TTL.

`ConversationCurator.curate_turn` сначала создаёт evidence-backed
`MemoryProposal` с `source_turn_id`, а `/v1/memory/recall` его не видит. По
умолчанию API включает узкую автоматическую policy: она может принять только
high-confidence preference/decision/task/procedure с literal evidence quote,
без temporal markers и без deterministic fallback. Все остальные результаты
остаются открытыми proposal для review. Так LLM не становится источником
автоматически принятой истины: автоматизация допускается лишь при проверяемой
связи с конкретным исходным turn. Передайте `auto_accept: false`, чтобы всегда
оставлять proposal на ручную проверку.

`MemoryProposalService.submit` хранит предлагаемое изменение памяти отдельно от
`MemoryItem`. Запись в `/v1/memory/proposals` не попадает в recall и не запускает
embedding jobs. Это входной шлюз для агентов: proposal + evidence + confidence
сначала проходят privacy guard и review/curation, а уже затем могут стать
append-only memory.

`GET /v1/memory/proposals` возвращает не более 200 записей и использует
стабильный descending keyset cursor. Для следующей страницы клиент передаёт
оба поля `before_created_at` и `before_proposal_id`, полученные как
`next_before_created_at` и `next_before_proposal_id`; передача только одного
поля отклоняется. Новые proposal, появившиеся между запросами, не сдвигают уже
выбранную границу страницы.

`MemoryProposalService.accept` создаёт `MemoryItem` через обычный
`RetentionService` с provenance `proposal://{proposal_id}` и идемпотентным ключом
`accept-proposal:{proposal_id}`. PostgreSQL блокирует proposal и пишет canonical
memory, idempotency record, outbox event и `accepted_memory_id` в одной
транзакции. Повторный accept не создаёт дубль. Reject обновляет только proposal
status и никогда не создаёт recallable memory.

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
- `index_freshness` — durable per-workspace counts of active heads whose
  `embed-v1` delivery is pending, processing, dead-lettered or missing.

Adapter-specific exceptions не должны выходить через API.
