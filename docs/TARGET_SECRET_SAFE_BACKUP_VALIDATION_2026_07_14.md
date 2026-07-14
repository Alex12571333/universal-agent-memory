# Target validation — secret-safe backup and restore

Date: 2026-07-14

Target: local LAN node `192.168.0.14`

Validated code tree: `3281016`

## Scope

An isolated PostgreSQL 17 deployment was created on the target node. The test
applied every migration through `016_audit_immutability.sql`, created a real
custom-format dump through the Docker Compose fallback, restored that dump into
a second temporary PostgreSQL container, and compared critical table counts.

The running OpenClaw/Hermes agents, their workspaces, and the normal Obelisk
database were not used or modified.

## Result

`PASS`

The target run proved:

- `pg_dump` produced a non-empty custom-format artifact;
- the restore contained every required production table;
- tenant RLS remained enabled and forced after restoration;
- critical source and restored row counts matched;
- the temporary restore container, source Compose project, networks and volumes
  were removed after the run.

Regression tests additionally inspect the generated command arrays. Database,
encryption and signing secrets are absent from subprocess argv. Host libpq
tools use a short-lived mode-`0600` `PGPASSFILE`; Docker fallback passwords use
stdin, and temporary restore credentials use a deleted mode-`0600` env file.

## Recovery defects found during the target run

The first candidate exposed two pre-existing readiness races:

1. the PostgreSQL Unix socket could become ready before the temporary TCP
   listener, while restore used `localhost`;
2. `pg_isready` could succeed during the image's initialization server before
   `POSTGRES_DB` had actually been created.

The restore now uses the local Unix socket and waits for a real `select 1`
against the exact target database before running `pg_restore`. The complete
target workflow passed after these corrections.
