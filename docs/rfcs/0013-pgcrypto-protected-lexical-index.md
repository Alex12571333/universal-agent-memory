# RFC 0013: Protected lexical index for pgcrypto memory

## Context

When canonical memory text is encrypted with pgcrypto, PostgreSQL cannot use
the plaintext FTS index. Current recall is correct but decrypts the authorized
workspace candidate set in the application process. This is a performance P1,
not a reason to store plaintext search terms.

## Proposed contract

Add an opt-in `UAM_PROTECTED_SEARCH_INDEX=hmac-v1` mode. For each canonical
memory item, derive normalized query tokens, apply HMAC-SHA-256 with a distinct
`UAM_PROTECTED_SEARCH_INDEX_KEY`, and store only fixed-length digests in a
tenant/workspace-scoped `memory_search_tokens` table. The encryption key and
search-index key must be different and startup must reject equal values.

Recall derives the same digests, performs an indexed intersection query scoped
by tenant/workspace/agent/thread/status, then decrypts and scores only the
bounded candidate IDs. The system documents equality/frequency leakage of a
blind index; it never claims semantic privacy or prefix/fuzzy search in this
mode.

## Compatibility

The default remains the current correctness-first fallback. Plaintext FTS is
unchanged. Empty/legacy token indexes fall back to the existing authorized
workspace scan, so rollout cannot cause recall loss.

## Data migration

1. Add the token table, RLS policy and `(tenant_id, workspace_id, digest)` B-tree index.
2. Backfill in restart-safe batches by decrypting canonical rows through the
   existing application boundary; report rows scanned/indexed/failed.
3. Dual-write tokens during retain/supersede and delete tokens only through
   canonical lifecycle handling.
4. Require a complete backfill report before enabling indexed-only operation.

## Implementation status

- Migrations 013/014 install the RLS-scoped token table and enforce that every
  token row has the same tenant/workspace as its canonical memory item.
- `hmac-v1` dual-writes tokens during canonical retention and supersession in
  the same PostgreSQL transaction. Startup requires a separate key and a
  positive `UAM_PROTECTED_SEARCH_INDEX_KEY_VERSION`.
- The reader uses HMAC terms only after a scoped SQL coverage check sees a
  per-document marker for every non-deleted item at the active key version.
  Missing, partial or tokenless backfill always retains the authorized fallback;
  no configuration flag can force indexed-only recall.
- Batch backfill is restart-safe. Rotation execution and query-plan release
  evidence were validated in an isolated PostgreSQL 17 target run; see
  [TARGET_PROTECTED_SEARCH_ROTATION_VALIDATION_2026_07_14.md](../TARGET_PROTECTED_SEARCH_ROTATION_VALIDATION_2026_07_14.md).
  Coordinated all-writer deployment and retained reports remain release gates.

### Backfill operation

Run one tenant/workspace at a time with the same restricted runtime role used
by the memory server. The state file contains only an `(created_at, item_id)`
cursor and is atomically updated after each committed batch; it contains no
canonical text or token digests.

```bash
UAM_PROTECTED_SEARCH_INDEX=hmac-v1 \
UAM_PROTECTED_SEARCH_INDEX_KEY_FILE=/run/secrets/protected_search_index_key \
PYTHONPATH=src python scripts/backfill_protected_search_tokens.py \
  --tenant-id <tenant-uuid> --workspace-id <workspace-uuid> \
  --state-file ./ops/protected-search-<workspace-uuid>.state.json \
  --report ./ops/protected-search-<workspace-uuid>.report.json \
  --batch-size 500
```

Do not treat a backfill report as a rotation approval. Retain the final report,
verify `complete: true`, perform a scoped count/restart drill, then capture the
reader's separate query-plan evidence before relying on the capacity benefit.

### Key rotation and retirement

1. Deploy the new distinct index key and increment
   `UAM_PROTECTED_SEARCH_INDEX_KEY_VERSION` on every writer. The reader falls
   back until the new version is covered.
2. Backfill and probe every workspace with the new version.
3. Use an administrator-only DSN to remove an old version. The command refuses
   `--apply` unless the new marker covers every non-deleted item in that
   workspace.

```bash
UAM_PROTECTED_SEARCH_INDEX=hmac-v1 \
UAM_PROTECTED_SEARCH_INDEX_KEY_FILE=/run/secrets/protected_search_index_key \
UAM_ADMIN_DATABASE_URL_FILE=/run/secrets/admin_database_url \
PYTHONPATH=src python scripts/retire_protected_search_key.py \
  --tenant-id <tenant-uuid> --workspace-id <workspace-uuid> \
  --retire-key-version <old-version> \
  --report ./ops/protected-search-key-retirement.json --apply
```

The report contains counts and versions only. Never run the destructive step
until all memory-server/worker replicas use the new key and retained backfill
and query-plan evidence exists.

Capture that evidence with a non-secret plan probe. It fails unless coverage is
complete and PostgreSQL can use `memory_search_tokens_lookup_idx`; the report
redacts HMAC literals and contains neither memory text nor the query text.

```bash
UAM_PROTECTED_SEARCH_INDEX=hmac-v1 \
UAM_PROTECTED_SEARCH_INDEX_KEY_FILE=/run/secrets/protected_search_index_key \
PYTHONPATH=src python scripts/protected_search_index_probe.py \
  --tenant-id <tenant-uuid> --workspace-id <workspace-uuid> \
  --query 'non-secret release probe terms' \
  --report ./ops/protected-search-index-plan.json
```

## Rollback

Disable `UAM_PROTECTED_SEARCH_INDEX`; recall reverts to the current fallback.
Do not delete the token table until an operator has verified rollback and key
retirement. Key rotation creates a versioned second digest set, backfills it,
switches reads, then deletes the prior version.

## Acceptance tests

- No plaintext token, normalized text or encryption key appears in the token table.
- Scoped queries never return another tenant, workspace, agent or thread.
- Exact token recall works after encrypted retain, supersede and restart.
- Partial/failed backfill cannot reduce recall versus fallback.
- Rotation supports old and new digest versions during transition.
- PostgreSQL query-plan evidence proves candidate lookup uses the token index.
