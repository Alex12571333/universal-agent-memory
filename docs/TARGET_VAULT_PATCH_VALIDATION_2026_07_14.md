# Target validation — targeted vault CAS patch

Date: 2026-07-14

Target: local LAN node `192.168.0.14`

Source commit: `e9f6309`

## Scope

This probe validated the new targeted vault editor against a real isolated
PostgreSQL 17 instance. It did not use or modify the OpenClaw/Hermes runtime,
their workspaces, or the appliance's production database.

The test provisioned the repository's least-privileged application role,
applied every migration with checksum verification, and ran only
`test_targeted_vault_patch_is_postgres_cas_and_idempotent`.

## Result

`PASS` — one test passed.

The test proved that:

- a section edit creates revision 2 and preserves the original immutable row;
- the new revision keeps the expected `supersedes_id` lineage;
- an unrelated adjacent section remains unchanged;
- retrying the identical patch returns the same revision without another
  outbox event;
- a different concurrent patch against revision 1 is rejected by PostgreSQL
  CAS.

The temporary source tree and PostgreSQL container were removed automatically
after the test. No credentials or memory text from the running appliance were
captured in this evidence.
