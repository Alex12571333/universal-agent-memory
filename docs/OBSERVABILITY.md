# Observability

Obelisk Memory exposes Prometheus text metrics at `/metrics`. The endpoint is
protected by an operator/admin key in production.

## Scrape target

Example Prometheus scrape config:

```yaml
scrape_configs:
  - job_name: obelisk-memory
    metrics_path: /metrics
    scheme: http
    static_configs:
      - targets: ["memory-server:8080"]
    authorization:
      type: Bearer
      credentials_file: /run/secrets/uam_operator_metrics_key
```

The embedding worker exports its own private scrape endpoint. This is required:
API-process embedding counters do not describe asynchronous embedding work.

```yaml
  - job_name: obelisk-embedding-worker
    metrics_path: /metrics
    static_configs:
      - targets: ["embedding-worker:9091"]
```

Both endpoints remain inside the Docker network in the local deployment; no
public port, domain, VPN or reverse proxy is required.

## Dashboard and alerts

Repository artifacts:

- `deploy/observability/grafana-dashboard.json`
- `deploy/observability/prometheus-alerts.yml`

The dashboard covers:

- outbox backlog, dead letters and oldest-event lag;
- active processed-event leases;
- worker embedding throughput/failures and API reindex throughput/failures;
- embedding/reindex latency;
- degraded retrieval sources and cumulative source failures;
- durable required/ready/missing/stale worker-role counts;
- memory, audit and API-key ledger growth.

The alert rules cover the same production failure modes used by
`scripts/check_metrics_health.py`: dead letters, outbox backlog, outbox lag,
stuck leases, embedding failures, reindex failures, missing/stale worker
heartbeats and recall-source outages.

## Runtime dependency gate

Production Compose configures `UAM_REQUIRED_WORKERS` with `outbox-relay`,
`embedding-worker` and `maintenance-worker`. Each process writes a tenant-scoped
heartbeat to PostgreSQL every `UAM_WORKER_HEARTBEAT_SECONDS`; `/ready` returns
`503 not_ready` when no running replica for a required role is newer than
`UAM_WORKER_HEARTBEAT_TTL_SECONDS`. The public response contains only role
names and aggregate replica counts, never worker IDs, hostnames or internal
addresses. Basic development Compose leaves this gate opt-in.

The authenticated scheduled dependency check remains defense in depth for NATS
and the embedding worker's private HTTP endpoint:

```bash
UAM_RUNTIME_DEPENDENCY_PROBES=true \
PYTHONPATH=src python scripts/check_runtime_dependencies.py \
  --status-url http://localhost:6798/v1/system/status \
  --report ./ops/runtime-dependencies-health.json
```

Its report format is `obelisk-runtime-dependencies-health-v1`. It records only
component status and a scrubbed URL—never an API key, query string or endpoint
credentials. Configure `UAM_METRICS_ALERT_WEBHOOK` or `UAM_ALERT_COMMAND` to
route a failed run, and retain the report beside metrics evidence.

## Release gate

Before claiming a production release:

```bash
UAM_API_KEY=... PYTHONPATH=src python scripts/check_metrics_health.py \
  --metrics-url http://localhost:6798/metrics \
  --max-worker-unready 0 \
  --require-metric uam_outbox_pending_total \
  --require-metric uam_outbox_dead_letter_total \
  --require-metric uam_outbox_lag_seconds \
  --require-metric uam_processed_events_inflight_total \
  --require-metric uam_embedding_failures_total \
  --require-metric uam_retrieval_degraded_sources \
  --require-metric uam_retrieval_source_failures_total \
  --require-metric uam_worker_required \
  --require-metric uam_worker_ready \
  --require-metric uam_worker_unready \
  --require-metric uam_worker_missing \
  --require-metric uam_worker_stale \
  --report ./ops/metrics-health.json

PYTHONPATH=src python scripts/observability_preflight.py \
  --grafana-dashboard ./deploy/observability/grafana-dashboard.json \
  --prometheus-alerts ./deploy/observability/prometheus-alerts.yml \
  --report ./ops/observability-preflight.json
```

For full production, import the dashboard into Grafana, load the Prometheus alert
rules into the target alerting stack, and preserve the generated
`ops/metrics-health.json` and `ops/observability-preflight.json` in release
evidence.
