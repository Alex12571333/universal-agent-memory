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
- The indexed recall reader, batch backfill, rotation executor and query-plan
  release evidence are deliberately not implemented yet. Existing recall keeps
  its authorized fallback, so enabling dual-write cannot reduce recall.

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
