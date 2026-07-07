alter table memory_items
  add column status text not null default 'active' check (
    status in (
      'active','stale','deprecated','disputed','hypothesis','rejected','archived','pinned'
    )
  );

create index memory_items_status_idx
  on memory_items (tenant_id, workspace_id, status, created_at desc);
