# Target validation — durable authorization denials

Date: 2026-07-14

Target: local LAN node `192.168.0.14`

Validated code tree: `33244d8`

## Scope

The target run cloned the published hardening branch into a temporary directory,
started a fresh isolated PostgreSQL 17 container, applied every migration through
`016_audit_immutability.sql`, provisioned the least-privileged runtime role, and
ran the API denial integration test through that role.

The running OpenClaw/Hermes agents, their extensions, workspaces, normal Obelisk
database and model endpoints were not used or modified.

## Result

`PASS`

The target run proved:

- the database connection used by the API test was a non-superuser runtime role;
- an invalid bearer request returned `401`;
- PostgreSQL durably stored an `auth.request.denied` event;
- the stored row had status `denied`, actor `anonymous`, route family
  `/v1/settings` and reason `invalid_credential`;
- the submitted bearer token, configured valid token and query-string marker
  were absent from the stored domain event;
- the temporary PostgreSQL container, clone, virtual environment and runner
  script were removed after the test.

Target summary:

```text
target_commit=33244d8
target_postgres_role=non_superuser
target_auth_denial_row=PASS
target_secret_redaction=PASS
durable_auth_denial_target=PASS
target_cleanup=PASS
```

## Remaining boundary

If canonical audit storage is unavailable, authorization remains fail-closed but
the denied event cannot be durable. The server emits a fixed secret-free error
message for that condition. Operators must alert on that message, preserve the
surrounding process logs and treat the interval as missing security evidence.
