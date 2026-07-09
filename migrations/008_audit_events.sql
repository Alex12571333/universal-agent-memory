create table audit_events (
  id uuid primary key,
  tenant_id uuid not null references tenants(id),
  workspace_id uuid references workspaces(id),
  action text not null check (length(btrim(action)) > 0),
  actor text not null check (length(btrim(actor)) > 0),
  actor_type text not null check (length(btrim(actor_type)) > 0),
  resource_type text not null check (length(btrim(resource_type)) > 0),
  resource_id text,
  status text not null default 'succeeded' check (
    status in ('succeeded','failed','denied')
  ),
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index audit_events_tenant_created_idx
  on audit_events (tenant_id, created_at desc);
create index audit_events_workspace_created_idx
  on audit_events (tenant_id, workspace_id, created_at desc)
  where workspace_id is not null;
create index audit_events_action_created_idx
  on audit_events (tenant_id, action, created_at desc);
create index audit_events_resource_created_idx
  on audit_events (tenant_id, resource_type, resource_id, created_at desc);

alter table audit_events enable row level security;
alter table audit_events force row level security;
create policy tenant_isolation on audit_events using
  (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
  with check
  (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

