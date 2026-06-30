# Правила параллельной работы агентов

## Границы владения

Один агент за один change-set меняет преимущественно одну область:

| Track | Разрешённая область | Стабильная граница |
|---|---|---|
| A — domain | `domain/`, domain tests | `contracts/` и `ports/` только через RFC |
| B — retention | `services/retention.py`, tests | `MemoryLedger`, `EventPublisher` |
| C — retrieval | `services/retrieval.py`, tests | `CandidateSource` |
| D — context | `services/context.py`, tests | `ContextRecipe`, `ContextPackage` |
| E — reflection | `services/reflection.py`, worker, tests | `MemoryLedger` |
| F — Postgres | `adapters/postgres.py`, `migrations/` | `MemoryLedger` |
| G — Qdrant | `adapters/qdrant.py` | `CandidateSource` |
| H — API/SDK | `api/`, `sdk/` | application service signatures |
| I — platform | Compose/K8s/observability | health/readiness contracts |

## Независимый workflow

1. Выберите свободный track в `docs/WORK_PACKAGES.md`.
2. Не импортируйте адаптеры из domain/services. Зависимости направлены внутрь:
   `api/workers/adapters -> services -> ports/contracts/domain`.
3. Сначала добавьте contract test или unit test.
4. Публичную сигнатуру меняйте только вместе с:
   `contracts`, `docs/FUNCTION_CATALOG.md`, contract tests и записью в changelog.
5. Миграции только вперёд; существующие migration-файлы не переписывать.
6. События версионируются (`memory.retained.v1`), старые consumers не ломаются.
7. Каждая запись обязана нести `tenant_id`, `workspace_id` и provenance.

## Definition of done

- модуль тестируется без production-инфраструктуры;
- tenant boundary проверена тестом;
- повторная доставка события идемпотентна;
- публичные функции имеют docstring и запись в каталоге;
- нет прямого доступа API к БД/индексу в обход service layer.

## Зоны координации

Файлы `contracts/*`, `ports/*`, `migrations/*`, `pyproject.toml` считаются
shared hotspots. Перед их изменением агент публикует короткий RFC в
`docs/rfcs/NNNN-title.md`: проблема, новая сигнатура, совместимость, миграция.
