create table api_key_registry (
  id uuid primary key,
  tenant_id uuid not null references tenants(id),
  name text not null check (length(btrim(name)) > 0),
  secret_fingerprint text not null check (length(btrim(secret_fingerprint)) > 0),
  scopes text[] not null check (array_length(scopes, 1) > 0),
  created_at timestamptz not null default now(),
  last_used_at timestamptz,
  revoked_at timestamptz,
  revoked_reason text not null default '',
  unique (tenant_id, secret_fingerprint)
);

create index api_key_registry_tenant_name_idx
  on api_key_registry (tenant_id, name);
create index api_key_registry_last_used_idx
  on api_key_registry (tenant_id, last_used_at desc)
  where last_used_at is not null;
create index api_key_registry_revoked_idx
  on api_key_registry (tenant_id, revoked_at desc)
  where revoked_at is not null;

alter table api_key_registry enable row level security;
alter table api_key_registry force row level security;
create policy tenant_isolation on api_key_registry using
  (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
  with check
  (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

