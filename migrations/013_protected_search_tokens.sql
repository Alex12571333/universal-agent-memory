-- RFC 0013 staging schema. No reader/writer uses this table until the
-- protected-search feature is explicitly enabled and backfilled.
create table memory_search_tokens (
  tenant_id uuid not null references tenants(id) on delete cascade,
  workspace_id uuid not null,
  memory_item_id uuid not null references memory_items(id) on delete cascade,
  key_version smallint not null check (key_version > 0),
  digest bytea not null check (octet_length(digest) = 32),
  primary key (tenant_id, memory_item_id, key_version, digest),
  foreign key (workspace_id) references workspaces(id) on delete cascade
);

create index memory_search_tokens_lookup_idx
  on memory_search_tokens (tenant_id, workspace_id, key_version, digest);

alter table memory_search_tokens enable row level security;
create policy memory_search_tokens_tenant_isolation on memory_search_tokens
  using (tenant_id = current_setting('app.tenant_id', true)::uuid)
  with check (tenant_id = current_setting('app.tenant_id', true)::uuid);
