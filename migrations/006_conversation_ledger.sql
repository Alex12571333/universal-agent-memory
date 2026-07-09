create table conversation_turns (
  id uuid primary key,
  tenant_id uuid not null references tenants(id),
  workspace_id uuid not null references workspaces(id),
  thread_id uuid not null,
  agent_id uuid,
  namespace text not null default 'default',
  source_kind text not null,
  retention_policy text not null default 'raw_and_curated' check (
    retention_policy in ('raw_only','curated_only','raw_and_curated')
  ),
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  check (length(btrim(namespace)) > 0),
  check (length(btrim(source_kind)) > 0)
);

create table conversation_messages (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id),
  turn_id uuid not null references conversation_turns(id) on delete cascade,
  position int not null check (position >= 0),
  role text not null check (length(btrim(role)) > 0),
  name text,
  content text not null check (length(btrim(content)) > 0),
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  unique (turn_id, position)
);

create table conversation_idempotency_keys (
  tenant_id uuid not null references tenants(id),
  key text not null,
  turn_id uuid not null references conversation_turns(id),
  created_at timestamptz not null default now(),
  primary key (tenant_id, key)
);

create index conversation_turns_workspace_created_idx
  on conversation_turns (tenant_id, workspace_id, created_at desc);
create index conversation_turns_thread_created_idx
  on conversation_turns (tenant_id, workspace_id, thread_id, created_at desc);
create index conversation_turns_namespace_created_idx
  on conversation_turns (tenant_id, workspace_id, namespace, created_at desc);
create index conversation_messages_turn_position_idx
  on conversation_messages (tenant_id, turn_id, position);

alter table conversation_turns enable row level security;
alter table conversation_messages enable row level security;
alter table conversation_idempotency_keys enable row level security;

alter table conversation_turns force row level security;
alter table conversation_messages force row level security;
alter table conversation_idempotency_keys force row level security;

do $$
declare table_name text;
begin
  foreach table_name in array array[
    'conversation_turns','conversation_messages','conversation_idempotency_keys'
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
