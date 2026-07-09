# Security policy

Obelisk Memory stores agent context, conversation fragments, tool traces, and
operator-edited vault files. Treat it as sensitive infrastructure.

## Supported deployment posture

- Run behind localhost, VPN, or a private network by default.
- Expose only `memory-server`/`6798` to clients.
- Do not expose PostgreSQL, Qdrant, NATS, or MinIO to an untrusted network.
- Use `docker-compose.prod.yml` for production because it keeps internal services
  off host ports.
- Put TLS, IP allowlisting, and rate limits at the reverse proxy when accessing
  the service outside the host.

## Authentication

Set a long random `UAM_API_KEY` in production. Without it, the server is in local
development mode.

For production integrations, prefer additional scoped keys:

```dotenv
UAM_API_KEYS=openclaw:replace-openclaw-key:agent,hermes:replace-hermes-key:agent,operator:replace-operator-key:operator
```

Scopes:

- `admin` and `operator` can access operational routes, UI, docs, and metrics;
- `agent` can read/write agent memory routes but cannot access metrics/settings;
- `read` can recall/read only;
- `write` can write non-operator routes.

`/health` is intentionally public for probes. API routes, OpenAPI docs, UI, and
metrics require bearer auth when `UAM_API_KEY` is set.

Configured keys are mirrored into the API-key registry by non-secret
fingerprint. Operators can inspect metadata and revoke a key without storing the
bearer secret:

```bash
curl -H "Authorization: Bearer $UAM_API_KEY" http://localhost:6798/v1/keys
curl -X POST -H "Authorization: Bearer $UAM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"reason":"rotation"}' \
  http://localhost:6798/v1/keys/<key_id>/revoke
```

Revoked keys are denied even if the old secret is still present in
`.env.production`; replace the secret and restart the server to complete
rotation.

## Secrets and PII

The privacy guard redacts common secrets and high-risk PII before retention.
Keep it enabled:

```dotenv
UAM_PRIVACY_ENABLED=true
UAM_PRIVACY_ACTION=redact
```

The guard is a safety net, not a replacement for client-side hygiene. Agent
plugins should avoid sending raw credentials, private keys, cookies, or full
tool logs unless explicitly needed.

## Operational rules

- Rotate `UAM_API_KEY`, scoped agent keys, database passwords, and MinIO
  credentials when sharing access with new operators.
- Back up PostgreSQL before migrations and before major embedding/model changes.
- Keep `.env.production` out of git.
- Do not import edited vault files with `--apply` until dry-run output is
  reviewed.
- Treat graph and LLM-curated facts as proposals until accepted or verified.

## Reporting vulnerabilities

For this private/operator-owned project, open a private issue or contact the
repository owner directly. Include:

- affected version/commit;
- reproduction steps;
- expected impact;
- whether memory contents, credentials, or agent execution could be exposed.
