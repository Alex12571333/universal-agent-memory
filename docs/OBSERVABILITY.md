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

For non-local deployments, scrape through the private network or VPN side of the
reverse proxy. Do not expose `/metrics` publicly.

## Dashboard and alerts

Repository artifacts:

- `deploy/observability/grafana-dashboard.json`
- `deploy/observability/prometheus-alerts.yml`

The dashboard covers:

- outbox backlog, dead letters and oldest-event lag;
- active processed-event leases;
- embedding and reindex throughput/failures;
- embedding/reindex latency;
- degraded retrieval sources and cumulative source failures;
- memory, audit and API-key ledger growth.

The alert rules cover the same production failure modes used by
`scripts/check_metrics_health.py`: dead letters, outbox backlog, outbox lag,
stuck leases, embedding failures, reindex failures and recall-source outages.

## Release gate

Before claiming a production release:

```bash
UAM_API_KEY=... PYTHONPATH=src python scripts/check_metrics_health.py \
  --metrics-url http://localhost:6798/metrics \
  --require-metric uam_outbox_pending_total \
  --require-metric uam_outbox_dead_letter_total \
  --require-metric uam_outbox_lag_seconds \
  --require-metric uam_processed_events_inflight_total \
  --require-metric uam_embedding_failures_total \
  --require-metric uam_retrieval_degraded_sources \
  --require-metric uam_retrieval_source_failures_total \
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
