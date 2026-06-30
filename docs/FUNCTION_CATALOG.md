# Каталог публичных функций

Это карта ответственности, побочных эффектов и контрактов. Приватные helpers с
префиксом `_` документируются docstring рядом с кодом.

## Domain — `domain/models.py`

| Функция | Назначение | Побочные эффекты / ошибки |
|---|---|---|
| `MemoryItem.__post_init__()` | Проверяет текст, score ranges, validity и thread scope | `ValueError` при нарушении инварианта |
| `MemoryItem.is_valid_at(moment)` | Point-in-time проверка temporal validity | Нет |
| `MemoryItem.supersede(text, confidence=...)` | Создаёт новый immutable revision с `supersedes_id` | Генерирует UUID/time; старый item не меняет |
| `Observation.__post_init__()` | Запрещает belief без summary/evidence | `ValueError` |
| `ContextPackage.render_markdown()` | Детерминированно рендерит sections для LLM | Нет |

## Contracts — `contracts/dto.py`, `contracts/events.py`

| Функция | Назначение | Ограничение |
|---|---|---|
| `RecallQuery.__post_init__()` | Валидирует query и `top_k` | `1..100` |
| `ContextRecipe.__post_init__()` | Валидирует token budget | минимум 128 |
| `IntegrationEvent.__post_init__()` | Требует версию в имени события | пример `memory.retained.v1` |

## Retention — `services/retention.py`

| Функция | Вход → выход | Гарантия |
|---|---|---|
| `RetentionService.__init__(store)` | Atomic retention port → service | Не открывает соединения |
| `RetentionService.retain(command)` | `RetainCommand` → `RetainResult` | Append-only; memory и outbox фиксируются одной транзакцией |

## Ingestion — `services/ingestion.py`

| Функция | Вход → выход | Гарантия |
|---|---|---|
| `IngestDocumentCommand.__post_init__()` | Parameters → validated command | Проверяет текст, размер и overlap |
| `TextChunker.split(text, size, overlap)` | Text → stable `(start,end,chunk)` | Paragraph/sentence-aware, deterministic |
| `IngestionService.__init__(retention, chunker=None)` | Retain seam → service | Parser можно заменить независимо |
| `IngestionService.ingest_text(command)` | Document → `IngestResult` | SHA-256 provenance и idempotency на каждый chunk |
| `MarkdownParser.parse(data)` | Markdown bytes → readable text | Не исполняет HTML/code |
| `PdfParser.parse_pages(data)` | PDF bytes → page texts | Optional pypdf; rejects image-only PDF |
| `DocumentIngestor.ingest_markdown()` | Binary source → memory chunks | Binary checksum и stable origin |
| `DocumentIngestor.ingest_pdf()` | PDF pages → memory chunks | `#page=N` provenance, общий checksum |

## Retrieval — `services/retrieval.py`

| Функция | Вход → выход | Гарантия |
|---|---|---|
| `RetrievalService.__init__(sources, weights)` | Sources/weights → service | Требует source; веса = 1.0 |
| `RetrievalService.recall(query)` | `RecallQuery` → ranked `RecallResult` | Tenant/workspace/validity filters применяются после каждого adapter |

## Context — `services/context.py`

| Функция | Вход → выход | Гарантия |
|---|---|---|
| `ContextCompiler.compile(recall, recipe)` | Ranked recall → `ContextPackage` | Не превышает budget; core/working имеют приоритет |
| `ContextCompiler.estimate_tokens(text)` | text → integer | Portable heuristic `ceil(chars/4)` |

## Reflection — `services/reflection.py`

| Функция | Вход → выход | Гарантия |
|---|---|---|
| `ReflectionService.__init__(ledger, observations)` | Ports → service | Нет I/O до вызова |
| `ReflectionService.reflect(tenant, workspace)` | scope → observations | Raw evidence не меняется; baseline требует ≥2 совпадающих facts |

## In-memory adapter — `adapters/in_memory.py`

| Функция | Назначение |
|---|---|
| `InMemoryMemoryStore.append()` | Thread-safe append и idempotency |
| `get()` | Tenant-safe lookup |
| `list_for_workspace()` | Канонический fallback/listing |
| `search()` | Dependency-free lexical retrieval + metadata filters |
| `publish()` | In-memory idempotent outbox |
| `save()` / `list_observations()` | Derived observation storage |
| `InMemoryObservationRepository.*` | Адаптирует observation port без конфликта имён |

## Workers — `workers/handlers.py`

| Функция | Назначение | Семантика |
|---|---|---|
| `RetainedEventRouter.__init__(handlers)` | Регистрирует handlers по job name | Handler можно тестировать отдельно |
| `RetainedEventRouter.handle(event)` | Dispatch jobs из `memory.retained.v1` | Неизвестные event/job пропускаются |

## Outbox — `services/outbox.py`, `services/consumer.py`

| Функция | Назначение | Семантика |
|---|---|---|
| `OutboxRelay.run_once()` | Lease → JetStream publish → ack/release | At-least-once, bounded batch |
| `IdempotentEventConsumer.handle()` | Защищает handler от completed/concurrent duplicates | Failed handler освобождает lease |
| `PostgresMemoryLedger.claim_outbox()` | Конкурентно выдаёт due events | `FOR UPDATE SKIP LOCKED` |
| `mark_outbox_published()` | Подтверждает событие | Только текущий lease owner |
| `release_outbox()` | Retry или dead-letter | Порог по attempts |
| `claim_event_processing()` | Consumer dedupe lease | acquired/completed/busy |
| `NatsJetStreamSink.send()` | Публикует versioned event | Ждёт server ack, `Nats-Msg-Id=event.id` |
| `NatsPullWorker.run_once()` | Pull → decode → handler → ack/nak | Busy/error delivery не подтверждается |
| `migrate(dsn)` | Применяет forward-only SQL migrations | Advisory lock; повторный запуск безопасен |

## Composition/API

| Функция | Назначение |
|---|---|
| `build_in_memory_container()` | Собирает полностью рабочий local/test graph |
| `build_postgres_container(...)` | Собирает durable standalone server graph |
| `create_app(container=None)` | Создаёт FastAPI app; позволяет dependency injection |
| `GET /health` | Liveness, не readiness |
| API-key middleware | Защищает все non-health routes при `UAM_API_KEY` |
| `POST /v1/memory/retain` | REST boundary для retain |
| `POST /v1/ingest/text` | Детерминированный text ingestion |
| `POST /v1/ingest/document` | Base64 Markdown/PDF ingestion, лимит 20 MiB |
| `POST /v1/memory/recall` | Recall + context compilation |
| `POST /v1/workspaces/{id}/reflect` | Запуск baseline sleep/reflection |

## PostgreSQL adapter

| Функция | Назначение | Гарантия |
|---|---|---|
| `PostgresMemoryLedger.connect()` | Проверяет соединение и наличие schema | Не оставляет открытое соединение |
| `ensure_standalone_scope(...)` | Создаёт fixed server/project namespace | Идемпотентно |
| `retain(item, event, key)` | Записывает item, provenance, key и outbox | Одна транзакция; concurrent idempotency через advisory lock |
| `append(item, key)` | Импортирует memory без события | Append-only и tenant-bound |
| `get(tenant, item)` | Загружает memory с provenance | Устанавливает RLS tenant context |
| `list_for_workspace(...)` | Детализация workspace с layer filter | Детерминированный порядок |
| `search(query)` | PostgreSQL lexical fallback | Project/thread/label/time filters |
| `save(observation)` | Хранит reflection и evidence links | Evidence не меняется |

## Checkpoint domain — `domain/checkpoint.py`

| Тип | Назначение | Инвариант |
|---|---|---|
| `Checkpoint` | Frozen ревизионный snapshot | `revision >= 1`, immutable |
| `StaleRevisionError` | CAS conflict exception | Содержит `expected`, `actual` |

## CheckpointService — `services/checkpoint.py`

| Функция | Назначение | Гарантия |
|---|---|---|
| `save(tenant_id, workspace_id, thread_id, state)` | Auto-increment save | CAS-protected через store |
| `update(tenant_id, workspace_id, thread_id, state, expected_revision)` | CAS update | Raises `StaleRevisionError` |
| `restore(tenant_id, thread_id)` | Load latest checkpoint | None если нет |
| `restore_revision(tenant_id, thread_id, revision)` | Load specific revision | None если нет |
| `compact(tenant_id, thread_id, keep_last)` | Удаляет старые ревизии | Возвращает count удалённых |
| `list_for_workspace(tenant_id, workspace_id)` | Head checkpoints по workspace | Детерминированный порядок |

## CheckpointStore port — `ports/checkpoint_store.py`

| Метод | Назначение | Гарантия |
|---|---|---|
| `save(checkpoint)` | Unconditional append | Без CAS проверки |
| `save_if_head(checkpoint, expected_revision)` | CAS append | Raises `StaleRevisionError` |
| `get_head(tenant_id, thread_id)` | Latest revision | None если нет |
| `get_revision(tenant_id, thread_id, revision)` | Specific revision | None если нет |
| `list_for_workspace(tenant_id, workspace_id)` | Head per thread | Tenant-scoped |
| `compact(tenant_id, thread_id, keep_last)` | Удаление старых | Возвращает count |

## SDK — `sdk/python`, `sdk/typescript`

| Функция | Назначение | Гарантия |
|---|---|---|
| `MemoryClient.retain()` | Сохраняет memory | Один generated idempotency key на все retries |
| `MemoryClient.recall()` | Возвращает typed results и context | Standalone defaults остаются server-side |
| `MemoryClient.ingest_text()` / `ingestText()` | Загружает текст | Typed checksum и memory IDs |
| retry loop | Повторяет network/429/502/503/504 | Bounded exponential backoff, `Retry-After` |
| typed errors | Нормализует HTTP failures | Сохраняет status code |

## Qdrant adapter — `adapters/qdrant.py`

| Функция | Назначение | Гарантия |
|---|---|---|
| `QdrantCandidateSource.__init__(url, collection, dense_dim, api_key)` | Capture Qdrant endpoint and vector config | Нет I/O до `connect()` |
| `connect()` | Create QdrantClient, ensure collection with dense+sparse vectors | Идемпотентно; requires `qdrant-client` |
| `search(query)` | Hybrid search with project-scoped filtering | Tenant/workspace/layer/label filters |
| `upsert(item, dense_vector, sparse_indices?, sparse_values?)` | Insert or update point with full payload | Idempotent by item ID |
| `delete(item_id)` | Remove point by memory item ID | Нет ошибки если не существует |
| `reindex(items)` | Drop collection and re-insert from scratch | Блокирующий; batch по 100 |
| `_use_in_memory_backend()` | Activate test-only in-memory fallback | Нет зависимости на qdrant-client |
