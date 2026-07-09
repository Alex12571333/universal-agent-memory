# Web dashboard

`GET /ui` serves the local operator dashboard for Universal Agent Memory.

The production dashboard is a React/Vite single-page app in `web/`. The Docker
image builds it in a Node build stage and copies the generated files into
`/app/web/dist`; FastAPI then serves that build from `/ui`.

This is still a local self-hosted control surface for one memory server, not a
hosted SaaS admin UI. If the React build is absent during development, the
server falls back to the legacy embedded HTML dashboard so API smoke tests and
minimal local operation remain available.

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
  use the DGX Spark Jina v4 Q8 preset, probe an embedding endpoint, and copy
  Docker env values.

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
2. Restart `memory-server` and `embedding-worker`, then run workspace reindex.

This prevents accidentally mixing vectors produced by different models or
different dimensions in the same Qdrant collection.

The desired settings endpoint can persist JSON when
`UAM_MODEL_SETTINGS_PATH=/path/to/settings.json` is set. Without that variable,
settings are kept in memory for the current server process.

API:

- `GET /v1/settings/models`
- `PUT /v1/settings/models`
- `POST /v1/settings/models/test`

Recommended real local preset:

```text
UAM_EMBEDDING_PROVIDER=tei
UAM_EMBEDDING_BASE_URL=http://192.168.0.10:8002
UAM_EMBEDDING_MODEL=jina-embeddings-v4
UAM_EMBEDDING_DIM=2048
```

The UI's **Use DGX preset** button fills these values and **Test endpoint**
verifies that the endpoint returns `2048` floats before you restart/reindex.

## Verification

The dashboard is covered by:

- `tests/test_api.py::test_memory_list_endpoint_and_operator_ui`
- `tests/test_api.py::test_operator_ui_serves_react_dist_when_built`
- `tests/test_api.py::test_model_settings_endpoints_save_and_probe_fake_provider`
- `npm run build` in `web/`
- `scripts/api_e2e_eval.py`, including `PASS ui` and `PASS model_settings`
