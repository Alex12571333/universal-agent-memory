# ADR-0001: SQL ledger + rebuildable indexes

Статус: accepted.

## Решение

Использовать PostgreSQL как транзакционный ledger, Qdrant как hybrid retrieval
index, MinIO/S3 как object store и NATS JetStream как delivery layer. Граф —
опциональный adapter после подтверждённой потребности в multi-hop/temporal
traversal.

## Почему

Оба исследования независимо рекомендуют этот разрез: SQL лучше решает MVCC,
RLS, ACL, аудит и recovery; vector/graph движки лучше решают специализированный
recall, но не должны быть authority.

## Последствия

Плюсы: компоненты заменяемы, индекс можно перестроить, миграция в managed
сервисы проще. Цена: eventual consistency и необходимость outbox/lag monitoring.
