-- A token row must inherit its scope from the canonical item it indexes.  The
-- separate foreign keys in 013 guarantee existence, while this trigger closes
-- the cross-tenant/workspace mismatch hole without adding mutable application
-- privileges to canonical memory rows.
create or replace function memory_search_tokens_match_item_scope()
returns trigger
language plpgsql
as $$
declare
  item_tenant_id uuid;
  item_workspace_id uuid;
begin
  select tenant_id, workspace_id
    into item_tenant_id, item_workspace_id
    from memory_items
   where id = new.memory_item_id;

  if item_tenant_id is null then
    raise exception 'indexed memory item does not exist'
      using errcode = 'foreign_key_violation';
  end if;
  if new.tenant_id <> item_tenant_id or new.workspace_id <> item_workspace_id then
    raise exception 'protected-search token scope must match memory item scope'
      using errcode = 'integrity_constraint_violation';
  end if;
  return new;
end;
$$;

create trigger memory_search_tokens_scope_integrity
before insert or update on memory_search_tokens
for each row execute function memory_search_tokens_match_item_scope();
