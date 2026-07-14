# Target audit-integrity validation — 2026-07-14

## Scope

This is a disposable target-host validation of migration `016` and migration
checksum enforcement. It ran from repository `main` at merge commit `5e6d4f9`
on host `192.168.0.14` with Docker Engine `29.5.0` and PostgreSQL 17 Alpine.

The probe used an isolated Compose project and dedicated temporary volume. It
did not connect to or mutate the normal Obelisk database, OpenClaw state or
Hermes state.

## Checks

1. A clean database applied all 16 migrations and every
   `schema_migrations` row had a non-null SHA-256 checksum.
2. An administrator attempted to update an `audit_events` row. PostgreSQL
   rejected it with the expected `audit_events is append-only` trigger error.
3. An administrator attempted an ordinary delete of the row. PostgreSQL
   rejected it with the same expected trigger error.
4. A transaction enabled `uam.audit_retention_mode=on` with transaction-local
   `set_config`, deleted the row and committed. The final row count was zero.
5. The recorded digest for `001_initial.sql` was deliberately replaced in the
   disposable database. A second migration run failed with the expected
   `applied migration checksum mismatch: 001_initial.sql` error.

## Result

```text
checksummed_migrations=16
update_guard=PASS
delete_guard=PASS
retention_delete=PASS
checksum_mismatch_guard=PASS
update_error=expected
delete_error=expected
checksum_error=expected
temporary_stack_removed=true
```

The temporary PostgreSQL container, network and volume were removed after the
probe. This proves the database behavior on the target host; it does not replace
the separately required signed audit-export/retention schedule evidence for the
long-running appliance.
