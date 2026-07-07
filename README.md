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
transactional-outbox relay, embedding worker, Qdrant и MinIO:

```bash
UAM_QDRANT_URL=http://qdrant:6333 docker compose --profile advanced up -d
```

Перед API и relay автоматически запускается forward-only migration service.
Повторный `docker compose up` сохраняет volume и применяет только новые SQL
migrations. Подробный продовый smoke/E2E-план: [docs/PRODUCTION_READINESS_TESTING.md](docs/PRODUCTION_READINESS_TESTING.md).

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

Локальная operator UI доступна на `http://localhost:8080/ui`. Она умеет
просматривать память, запускать recall, смотреть conflict inbox и запускать
reflect/reindex. Если задан `UAM_API_KEY`, UI защищена тем же bearer-key
middleware, что и API.

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

## Secrets/PII guard

Перед сохранением память проходит deterministic privacy guard. По умолчанию
секреты и high-risk PII редактируются, а в `metadata.privacy` сохраняется audit
trail без сырого секрета.

```bash
UAM_PRIVACY_ENABLED=true
UAM_PRIVACY_ACTION=redact  # redact|reject|metadata_only|allow
```

Детекторы покрывают common API keys/tokens, private keys, password assignments,
AWS-style access keys, SSN и payment-card-like значения с Luhn-проверкой.

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

## Obsidian/vault export

Чтобы человек мог читать память как knowledge vault, ops-профиль умеет
экспортировать workspace в Markdown-файлы с frontmatter и Obsidian backlinks:

```bash
docker compose --profile ops run --rm vault-export
```

По умолчанию экспорт пишется в `./vault`:

```text
vault/
  README.md
  semantic/mem-<uuid>.md
  core/mem-<uuid>.md
  reflections/obs-<uuid>.md
```

Vault можно открыть в Obsidian через **Open folder as vault**. Изменения лучше
сначала проверять через dry-run import: сервер сравнит `mem-*` note с
канонической памятью и применит изменение только через CAS `supersede`, без
destructive overwrite. Подробности: [docs/VAULT.md](docs/VAULT.md).

```bash
# dry-run: показать, какие notes создадут новую ревизию
docker compose --profile ops run --rm vault-import

# apply: реально создать новые ревизии через supersede
docker compose --profile ops run --rm vault-import python scripts/import_vault.py /vault --apply
```

## Native agent integrations

`agent-integrations/` содержит следующий слой интеграции для OpenClaw, Hermes и
похожих runtimes. Это не skill и не MCP-first подход: цель — plugin/runtime
hooks, которые подключают память до, во время и после agent run:

- before run: recall core/working/task context;
- before model call: inject compact context package;
- after tool call/message: retain observations, traces and errors;
- checkpoint: save working state;
- run complete: retain summary and trigger reflection.

MCP можно оставить как совместимость, но для агентов с plugin API основная
интеграция должна быть native.

Сейчас есть installable adapters:

- OpenClaw: `agent-integrations/openclaw/plugin` — ESM plugin с
  `agent_turn_prepare`, `after_tool_call`, `agent_end`;
- Hermes: `agent-integrations/hermes/universal_agent_memory` — native
  `MemoryProvider`, подключаемый как `memory.provider: universal_agent_memory`.

Оба читают `UAM_URL`, `UAM_API_KEY`, `UAM_TENANT_ID`, `UAM_WORKSPACE_ID`,
`UAM_AGENT_ID` и могут работать без SaaS onboarding, deriving stable local UUIDs
when IDs are omitted.

## Embedding providers

По умолчанию сервер использует deterministic `fake` embeddings, чтобы локальный
Docker запускался без внешних ключей. Для production indexing можно выбрать
реальный provider через env:

```bash
UAM_EMBEDDING_PROVIDER=openai   # fake|openai|ollama|tei
UAM_EMBEDDING_MODEL=text-embedding-3-small
UAM_EMBEDDING_DIM=1536
UAM_EMBEDDING_BASE_URL=https://api.openai.com/v1
UAM_EMBEDDING_API_KEY=...
```

Поддерживаются:

- `openai` — `/v1/embeddings`, bearer API key, `dimensions`;
- `ollama` — локальный `/api/embeddings`;
- `tei` — OpenAI-compatible `/v1/embeddings` для TEI/vLLM-style endpoints;
- `fake` — deterministic vectors for tests/local CI.

Перед записью в Qdrant сервер проверяет, что длина embedding совпадает с
`UAM_EMBEDDING_DIM`; mismatch прерывает indexing до upsert.

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
- OpenAI/Ollama/TEI/fake embedding providers with dimension validation;
- text ingestion и baseline reflection;
- Markdown/PDF ingestion с checksum, page provenance и idempotent retry;
- Prometheus-style `/metrics` и Docker-friendly PostgreSQL backup/restore;
- Obsidian-compatible Markdown vault export/import;
- deterministic conflict review inbox with persisted human decisions;
- local operator UI at `/ui`;
- secrets/PII guard with redaction audit metadata;
- memory lifecycle statuses with recall exclusion/demotion policy;
- typed memory graph edges and neighbor API;
- изоляция проектов через PostgreSQL RLS;
- in-memory режим для unit-тестов;
- Docker image, Compose и CI.

Дальше по roadmap: live install smoke tests for OpenClaw/Hermes, scheduled
working-memory expiration, graph edges и UI edit workflows.

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
pip install -e ".[dev,api,postgres,qdrant,nats]"
pytest
ruff check .
mypy src
```

Начните с [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [AGENTS.md](AGENTS.md) и
[docs/WORK_PACKAGES.md](docs/WORK_PACKAGES.md).

Для проверки реального Docker-стека и внешнего embedding endpoint используйте
[docs/PRODUCTION_READINESS_TESTING.md](docs/PRODUCTION_READINESS_TESTING.md).
