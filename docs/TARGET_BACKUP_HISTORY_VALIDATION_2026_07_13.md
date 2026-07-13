# Target backup-history validation — 2026-07-13

## Scope

This document records a local, non-destructive validation of repeated
production backup runs. It closes neither off-machine retention nor key
custody: those remain deployment controls. It does prove that a retained run
contains the complete, tamper-evident recovery bundle expected by Obelisk:

- an AES-256-GCM encrypted PostgreSQL dump;
- an isolated successful restore-drill report;
- a bounded audit-export manifest; and
- an HMAC-SHA256 signed manifest whose file hashes still verify.

## Method

Two independent executions of `scripts/scheduled_backup.py` ran against the
local Docker appliance. Each used its configured secret environment, required
a bundle signature, and wrote only under a temporary evidence root:

```text
/tmp/obelisk-backup-history-20260713/
```

The two fixed run identifiers were `20260713T150000Z` and
`20260713T151000Z`. Neither command overwrote the normal `backups/` directory
or its `latest-backup-report.json`.

The read-only verifier then checked both retained bundles:

```bash
PYTHONPATH=src:scripts python scripts/verify_backup_history.py \
  --backup-dir /tmp/obelisk-backup-history-20260713/backups \
  --min-runs 2 \
  --report /tmp/obelisk-backup-history-20260713/reports/history.json \
  --require-signature
```

## Result

```text
backup_history=PASS
verified_runs=2
20260713T150000Z signed=signed restore=passed
20260713T151000Z signed=signed restore=passed
```

The history report has format `obelisk-backup-history-validation-v1` and
returned `ok: true`. It validates the bundle bytes and signatures without
printing secret keys, dump content, raw memory text, or audit content.

## Operational use

Run the verifier against the retained production backup root after a schedule
has produced at least two full runs:

```bash
PYTHONPATH=src:scripts python scripts/verify_backup_history.py \
  --backup-dir ./backups \
  --min-runs 2 \
  --report ./backups/backup-history-report.json \
  --require-signature
```

Use `--since YYYYmmddTHHMMSSZ` when beginning a new, explicitly recorded
schedule epoch. The boundary is auditable in the report; it must not be used
to hide a failing run within that epoch.

For a production release, retain the encrypted artifacts and report outside
the Docker volume, use a signing key held separately from the encryption key,
and periodically perform a restore on an operator-controlled target. This
validation is strong local recovery evidence, not high availability or an
immutable-storage guarantee.
