# Universal Agent Memory benchmark results

Generated: 2026-07-09 20:48:18 KST

Summary: **12 passed**, **0 failed**, **0 skipped**.

| Benchmark | Status | Duration | Details |
|---|---:|---:|---|
| `config_contracts` | PASS | 0.1 ms | Docker/env contract uses 6798 host API port and 128k context. |
| `api_memory_contract` | PASS | 39.5 ms | In-process API retains, recalls and compiles 128k-budget context. |
| `llm_wiring_contract` | PASS | 0.1 ms | Conversation curator and Memory Gateway use LLM reasoner contracts. |
| `in_memory_vector_recall` | PASS | 12.4 ms | In-memory vector pipeline indexes and recalls benchmark memories. |
| `long_context_compiler` | PASS | 0.8 ms | ContextCompiler packs a large synthetic context under the 128k budget. |
| `agent_integration_defaults` | PASS | 0.3 ms | OpenClaw/Hermes/native defaults use port 6798 and 128k context. |
| `web_contract` | PASS | 0.2 ms | Web dashboard keeps Russian UI, graph controls, settings and build script. |
| `web_build` | PASS | 1129.6 ms | React/Vite dashboard builds successfully. |
| `docker_compose_state` | PASS | 149.6 ms | Docker compose daemon is reachable and stack state was inspected. |
| `live_http_api` | PASS | 183.1 ms | Live API health and recall responded. |
| `live_memory_llm` | PASS | 148.7 ms | Live Qwen/Spark chat-completions returned final content. |
| `live_embeddings` | PASS | 440.5 ms | Live embedding endpoint ranks Qdrant semantic recall first. |

## Metrics

### config_contracts

- status: `PASS`
- memory_port: `True`
- postgres_port: `True`
- qdrant_ports: `True`
- minio_ports: `True`
- nats_ports: `True`
- context_budget_env: `True`
- context_per_layer_limit_env: `True`
- llm_context_env: `True`
- llm_thinking_disabled: `True`
- llm_context_window_tokens: `131072`
- llm_model: `qwen3.6-35b-a3b`
- llm_enable_thinking: `False`

### api_memory_contract

- status: `PASS`
- context_budget_tokens: `131072`
- result_count: `1`
- used_tokens: `13`

### llm_wiring_contract

- status: `PASS`
- curator_engine: `memory_llm`
- proposal_target: `preference`
- proposal_confidence: `0.9`

### in_memory_vector_recall

- status: `PASS`
- indexed_items: `50`
- top_k_returned: `8`
- sources: `sql_lexical,qdrant_hybrid`
- top_score: `0.7909`

### long_context_compiler

- status: `PASS`
- budget_tokens: `131072`
- used_tokens: `97920`
- items_included: `120`
- rendered_chars: `391931`

### agent_integration_defaults

- status: `PASS`
- shared_url_6798: `True`
- shared_budget_128k: `True`
- hermes_url_6798: `True`
- hermes_budget_128k: `True`
- openclaw_url_6798: `True`
- openclaw_budget_128k: `True`
- docs_budget_128k: `True`

### web_contract

- status: `PASS`
- russian_dashboard: `True`
- graph_expand_button: `True`
- settings_panel: `True`
- responsive_css: `True`
- build_script: `True`

### web_build

- status: `PASS`
- dist_index_bytes: `461`

### docker_compose_state

- status: `PASS`
- services_total: `7`
- services_running: `7`

### live_http_api

- status: `PASS`
- base_url: `http://127.0.0.1:6798`
- retained_id: `970d514d-2534-4c7a-a9fb-bdb1851e43c3`
- context_budget_tokens: `131072`
- result_count: `5`

### live_memory_llm

- status: `PASS`
- base_url: `http://192.168.0.10:8000/v1`
- model: `qwen3.6-35b-a3b`
- response_chars: `6`
- contains_memory_word: `True`

### live_embeddings

- status: `PASS`
- base_url: `http://192.168.0.10:8002`
- model: `jina-embeddings-v4`
- dimension: `2048`
- top: `qdrant`
- top_score: `0.5897`
