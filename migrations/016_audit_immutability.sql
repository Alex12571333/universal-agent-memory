-- Audit events are append-only. Deliberate retention pruning is the only
-- allowed deletion path and must opt in for the current transaction.
create or replace function protect_audit_events_immutable()
returns trigger
language plpgsql
as $$
begin
  if tg_op = 'DELETE'
     and current_setting('uam.audit_retention_mode', true) = 'on' then
    return old;
  end if;
  raise exception 'audit_events is append-only; use signed retention export before pruning'
    using errcode = '55000';
end;
$$;

drop trigger if exists audit_events_immutable on audit_events;
create trigger audit_events_immutable
before update or delete on audit_events
for each row execute function protect_audit_events_immutable();
