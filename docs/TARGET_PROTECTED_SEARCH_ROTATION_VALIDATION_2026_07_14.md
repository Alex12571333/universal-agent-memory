# Target protected-search rotation validation — 2026-07-14

## Scope

This document records an isolated key-rotation proof for the optional
`hmac-v1` protected lexical index. It ran from repository `main` on host
`192.168.0.14` using a disposable PostgreSQL 17 Compose project.

The test keys, tenant, workspace and memory row existed only in that disposable
database. No production key, canonical Obelisk database, OpenClaw state or
Hermes state was read or changed.

## Sequence

1. Apply all migrations to a fresh PostgreSQL database.
2. Create one workspace-scoped canonical memory row.
3. Backfill key version 1 with the restricted `memory_app` role.
4. Switch configuration to a distinct key version 2 and backfill again.
5. Verify both digest versions coexist for the same canonical row.
6. Run `protected_search_index_probe.py` for version 2; require complete
   coverage and use of `memory_search_tokens_lookup_idx`.
7. Run `retire_protected_search_key.py --apply` with the administrator role.
   The command independently rechecks active-version coverage before deleting
   version 1.
8. Verify version 1 has zero rows and version 2 still has its four HMAC entries.
9. Remove the temporary container, network and volume.

## Result

```text
v1_complete=true rows=1 key=1
v2_complete=true rows=1 key=2
dual_version_counts=1=4,2=4
active_v2_probe=PASS
retired_v1_count=0 active_v2_count=4
temporary_stack_removed=true
```

The four entries per version are three normalized query-token digests plus the
per-document coverage marker. Reports and console output contained counts,
versions and index metadata only; they did not contain HMAC values or canonical
memory text.

## Remaining deployment gate

This proves the rotation tools and database invariants on a target host. A
long-running appliance must still coordinate deployment so every writer uses
the new key version before retirement, preserve its backfill/plan/retirement
reports, and keep old key material until rollback approval. The reader remains
fail-safe during partial coverage by falling back to authorized canonical
scanning.
