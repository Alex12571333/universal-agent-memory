# Production gap audit addendum — 2026-07-13

This addendum records changes made after
[PRODUCTION_GAP_AUDIT_2026_07_10.md](PRODUCTION_GAP_AUDIT_2026_07_10.md).
It narrows specific findings; it does not claim that the full production gate
is closed.

## Corrected P1 findings

### Desired model settings survive local appliance restarts

The local `docker-compose.yml` now mounts a dedicated named volume at
`/var/lib/obelisk` and configures
`UAM_MODEL_SETTINGS_PATH=/var/lib/obelisk/model-settings.json`. An init service
assigns that directory to the unprivileged API user before the server starts.

Only non-secret desired settings are written. The model-settings writer uses an
atomic mode-`0600` file and deliberately excludes provider API keys. A live
save → restart → readback proof on the local appliance confirmed persistence,
absence of a persisted API key and `0600` ownership. The change is merged in
PR #220.

### Multi-layer hybrid Qdrant retrieval is implemented

The Qdrant adapter writes both dense and sparse vectors and performs hybrid
fusion. When a recall specifies multiple layers it queries every requested
layer with a tenant/workspace filter instead of silently retaining a single
layer. The remaining release work is target traffic evidence, score-quality
evaluation and collection-migration evidence—not implementation of a
single-layer dense-only path.

### Conservative bilingual preference conflicts

Conflict and reflection maintenance now share one deterministic extractor for
English and Russian present-tense preferences, for example:

- `User prefers Qwen.`
- `Пользователь предпочитает Qwen.`

Two different values for the same subject are surfaced as an operator-review
conflict. The extractor refuses temporal-transition language such as
`раньше`, `сейчас`, `formerly`, `now` and `switched`; such statements remain
opaque evidence for the proposal/curation pipeline and cannot be silently
converted into a durable timeless winner. This is deliberately limited
coverage, not a general multilingual NLU claim.

### Migration identity and database-enforced audit immutability

The migration ledger now records the SHA-256 digest of every SQL migration.
Existing rows without a digest are baselined once under the migration advisory
lock; any later byte change to an applied migration fails startup instead of
silently accepting schema drift.

PostgreSQL now rejects `UPDATE` and `DELETE` on `audit_events` with an
append-only trigger, including operations performed by the table owner. The
only deletion exception is the existing signed-export retention flow, which
enables a transaction-local `uam.audit_retention_mode` immediately before its
bounded delete. Runtime ACLs still deny the application role direct audit
updates and deletes, so this exception does not grant a new API capability.

This closes the repository implementation part of P1 finding 8. A target
migration report and a real retention-export/prune probe must still be retained
for a production release.

The isolated target probe recorded in
[TARGET_AUDIT_INTEGRITY_VALIDATION_2026_07_14.md](TARGET_AUDIT_INTEGRITY_VALIDATION_2026_07_14.md)
subsequently confirmed all 16 checksums, administrator UPDATE/DELETE rejection,
the transaction-local retention exception and checksum-mismatch startup
failure on PostgreSQL 17. The remaining evidence gap is the installed,
long-running signed retention schedule—not trigger or checksum behavior.

### Protected-search key rotation target proof

An isolated PostgreSQL 17 run on `192.168.0.14` completed the full protected
search lifecycle: version 1 backfill, version 2 backfill, dual-version
coexistence, complete-coverage/index-plan probe, guarded retirement of version
1 and preservation of version 2. The evidence is recorded in
[TARGET_PROTECTED_SEARCH_ROTATION_VALIDATION_2026_07_14.md](TARGET_PROTECTED_SEARCH_ROTATION_VALIDATION_2026_07_14.md).

This closes the missing rotation-execution and query-plan behavior proof in P1
finding 6. Production still requires a coordinated rollout in which every
writer advertises the new version before retirement and the resulting reports
are preserved with release evidence.

## Remaining gate

No deterministic extractor can safely infer all paraphrases, negations or
temporal relations. Proposal-first curation, evidence quotes, non-temporal
auto-accept policy and operator conflict review remain mandatory. A production
claim still requires preserved target evidence for long-running agents,
recovery, worker reliability, backup retention, monitoring and the other gates
listed in the base audit.
