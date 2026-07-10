alter table outbox_events
  add column if not exists available_at timestamptz not null default now();

create index if not exists outbox_due_retry_idx
  on outbox_events (tenant_id, available_at, occurred_at)
  where published_at is null and dead_lettered_at is null;
