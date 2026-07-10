alter table conversation_turns
  add column if not exists expires_at timestamptz;

create index if not exists conversation_turns_expiry_idx
  on conversation_turns (tenant_id, workspace_id, expires_at)
  where expires_at is not null;
