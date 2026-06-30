-- Development-only login. Production deployments should provision credentials
-- through their secret manager and apply equivalent least-privilege grants.
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'memory_app') then
    create role memory_app login password 'memory' nosuperuser nocreatedb nocreaterole;
  end if;
end
$$;

grant usage on schema public to memory_app;
grant select, insert, update, delete on all tables in schema public to memory_app;
grant usage, select on all sequences in schema public to memory_app;

alter default privileges in schema public
  grant select, insert, update, delete on tables to memory_app;
alter default privileges in schema public
  grant usage, select on sequences to memory_app;
