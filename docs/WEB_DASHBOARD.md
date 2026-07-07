# Web dashboard

`GET /ui` serves the local operator dashboard for Universal Agent Memory.

The dashboard is intentionally self-contained inside the FastAPI server: no
Node build step is required for the Docker server. It is designed as a local
control surface for a single self-hosted memory server, not as SaaS admin UI.

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
  probe an embedding endpoint, and copy Docker env values.

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

## Verification

The dashboard is covered by:

- `tests/test_api.py::test_memory_list_endpoint_and_operator_ui`
- `tests/test_api.py::test_model_settings_endpoints_save_and_probe_fake_provider`
- `scripts/api_e2e_eval.py`, including `PASS ui` and `PASS model_settings`

