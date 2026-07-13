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

## Recovery regression closure — 2026-07-13

The scheduled semantic-recovery path was exercised again against a newly
created local signed AES-256-GCM backup.  Two defects were found and fixed
before recording the successful result:

1. the isolated drill did not pass the backup decryption key to its nested
   restore process when invoked with only `--runtime-env-file`;
2. Docker's raw `--env-file` parser retained Compose single quotes, which made
   the JSON-valued Qwen extra-body profile invalid inside the recovery probe.

The drill now resolves secrets from the process or selected runtime env/secret
file, passes them only through the nested process environment, and creates a
temporary mode-`0600` Docker-compatible env file.  It never records keys or
database URLs in command arguments or JSON evidence.

It also records non-secret `source_row_counts` in the fresh restore report.
This lets a later isolated recovery validate an older backup against the row
snapshot captured when that exact backup was created, rather than falsely
comparing it to a subsequently changed live database.  A legacy backup without
that snapshot remains unsuitable for this stricter parity mode.

The final fresh-backup recovery evidence passed all five gates: schema/RLS and
row parity, canonical vault health, reindex, and dense semantic recall.  The
temporary restored ledger indexed and verified 75 points with
`jina-embeddings-v4` at dimension `2048`; `qdrant_hybrid` returned the
restored candidate.  The backup, audit bundle and detailed reports remain
local operational artifacts and are not committed to the public repository.
