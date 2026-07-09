# Enterprise readiness report — 2026-07-09

Passed: 31
Failed: 0

| Check | Status | Detail |
|---|---:|---|
| `file:README.md` | PASS | required production artifact |
| `file:SECURITY.md` | PASS | required production artifact |
| `file:.env.production.example` | PASS | required production artifact |
| `file:docker-compose.prod.yml` | PASS | required production artifact |
| `file:.github/workflows/ci.yml` | PASS | required production artifact |
| `file:docs/assets/obelisk-memory-hero.png` | PASS | required production artifact |
| `file:docs/OPERATIONS_RUNBOOK.md` | PASS | required production artifact |
| `file:docs/ENTERPRISE_READINESS.md` | PASS | required production artifact |
| `file:docs/RELEASE_CHECKLIST.md` | PASS | required production artifact |
| `file:docs/DGX_SPARK_MEMORY_LLM.md` | PASS | required production artifact |
| `file:docs/BENCHMARK_RESULTS_2026_07_09.md` | PASS | required production artifact |
| `readme:brand` | PASS | README uses product name |
| `readme:hero` | PASS | README references generated hero asset |
| `readme:production` | PASS | prod path documented |
| `readme:agents` | PASS | agent adapters documented |
| `readme:128k` | PASS | 128k context budget documented |
| `readme:dgx` | PASS | DGX Spark .10 endpoint documented |
| `prod-compose:only-api-published` | PASS | production compose publishes API/UI but not PostgreSQL |
| `prod-compose:internal-qdrant` | PASS | Qdrant is internal in production |
| `prod-compose:nats-health` | PASS | NATS JetStream has monitoring healthcheck |
| `prod-compose:required-api-key` | PASS | production API key is required |
| `ci:ruff` | PASS | CI runs ruff |
| `ci:pytest` | PASS | CI runs pytest |
| `ci:web-build` | PASS | CI builds web UI |
| `ci:prod-compose` | PASS | CI validates production compose |
| `env:memory-llm` | PASS | Qwen memory LLM endpoint |
| `env:embeddings` | PASS | embedding endpoint |
| `env:privacy` | PASS | privacy defaults |
| `env:scoped-keys` | PASS | scoped API keys documented |
| `benchmark:passed` | PASS | latest benchmark pass count |
| `benchmark:no-failures` | PASS | latest benchmark failure count |

## Verdict

Obelisk Memory passes the repository-level enterprise readiness gate.
