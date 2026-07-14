create table worker_heartbeats (
  tenant_id uuid not null references tenants(id),
  worker_kind text not null check (
    length(btrim(worker_kind)) between 1 and 64
  ),
  worker_id text not null check (
    length(btrim(worker_id)) between 1 and 128
  ),
  status text not null check (status in ('running', 'stopping')),
  started_at timestamptz not null,
  last_seen_at timestamptz not null default clock_timestamp(),
  primary key (tenant_id, worker_kind, worker_id)
);

create index worker_heartbeats_kind_seen_idx
  on worker_heartbeats (tenant_id, worker_kind, last_seen_at desc);

alter table worker_heartbeats enable row level security;
alter table worker_heartbeats force row level security;
create policy tenant_isolation on worker_heartbeats using
  (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
  with check
  (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
