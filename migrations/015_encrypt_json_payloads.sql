-- Keep transcript lifecycle state queryable without leaving arbitrary metadata
-- plaintext once pgcrypto JSON protection is enabled. Existing deployments are
-- still plaintext while this migration executes; 015 then becomes the durable
-- source of the lifecycle marker for subsequent encrypted metadata writes.
alter table conversation_turns
  add column if not exists raw_content_state text not null default 'active'
  check (raw_content_state in ('active', 'purged_after_curation', 'purged_after_expiry'));

update conversation_turns
set raw_content_state = case
  when metadata #>> '{retention,raw_content}' = 'purged_after_curation'
    then 'purged_after_curation'
  when metadata #>> '{retention,raw_content}' = 'purged_after_expiry'
    then 'purged_after_expiry'
  else raw_content_state
end
where raw_content_state = 'active';

create index if not exists conversation_turns_expiry_active_idx
  on conversation_turns (tenant_id, workspace_id, expires_at)
  where expires_at is not null and raw_content_state = 'active';
