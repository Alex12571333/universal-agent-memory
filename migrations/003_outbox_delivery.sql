alter table outbox_events
  add column lease_owner text,
  add column lease_until timestamptz,
  add column last_error text,
  add column dead_lettered_at timestamptz;

drop index outbox_unpublished_idx;
create index outbox_due_idx
  on outbox_events (tenant_id, occurred_at)
  where published_at is null and dead_lettered_at is null;

create table processed_events (
  tenant_id uuid not null references tenants(id),
  event_id uuid not null,
  consumer text not null,
  lease_owner text,
  lease_until timestamptz,
  attempts int not null default 0,
  processed_at timestamptz,
  last_error text,
  primary key (tenant_id, event_id, consumer)
);

alter table processed_events enable row level security;
alter table processed_events force row level security;
create policy tenant_isolation on processed_events using
  (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
  with check
  (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
