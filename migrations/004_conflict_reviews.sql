create table conflict_reviews (
  tenant_id uuid not null references tenants(id),
  workspace_id uuid not null references workspaces(id),
  case_id uuid not null,
  status text not null check (
    status in ('unresolved','accepted','overridden','dismissed')
  ),
  winner_value text,
  reason text not null default '',
  updated_at timestamptz not null default now(),
  primary key (tenant_id, case_id)
);

create index conflict_reviews_workspace_idx
  on conflict_reviews (tenant_id, workspace_id, updated_at desc);

alter table conflict_reviews enable row level security;
alter table conflict_reviews force row level security;
create policy tenant_isolation on conflict_reviews using
  (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
  with check
  (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
