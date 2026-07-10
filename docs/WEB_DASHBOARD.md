# Web dashboard

`GET /ui` serves the local operator dashboard for Obelisk Memory.

The production dashboard is a React/Vite single-page app in `web/`. The Docker
image builds it in a Node build stage and copies the generated files into
`/app/web/dist`; FastAPI then serves that build from `/ui`.

This is a local self-hosted control surface for one memory server, not a hosted
SaaS admin UI. If the React build is absent during development, the server serves
the embedded minimal dashboard so API smoke tests and local operation remain
available.

## Browser authentication

When API keys are configured, `/ui` and its static assets remain loadable so the
login screen can render, but every operator API remains protected. The login
form exchanges an `operator` or `admin` key for a short-lived HMAC-signed
HttpOnly cookie. The original key is held only in component memory during the
exchange and is never written to localStorage, sessionStorage or JavaScript
cookies.

Unsafe cookie-authenticated requests require the per-session `X-CSRF-Token`.
Cookies use `SameSite=Strict`; production requires `Secure` and TLS. Key
revocation or rotation invalidates existing sessions because every request
re-resolves the signed fingerprint against current server configuration.

Required production configuration:

```dotenv
UAM_UI_SESSION_SIGNING_KEY_FILE=/run/secrets/ui_session_signing_key
UAM_UI_SESSION_TTL_SECONDS=28800
UAM_UI_COOKIE_SECURE=true
```

## Main areas

- **Overview dashboard** — Russian glass-style cockpit with workspace KPIs,
  OpenClaw/Hermes integration status, live model status and an interactive
  memory graph.
- **Память** — list memories, filter by layer/status/label and run recall.
- **Записать** — append a new memory through `/v1/memory/retain`.
- **Конфликты** — inspect conflict candidates and review rationale.
- **Хранилище** — edit human-readable memory text. Frontmatter, provenance,
  revisions and embeddings stay under the hood. Saving uses vault import,
  creates a new append-only revision and triggers reindex.
- **Граф** — Obsidian-style force graph. Nodes can be dragged, the canvas can
  be panned/zoomed, labels can be toggled, and physics can be restarted.
- **Модели** — inspect runtime embedding settings, save desired model config,
  choose hosted or self-hosted OpenAI-compatible templates, probe an endpoint,
  and copy Docker env values.

## Local frontend development

Run the server normally, then start Vite:

```bash
cd web
npm install
npm run dev
```

Vite proxies `/v1`, `/health` and `/metrics` to `http://127.0.0.1:6798`.

For production-like local serving through FastAPI:

```bash
cd web
npm run build
cd ..
UAM_WEB_DIST="$PWD/web/dist" uvicorn memory_plane.api.app:create_app --factory --host 0.0.0.0 --port 6798
```

## Model settings behavior

The web UI deliberately does not hot-swap the live embedding model inside a
running Qdrant index. Changing provider/model/dimension is a production-safe
two-step operation:

1. Save desired config in the dashboard or update Docker env:
   - `UAM_EMBEDDING_PROVIDER`
   - `UAM_EMBEDDING_MODEL`
   - `UAM_EMBEDDING_DIM`
   - `UAM_EMBEDDING_BASE_URL`
   - `UAM_EMBEDDING_TIMEOUT_SECONDS`
2. Build and verify a newly named collection with
   `scripts/migrate_vector_collection.py`.
3. Switch `memory-server` and `embedding-worker` to the new
   `UAM_QDRANT_COLLECTION` together.

This prevents accidentally mixing vectors produced by different models or
different dimensions in the same Qdrant collection. See
[VECTOR_COLLECTION_MIGRATION.md](VECTOR_COLLECTION_MIGRATION.md).

The desired settings endpoint can persist JSON when
`UAM_MODEL_SETTINGS_PATH=/path/to/settings.json` is set. Without that variable,
settings are kept in memory for the current server process.

Provider secrets are never written to this JSON file. A key entered in the UI
is held only by the current server process so the endpoint can be tested; after
a restart it must come from `UAM_EMBEDDING_API_KEY_FILE` (or another deployment
secret). Production also requires `UAM_MODEL_ENDPOINT_ALLOWLIST` with the exact
origins that operators may probe, for example:

```dotenv
UAM_MODEL_ENDPOINT_ALLOWLIST=https://api.openai.com,https://models.example.com
```

Origins include scheme, host and effective port. Credentials, query strings,
fragments, unlisted origins and HTTP redirects are rejected. When the allowlist
is absent, only localhost and numeric loopback endpoints are accepted.

API:

- `GET /v1/settings/models`
- `PUT /v1/settings/models`
- `POST /v1/settings/models/test`

Self-hosted template:

```text
UAM_EMBEDDING_PROVIDER=openai-compatible
UAM_EMBEDDING_BASE_URL=http://127.0.0.1:8002/v1
UAM_EMBEDDING_MODEL=jina-embeddings-v4
UAM_EMBEDDING_DIM=2048
UAM_EMBEDDING_SEND_DIMENSIONS=false
```

The UI's self-hosted template fills these values and **Test endpoint**
verifies that the endpoint returns `2048` floats before you restart/reindex.
For generic `/v1/embeddings` gateways use `openai-compatible`; reserve
`openai` for the OpenAI-hosted profile that sends the optional `dimensions`
field.

## Verification

The dashboard is covered by:

- `tests/test_api.py::test_memory_list_endpoint_and_operator_ui`
- `tests/test_api.py::test_operator_ui_serves_react_dist_when_built`
- `tests/test_api.py::test_operator_browser_session_uses_httponly_cookie_and_csrf`
- `tests/test_api.py::test_model_settings_endpoints_save_and_probe_fake_provider`
- `npm run build` in `web/`
- `scripts/api_e2e_eval.py`, including `PASS ui` and `PASS model_settings`
