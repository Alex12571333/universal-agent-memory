# Changelog

## Unreleased

- Implemented PostgreSQL canonical memory, provenance, idempotency and transactional outbox.
- Forced tenant RLS even for table owners and added PostgreSQL integration coverage.
- Replaced the split ledger/event retention boundary with one atomic port.

## 0.1.0

- Создан модульный memory-plane foundation.
- Добавлены модели восьми слоёв памяти и scope-модель.
- Реализованы retain, recall, context compile и reflection.
- Добавлены in-memory adapters, REST API, SQL/RLS и тесты.
