# Index freshness

Recall always returns `index_stale`. It also returns `index_freshness`, a
workspace-scoped, durable delivery summary for recallable active memory heads:

- `active_memory_count` — active heads that are eligible for recall;
- `stale_memory_count` — heads whose `embed-v1` delivery is not complete;
- `unpublished_memory_count` — retained events still waiting in the outbox;
- `processing_memory_count` — published events not yet durably completed by
  the embedding consumer;
- `dead_letter_memory_count` — deliveries that exhausted their retry budget;
- `missing_delivery_memory_count` — active heads with no matching retained
  event, for example a legacy/imported record that requires scoped reindex.

The source of truth is PostgreSQL: `memory_items`, `outbox_events` and the
tenant-scoped `processed_events` row for consumer `embed-v1`. Qdrant is not
used to declare a record fresh. If delivery state cannot be read, recall fails
closed and reports stale rather than falsely claiming a current vector index.

Only current recallable heads are counted. Superseded, archived and rejected
records cannot make a workspace appear stale merely because their historical
embedding delivery is still queued.

`index_freshness` is diagnostic information. Lexical canonical recall remains
available while vectors are stale; operators should investigate pending,
dead-letter or missing delivery before relying on dense retrieval as complete.
