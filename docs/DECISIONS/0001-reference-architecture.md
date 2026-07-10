# ADR-0001: SQL ledger + rebuildable indexes

Статус: accepted.

## Решение

Использовать PostgreSQL как транзакционный ledger, Qdrant как hybrid retrieval
index, MinIO/S3 как object store и NATS JetStream как delivery layer. Граф —
опциональный adapter после подтверждённой потребности в multi-hop/temporal
traversal.

## Почему

PostgreSQL provides MVCC, transactional outbox, RLS, auditability and recovery.
Vector and graph engines provide specialized recall, but remain rebuildable
indexes rather than authorities for canonical memory.

## Последствия

Плюсы: компоненты заменяемы, индекс можно перестроить, миграция в managed
сервисы проще. Цена: eventual consistency и необходимость outbox/lag monitoring.
