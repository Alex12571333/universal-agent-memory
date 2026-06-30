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

## Composition/API

| Функция | Назначение |
|---|---|
| `build_in_memory_container()` | Собирает полностью рабочий local/test graph |
| `create_app(container=None)` | Создаёт FastAPI app; позволяет dependency injection |
| `GET /health` | Liveness, не readiness |
| `POST /v1/memory/retain` | REST boundary для retain |
| `POST /v1/ingest/text` | Детерминированный text ingestion |
| `POST /v1/memory/recall` | Recall + context compilation |
| `POST /v1/workspaces/{id}/reflect` | Запуск baseline sleep/reflection |

## PostgreSQL adapter

| Функция | Назначение | Гарантия |
|---|---|---|
| `PostgresMemoryLedger.connect()` | Проверяет соединение и наличие schema | Не оставляет открытое соединение |
| `retain(item, event, key)` | Записывает item, provenance, key и outbox | Одна транзакция; concurrent idempotency через advisory lock |
| `append(item, key)` | Импортирует memory без события | Append-only и tenant-bound |
| `get(tenant, item)` | Загружает memory с provenance | Устанавливает RLS tenant context |
| `list_for_workspace(...)` | Детализация workspace с layer filter | Детерминированный порядок |

`QdrantCandidateSource.connect()` пока остаётся явной seam-точкой следующего
work package.
