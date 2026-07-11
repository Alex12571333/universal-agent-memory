# Target recovery validation — 2026-07-12

This is a real local-Docker recovery drill against the LAN deployment, not a
unit-test fixture. It contains no database dump, API key, conversation, or
memory text.

## Encrypted backup and restore

`obelisk-scheduled-backup-report-v2` completed successfully at
`2026-07-11T15:05:33Z`:

- PostgreSQL dump was encrypted with AES-256-GCM;
- an isolated PostgreSQL 17 restore verified required schema, forced RLS and
  source/restore row-count parity;
- audit export completed;
- the plaintext temporary dump was removed by the backup runner.

The local backup-encryption key is outside the repository in a mode-`0600`
operator configuration file. The backup artifact itself stays in the ignored
`backups/` directory.

## Restored-ledger semantic recovery

A second isolated restore was retained only for the duration of this probe.
An ephemeral database role was created inside that temporary container and a
fresh Qdrant collection, `recovery_probe_20260712`, was used. The production
PostgreSQL database and the active `memory_items` collection were never used
as an input vector source.

`obelisk-restored-reindex-probe-v1` passed at `2026-07-11T15:07:42Z`:

| Check | Result |
| --- | --- |
| Embedding model/dimension | `jina-embeddings-v4` / `2048` |
| Restored active records indexed | 35 |
| Qdrant points verified | 35 |
| Semantic source | `qdrant_hybrid` present |
| Exact restored source candidate | found with non-zero semantic score |

The temporary restore container, Docker volume and recovery Qdrant collection
were deleted immediately after the successful probe.

## Remaining recovery work

The recovery path is now functionally proven for this deployment. It still
needs a scheduled, retained and signed evidence bundle for a release claim;
this manual drill is not a replacement for that recurring operational control.
