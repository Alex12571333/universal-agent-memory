# Протокол совместной работы агентов

Цель протокола — дать всем агентам актуальный ответ на три вопроса: кто что
делает, где лежат изменения и когда они попадут в `main`.

## Единый источник координации

- Один репозиторий: `Alex12571333/universal-agent-memory`.
- GitHub Issue описывает один независимо сливаемый work package.
- Assignee + label `status:in-progress` означают, что задача занята.
- Одна issue → одна ветка `agent/<issue>-<slug>` → один draft PR.
- PR обязан содержать `Closes #<issue>`.
- `main` меняется только через PR с зелёным CI.
- После CI используется squash auto-merge; ветка удаляется.

Не ведите ручную таблицу статусов в Git: она устаревает и конфликтует при
параллельных записях. Живое состояние находится в Issues и PR, а команда
`make agent-status` собирает его в одном экране.

## Начало работы

```bash
make agent-status
make agent-claim ISSUE=12 SLUG=qdrant-index
```

`agent-claim` прекращает работу, если issue уже назначена другому исполнителю.
После claim агент пишет в issue короткий план, затем работает только в созданной
ветке. Если требуется shared hotspot, план должен перечислить затрагиваемые
контракты.

## Публикация

```bash
make test lint
make agent-submit ISSUE=12
```

Команда push-ит ветку, создаёт или обновляет draft PR и включает auto-merge.
GitHub не сольёт PR, пока проверки не прошли. Конфликтующие PR остаются видимыми
и требуют rebase на свежий `origin/main`.

## Границы владения

| Track | Основная область | Shared boundary |
|---|---|---|
| domain | `domain/`, domain tests | `contracts/`, `ports/` |
| retention | `services/retention.py`, tests | `RetentionStore` |
| retrieval | `services/retrieval.py`, tests | `CandidateSource` |
| context | `services/context.py`, tests | context DTO |
| reflection | `services/reflection.py`, tests | observation port |
| postgres | `adapters/postgres.py`, `migrations/` | schema |
| qdrant | `adapters/qdrant.py` | retrieval contract |
| server | `api/`, Dockerfile, Compose | application services |
| workers | `workers/`, outbox delivery | event contracts |
| sdk | `sdk/`, examples | OpenAPI |

`contracts/*`, `ports/*`, `migrations/*`, `pyproject.toml` и Compose — shared
hotspots. Агент указывает их в issue до редактирования и проверяет открытые PR
через `make agent-status`.

## Правила реализации

1. Сервер self-hosted и single-deployment; не добавлять billing, customer
   onboarding, cloud control plane или SaaS quotas.
2. PostgreSQL — source of truth; индексы должны перестраиваться.
3. Domain/services не импортируют API и adapters.
4. Сначала test, затем реализация.
5. Миграции только вперёд; применённый migration не переписывать.
6. События версионируются (`memory.retained.v1`).
7. Каждая память несёт project boundary и provenance.
8. Публичная сигнатура требует обновления function catalog и changelog.

## Definition of done

- server запускается через Docker Compose или изменение не затрагивает runtime;
- unit/integration tests, Ruff и mypy зелёные;
- API не обращается к storage в обход services;
- project/thread boundaries покрыты тестом;
- retry идемпотентен;
- PR связан с issue и пригоден для squash merge.
