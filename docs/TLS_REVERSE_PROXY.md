# TLS reverse proxy deployment

Obelisk Memory can run on localhost with port `6798` exposed. For any deployment
reachable from another machine, put TLS, access policy and logging in front of
the API/UI.

The repository ships a Caddy example:

```text
deploy/reverse-proxy/Caddyfile
deploy/reverse-proxy/docker-compose.caddy.yml
```

## Environment

Add these values to `.env.production`:

```dotenv
UAM_PUBLIC_HOST=memory.example.com
UAM_PUBLIC_EMAIL=operator@example.com
```

`UAM_PUBLIC_HOST=localhost` is acceptable only for local testing. Caddy issues
real certificates when the host has public DNS pointing at the server and ports
`80`/`443` are reachable.

## Start with TLS proxy

```bash
docker compose \
  -f docker-compose.prod.yml \
  -f deploy/reverse-proxy/docker-compose.caddy.yml \
  --env-file .env.production \
  up -d --build
```

Then verify:

```bash
curl -fsS https://$UAM_PUBLIC_HOST/health
curl -fsS -H "Authorization: Bearer $UAM_API_KEY" \
  https://$UAM_PUBLIC_HOST/metrics
UAM_API_KEY=... PYTHONPATH=src python scripts/deployment_preflight.py \
  --public-url https://$UAM_PUBLIC_HOST \
  --backend-url http://$UAM_PUBLIC_HOST:6798 \
  --report ./ops/deployment-preflight.json
```

## Direct port exposure

`docker-compose.prod.yml` publishes `6798:8080` for trusted local/team pilots.
The supplied Caddy overlay uses Docker Compose `!override` to replace that with
`127.0.0.1:6798:8080` for local diagnostics only. Confirm the final config
before release:

```bash
docker compose \
  -f docker-compose.prod.yml \
  -f deploy/reverse-proxy/docker-compose.caddy.yml \
  --env-file .env.production \
  config
```

Do not call the deployment production-hardened until the rendered config shows
`host_ip: 127.0.0.1` for backend `6798`, external clients can reach only
`80`/`443`, `ops/deployment-preflight.json` reports
`"backend_publicly_reachable": false`, and firewall/security-group policy
matches that posture.

## Headers

The API already sets baseline browser/API security headers. The Caddy example
adds an outer layer:

- HSTS;
- `X-Content-Type-Options`;
- `X-Frame-Options`;
- `Referrer-Policy`;
- `Permissions-Policy`;
- gzip/zstd compression.

Keep API bearer authentication enabled even behind TLS. TLS protects transport;
`UAM_API_KEY`/`UAM_API_KEYS` protect the memory plane.
