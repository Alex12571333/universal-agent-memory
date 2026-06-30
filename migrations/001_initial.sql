create extension if not exists pgcrypto;
create extension if not exists pg_trgm;

create table tenants (
  id uuid primary key default gen_random_uuid(),
  slug text not null unique,
  created_at timestamptz not null default now()
);

create table workspaces (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id),
  name text not null,
  created_at timestamptz not null default now(),
  unique (tenant_id, name)
);

create table agents (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id),
  workspace_id uuid not null references workspaces(id),
  name text not null,
  role text not null,
  config jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create table threads (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id),
  workspace_id uuid not null references workspaces(id),
  owner_agent_id uuid references agents(id),
  status text not null default 'active',
  created_at timestamptz not null default now()
);

create table memory_items (
  id uuid primary key,
  tenant_id uuid not null references tenants(id),
  workspace_id uuid not null references workspaces(id),
  agent_id uuid references agents(id),
  thread_id uuid references threads(id),
  layer text not null check (
    layer in ('working','core','episodic','semantic','procedural','social','reflection','error')
  ),
  scope text not null check (
    scope in ('private','thread','team','workspace','organization')
  ),
  kind text not null,
  text text not null check (length(btrim(text)) > 0),
  labels text[] not null default '{}',
  metadata jsonb not null default '{}'::jsonb,
  importance real not null default 0.5 check (importance between 0 and 1),
  salience real not null default 0.5 check (salience between 0 and 1),
  confidence real not null default 0.7 check (confidence between 0 and 1),
  observed_at timestamptz not null,
  valid_from timestamptz,
  valid_to timestamptz,
  revision bigint not null default 1,
  supersedes_id uuid references memory_items(id),
  created_at timestamptz not null default now(),
  deleted_at timestamptz,
  check (valid_to is null or valid_from is null or valid_to > valid_from),
  check (scope <> 'thread' or thread_id is not null)
);

create table memory_provenance (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id),
  workspace_id uuid not null references workspaces(id),
  memory_item_id uuid not null references memory_items(id),
  source_kind text not null,
  origin_uri text,
  object_key text,
  checksum_sha256 text,
  quote_text text,
  extraction_version text not null,
  created_at timestamptz not null default now()
);

create table memory_edges (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id),
  workspace_id uuid not null references workspaces(id),
  src_id uuid not null references memory_items(id),
  dst_id uuid not null references memory_items(id),
  edge_type text not null,
  weight real not null default 1.0,
  valid_from timestamptz,
  valid_to timestamptz,
  provenance_item_id uuid references memory_items(id),
  created_at timestamptz not null default now()
);

create table observations (
  id uuid primary key,
  tenant_id uuid not null references tenants(id),
  workspace_id uuid not null references workspaces(id),
  summary text not null,
  confidence real not null check (confidence between 0 and 1),
  stale boolean not null default false,
  created_at timestamptz not null default now()
);

create table observation_evidence (
  tenant_id uuid not null references tenants(id),
  observation_id uuid not null references observations(id),
  memory_item_id uuid not null references memory_items(id),
  primary key (observation_id, memory_item_id)
);

create table idempotency_keys (
  tenant_id uuid not null references tenants(id),
  key text not null,
  memory_item_id uuid not null references memory_items(id),
  created_at timestamptz not null default now(),
  primary key (tenant_id, key)
);

create table outbox_events (
  id uuid primary key,
  tenant_id uuid not null references tenants(id),
  workspace_id uuid not null references workspaces(id),
  name text not null,
  payload jsonb not null,
  correlation_id uuid,
  occurred_at timestamptz not null,
  published_at timestamptz,
  attempts int not null default 0
);

create table checkpoints (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id),
  workspace_id uuid not null references workspaces(id),
  thread_id uuid not null references threads(id),
  revision bigint not null,
  state jsonb not null,
  created_at timestamptz not null default now(),
  unique (thread_id, revision)
);

create index memory_items_workspace_layer_created_idx
  on memory_items (tenant_id, workspace_id, layer, created_at desc);
create index memory_items_thread_idx
  on memory_items (tenant_id, thread_id, created_at desc) where thread_id is not null;
create index memory_items_labels_idx on memory_items using gin (labels);
create index memory_items_text_trgm_idx on memory_items using gin (text gin_trgm_ops);
create index memory_items_text_fts_idx
  on memory_items using gin (to_tsvector('simple', text));
create index memory_edges_src_idx on memory_edges (tenant_id, workspace_id, src_id);
create index memory_edges_dst_idx on memory_edges (tenant_id, workspace_id, dst_id);
create index outbox_unpublished_idx
  on outbox_events (occurred_at) where published_at is null;

alter table workspaces enable row level security;
alter table agents enable row level security;
alter table threads enable row level security;
alter table memory_items enable row level security;
alter table memory_provenance enable row level security;
alter table memory_edges enable row level security;
alter table observations enable row level security;
alter table observation_evidence enable row level security;
alter table idempotency_keys enable row level security;
alter table outbox_events enable row level security;
alter table checkpoints enable row level security;

do $$
declare table_name text;
begin
  foreach table_name in array array[
    'workspaces','agents','threads','memory_items','memory_provenance','memory_edges',
    'observations','observation_evidence','idempotency_keys','outbox_events','checkpoints'
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
