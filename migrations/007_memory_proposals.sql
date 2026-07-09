create table memory_proposals (
  id uuid primary key,
  tenant_id uuid not null references tenants(id),
  workspace_id uuid not null references workspaces(id),
  agent_id uuid,
  thread_id uuid,
  namespace text not null default 'default',
  requester text not null,
  target text not null default 'auto' check (
    target in ('auto','fact','preference','decision','task','graph','procedure')
  ),
  proposal text not null check (length(btrim(proposal)) > 0),
  evidence text not null default '',
  confidence real not null default 0.7 check (confidence between 0 and 1),
  importance real not null default 0.5 check (importance between 0 and 1),
  status text not null default 'open' check (
    status in ('open','needs_review','accepted','rejected')
  ),
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  reviewed_at timestamptz,
  reviewer text,
  review_reason text not null default '',
  check (length(btrim(namespace)) > 0),
  check (length(btrim(requester)) > 0)
);

create table memory_proposal_idempotency_keys (
  tenant_id uuid not null references tenants(id),
  key text not null,
  proposal_id uuid not null references memory_proposals(id),
  created_at timestamptz not null default now(),
  primary key (tenant_id, key)
);

create index memory_proposals_workspace_created_idx
  on memory_proposals (tenant_id, workspace_id, created_at desc);
create index memory_proposals_namespace_status_idx
  on memory_proposals (tenant_id, workspace_id, namespace, status, created_at desc);
create index memory_proposals_agent_created_idx
  on memory_proposals (tenant_id, workspace_id, agent_id, created_at desc)
  where agent_id is not null;

alter table memory_proposals enable row level security;
alter table memory_proposal_idempotency_keys enable row level security;

alter table memory_proposals force row level security;
alter table memory_proposal_idempotency_keys force row level security;

do $$
declare table_name text;
begin
  foreach table_name in array array[
    'memory_proposals','memory_proposal_idempotency_keys'
  ]
  loop
    execute format(
      'create policy tenant_isolation on %I using
       (tenant_id = nullif(current_setting(''app.tenant_id'', true), '''')::uuid)
       with check
       (tenant_id = nullif(current_setting(''app.tenant_id'', true), '''')::uuid)',
      table_name
    );
  end loop;
end $$;
