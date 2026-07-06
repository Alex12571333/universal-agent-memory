# Universal Agent Memory Server

Self-hosted сервер общей памяти для AI-агентов. Это не SaaS: один Docker
deployment принадлежит пользователю или команде, хранит память локально и даёт
агентам простой HTTP API в стиле Mem0.

## Запуск

```bash
docker compose up -d --build
curl http://localhost:8080/health
```

По умолчанию запускаются только `memory-server` и PostgreSQL. Данные остаются в
Docker volume `postgres_data`. Advanced-профиль добавляет NATS JetStream,
transactional-outbox relay и экспериментальные Qdrant/MinIO:

```bash
docker compose --profile advanced up -d
```

Перед API и relay автоматически запускается forward-only migration service.
Повторный `docker compose up` сохраняет volume и применяет только новые SQL
migrations.

## API за минуту

Сохранить память:

```bash
curl -X POST http://localhost:8080/v1/memory/retain \
  -H 'Content-Type: application/json' \
  -d '{
    "layer": "semantic",
    "scope": "workspace",
    "kind": "fact",
    "text": "Основной язык проекта — Python",
    "agent_id": "11111111-1111-1111-1111-111111111111",
    "idempotency_key": "example-1"
  }'
```

Найти и собрать контекст:

```bash
curl -X POST http://localhost:8080/v1/memory/recall \
  -H 'Content-Type: application/json' \
  -d '{"query":"Какой язык используется в проекте?"}'
```

Markdown и text-bearing PDF принимаются через `POST /v1/ingest/document` как
base64 с полями `format` и `origin_uri`. Для PDF provenance сохраняет номер
страницы в URI, а checksum относится к исходному бинарному файлу.

`server_id` и `project_id` имеют standalone defaults, поэтому клиенту не нужно
передавать SaaS tenant. Их можно заменить через `UAM_SERVER_ID` и
`UAM_PROJECT_ID`, если на одном сервере требуется несколько независимых
проектов.

OpenAPI доступен на `http://localhost:8080/docs`.

## Доступ по API key

Без `UAM_API_KEY` сервер работает в удобном локальном режиме без авторизации.
Перед публикацией порта в LAN задайте ключ:

```bash
UAM_API_KEY='replace-with-a-long-random-secret' docker compose up -d
curl -H 'Authorization: Bearer replace-with-a-long-random-secret' \
  http://localhost:8080/v1/memory/recall \
  -H 'Content-Type: application/json' \
  -d '{"query":"project context"}'
```

`/health` остаётся публичным для Docker probes. Остальные маршруты, включая
OpenAPI/docs, требуют bearer key. Не публикуйте порт `8080` в интернет без TLS
reverse proxy.

## Метрики и backup

`/metrics` отдаёт Prometheus text format и показывает базовые counters/lag:
memory items, observations, checkpoints, pending/dead-letter outbox events и
consumer leases. Если задан `UAM_API_KEY`, маршрут тоже требует bearer key.

```bash
curl -H 'Authorization: Bearer replace-with-a-long-random-secret' \
  http://localhost:8080/metrics
```

PostgreSQL backup/restore делается через ops-профиль:

```bash
docker compose --profile ops run --rm backup
docker compose --profile ops run --rm restore
```

По умолчанию backup пишет `./backups/uam.dump` в custom `pg_dump` format.
Restore не делает destructive clean, если явно не запустить `scripts/restore.py`
с `--clean`.

## SDK

- [Python client](sdk/python/README.md)
- [TypeScript client](sdk/typescript/README.md)

Оба клиента типизируют retain/recall/ingest, сохраняют один idempotency key при
повторе запроса и преобразуют HTTP failures в специализированные ошибки.

## Что уже работает

- append-only memory и provenance;
- working, core, episodic, semantic, procedural, social, reflection и error layers;
- PostgreSQL source of truth;
- атомарная запись memory + idempotency key + transactional outbox;
- PostgreSQL leases, retries, dead-letter и доставка outbox в NATS JetStream;
- durable consumer deduplication по `(event_id, consumer)`;
- lexical recall и budgeted context compiler;
- text ingestion и baseline reflection;
- Markdown/PDF ingestion с checksum, page provenance и idempotent retry;
- Prometheus-style `/metrics` и Docker-friendly PostgreSQL backup/restore;
- изоляция проектов через PostgreSQL RLS;
- in-memory режим для unit-тестов;
- Docker image, Compose и CI.

Qdrant/vector recall, embeddings и SDK развиваются отдельными work packages.

## Совместная работа агентов

GitHub Issues — живая доска задач, Pull Requests — очередь интеграции, `main` —
единственная общая линия истории. Агент обязан занять issue до изменений:

```bash
make agent-status
make agent-claim ISSUE=12 SLUG=qdrant-index
# работа, тесты, commits
make agent-submit ISSUE=12
```

После зелёного CI PR получает auto-merge. Так каждый агент сразу видит assignee,
ветку и PR других агентов, а готовые изменения автоматически сходятся в этот
репозиторий. Полный протокол находится в [AGENTS.md](AGENTS.md).

## Локальная разработка

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,api,postgres]"
pytest
ruff check .
mypy src
```

Начните с [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [AGENTS.md](AGENTS.md) и
[docs/WORK_PACKAGES.md](docs/WORK_PACKAGES.md).
