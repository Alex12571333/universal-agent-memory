# Каталог публичных функций

Это карта ответственности, побочных эффектов и контрактов. Приватные helpers с
префиксом `_` документируются docstring рядом с кодом.

## Domain — `domain/models.py`

| Функция | Назначение | Побочные эффекты / ошибки |
|---|---|---|
| `MemoryItem.__post_init__()` | Проверяет текст, score ranges, validity и thread scope | `ValueError` при нарушении инварианта |
| `MemoryItem.is_valid_at(moment)` | Point-in-time проверка temporal validity | Нет |
| `MemoryItem.supersede(text, confidence=...)` | Создаёт новый immutable revision с `supersedes_id` | Генерирует UUID/time; старый item не меняет |
| `MemoryRevisionConflictError` | Ошибка stale CAS для MemoryItem | Содержит `expected`, `actual` |
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
| `RetentionService.supersede(command)` | `SupersedeMemoryCommand` → `RetainResult` | CAS append; stale revision → `MemoryRevisionConflictError` |

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
| `ReflectionService.reflect(tenant, workspace)` | scope → observations | Raw evidence не меняется; repeated/conflicting slots → observations |
| `_extract_slot(text)` | Text → subject/predicate/value | Deterministic fixtures для `X is Y`, `A owns B`, `X releases on D` |
| `_confidence(rows, conflict=...)` | Evidence → score | Повторы усиливают, конфликтующие значения штрафуются |

## In-memory adapter — `adapters/in_memory.py`

| Функция | Назначение |
|---|---|
| `InMemoryMemoryStore.append()` | Thread-safe append и idempotency |
| `supersede_if_current()` | Thread-safe CAS supersede и outbox event |
| `get()` | Tenant-safe lookup |
| `list_for_workspace()` | Канонический fallback/listing |
| `search()` | Dependency-free lexical retrieval + metadata filters |
| `publish()` | In-memory idempotent outbox |
| `collect_metrics()` | Local counters for tests/dev `/metrics` |
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

## Metrics/ops — `services/metrics.py`, `scripts/backup.py`, `scripts/restore.py`

| Функция | Назначение | Семантика |
|---|---|---|
| `render_prometheus(metrics)` | Numeric mapping → Prometheus text | Stable sort, `uam_` prefix |
| `backup.py` | Запускает `pg_dump --format=custom` | URL из `UAM_BACKUP_DATABASE_URL`/admin/database env |
| `restore.py` | Запускает `pg_restore` | Non-destructive by default; `--clean` opt-in |

## Vault — `services/vault.py`, `scripts/export_vault.py`

| Функция | Назначение | Семантика |
|---|---|---|
| `VaultExporter.export()` | Workspace → in-memory Markdown vault snapshot | Stable file names; deterministic file ordering |
| `VaultExporter.export_workspace()` | Workspace → folder on disk | Safe relative paths; memory/observation counts |
| `VaultExporter._memory_file()` | `MemoryItem` → Obsidian note | Frontmatter + provenance + supersede backlinks |
| `VaultExporter._observation_file()` | `Observation` → reflection note | Evidence backlinks to `mem-*` notes |
| `export_vault.py` | PostgreSQL workspace → folder | One-way export; no destructive import |

## Native integrations — `agent-integrations/`

| Файл | Назначение | Семантика |
|---|---|---|
| `shared/lifecycle.py` | Runtime-agnostic plugin contract | before-run, after-event, run-complete hooks |
| `openclaw/README.md` | OpenClaw native adapter plan | Plugin-level integration, not skill/MCP-only |
| `hermes/README.md` | Hermes native adapter plan | Same shared lifecycle contract |

## Composition/API

| Функция | Назначение |
|---|---|
| `build_in_memory_container()` | Собирает полностью рабочий local/test graph |
| `build_postgres_container(...)` | Собирает durable standalone server graph |
| `create_app(container=None)` | Создаёт FastAPI app; позволяет dependency injection |
| `GET /health` | Liveness, не readiness |
| API-key middleware | Защищает все non-health routes при `UAM_API_KEY` |
| `GET /metrics` | Prometheus counters/lag; защищён API key |
| `POST /v1/memory/retain` | REST boundary для retain |
| `PUT /v1/memory/{id}/supersede` | CAS replacement; stale revision → `409 revision_conflict` |
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
| `supersede_if_current(item, event, expected_revision, key)` | CAS-запись новой ревизии | `FOR UPDATE` parent + recursive head check; одна outbox-транзакция |
| `append(item, key)` | Импортирует memory без события | Append-only и tenant-bound |
| `get(tenant, item)` | Загружает memory с provenance | Устанавливает RLS tenant context |
| `list_for_workspace(...)` | Детализация workspace с layer filter | Детерминированный порядок |
| `search(query)` | PostgreSQL lexical fallback | Project/thread/label/time filters |
| `save(observation)` | Хранит reflection и evidence links | Evidence не меняется |
| `collect_metrics(tenant)` | Считает counters и outbox lag | Устанавливает RLS tenant context |

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

## Embedding ports — `ports/embeddings.py`

| Функция / Свойство | Назначение | Гарантия |
|---|---|---|
| `EmbeddingClient.model_name` | Название и версия модели | Уникальный строковый ID |
| `EmbeddingClient.dimension` | Размерность выходного вектора | Фиксированный `int` |
| `EmbeddingClient.embed(text)` | Генерация dense вектора | Возвращает `list[float]` |

## Embedding adapters — `adapters/embeddings.py`

| Класс / Функция | Назначение | Гарантия |
|---|---|---|
| `FakeEmbeddingClient` | Генерация детерминированных мок-векторов | Векторы воспроизводимы по MD5 от текста |

## Embedding service — `services/embedding.py`

| Функция | Назначение | Гарантия |
|---|---|---|
| `process_memory_retained(tenant, id)` | Асинхронная обработка и индексация памяти | Загружает память и делает upsert в Qdrant |
| `reindex_all(tenant, workspace)` | Полная переиндексация воркспейса | Удаляет и заново заливает коллекцию в Qdrant |
