alter table conflict_reviews
  add column if not exists applied_memory_id uuid references memory_items(id);

create index if not exists conflict_reviews_applied_memory_idx
  on conflict_reviews (tenant_id, workspace_id, applied_memory_id)
  where applied_memory_id is not null;
