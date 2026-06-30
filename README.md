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
Docker volume `postgres_data`. Qdrant, NATS и MinIO пока экспериментальны и
включаются отдельно:

```bash
docker compose --profile advanced up -d
```

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

`server_id` и `project_id` имеют standalone defaults, поэтому клиенту не нужно
передавать SaaS tenant. Их можно заменить через `UAM_SERVER_ID` и
`UAM_PROJECT_ID`, если на одном сервере требуется несколько независимых
проектов.

OpenAPI доступен на `http://localhost:8080/docs`.

## Что уже работает

- append-only memory и provenance;
- working, core, episodic, semantic, procedural, social, reflection и error layers;
- PostgreSQL source of truth;
- атомарная запись memory + idempotency key + transactional outbox;
- lexical recall и budgeted context compiler;
- text ingestion и baseline reflection;
- изоляция проектов через PostgreSQL RLS;
- in-memory режим для unit-тестов;
- Docker image, Compose и CI.

Qdrant/vector recall, outbox relay, embeddings и SDK развиваются отдельными
work packages.

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
