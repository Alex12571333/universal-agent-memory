# Universal Agent Memory

Основа универсального memory plane для нескольких AI-агентов. Проект собран по
двум исследованиям из задания и сочетает:

- append-only каноническую память и provenance;
- working, core, episodic, semantic, procedural, social, reflection и error слои;
- hybrid retrieval с независимыми источниками кандидатов;
- компиляцию контекста под бюджет и тип операции агента;
- outbox и фоновые `embed`, `dedupe`, `graph`, `reflect`, `summarize` задачи;
- tenant/workspace/agent/thread scopes;
- сменные хранилища через ports/adapters.

## Быстрый старт

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,api]"
pytest
uvicorn memory_plane.api.app:create_app --factory --reload
```

Без внешних сервисов ядро и тесты работают через in-memory адаптер:

```bash
python examples/basic_flow.py
```

Инфраструктура для production-профиля:

```bash
docker compose up -d
```

## Где что лежит

| Папка | Ответственность | Можно разрабатывать отдельно |
|---|---|---|
| `src/memory_plane/domain` | Модели и инварианты памяти | Да, без I/O |
| `src/memory_plane/contracts` | DTO и события между модулями | Да; изменения требуют contract review |
| `src/memory_plane/ports` | Интерфейсы репозиториев и индексов | Да |
| `src/memory_plane/services` | Retain, recall, context, reflection | Да, через fake ports |
| `src/memory_plane/adapters` | Postgres/Qdrant/S3/NATS и in-memory реализации | Каждый адаптер отдельно |
| `src/memory_plane/api` | REST boundary | Да, только через services |
| `src/memory_plane/workers` | Обработчики фоновых событий | Каждый handler отдельно |
| `migrations` | SQL schema, RLS, индексы | Отдельный владелец |
| `docs` | Архитектура, контракты, каталог функций | — |

Начните с [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), затем прочитайте
[AGENTS.md](AGENTS.md) и [docs/FUNCTION_CATALOG.md](docs/FUNCTION_CATALOG.md).

## Статус

Это foundation, а не законченный SaaS. Уже реализованы исполняемые domain-модели,
retain/recall/context/reflection, REST endpoints и unit tests. In-memory профиль
подходит для локальной разработки. PostgreSQL-профиль уже умеет атомарно сохранять
memory item, provenance, idempotency key и outbox event с tenant RLS. Также работает
детерминированный text ingestion с SHA-256 provenance и идемпотентными chunks.
Qdrant, NATS и S3 production-адаптеры остаются независимыми work packages.

PostgreSQL integration tests запускаются отдельно:

```bash
UAM_TEST_DATABASE_URL=postgresql://memory_app:memory@localhost:5432/memory \
  pytest tests/integration
```

В Compose миграции выполняет `memory_admin`, а приложение подключается как
`memory_app` из `.env.example`. Это разделение обязательно: PostgreSQL superuser
обходит RLS.
