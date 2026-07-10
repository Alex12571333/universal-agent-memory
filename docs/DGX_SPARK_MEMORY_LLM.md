# DGX Spark memory LLM alternative

This document describes an optional self-hosted OpenAI-compatible memory LLM
backend. The production contract is provider-neutral:
`UAM_MEMORY_LLM_BASE_URL` must expose `/v1/chat/completions`. OpenAI, OpenRouter,
LiteLLM, vLLM, llama.cpp, and this DGX Spark gateway can all fit that contract.

Obelisk Memory uses two different model runtimes:

- **Embeddings** — Jina embeddings v4 Q8 on DGX Spark `.10`, documented in
  [DGX_SPARK_EMBEDDINGS.md](DGX_SPARK_EMBEDDINGS.md).
- **Memory LLM** — optional Qwen on DGX Spark `.10`, usable by future
  Навигатор памяти and Куратор памяти reasoning tasks.

The memory LLM must not point at the embedding endpoint. It should use an
OpenAI-compatible chat/completions endpoint backed by Qwen.

## Optional DGX environment

```text
UAM_MEMORY_LLM_PROVIDER=spark
UAM_MEMORY_LLM_MODEL=qwen3.6-35b-a3b
UAM_MEMORY_LLM_BASE_URL=http://192.168.0.10:8000/v1
UAM_MEMORY_LLM_API_KEY=
UAM_MEMORY_LLM_TIMEOUT_SECONDS=120
UAM_MEMORY_LLM_TEMPERATURE=0.1
UAM_MEMORY_LLM_CONTEXT_TOKENS=131072
UAM_MEMORY_LLM_MAX_TOKENS=1600
UAM_MEMORY_LLM_ENABLE_THINKING=false
```

If the Spark gateway on `.10` uses another port, keep the model/provider and
override only `UAM_MEMORY_LLM_BASE_URL`.

The server adapter appends `/chat/completions` to `UAM_MEMORY_LLM_BASE_URL`.
With the DGX example above, the exact request URL is:

```text
http://192.168.0.10:8000/v1/chat/completions
```

Requests use the OpenAI-compatible shape:

```json
{
  "model": "qwen3.6-35b-a3b",
  "messages": [{"role": "user", "content": "..."}],
  "temperature": 0.1,
  "max_tokens": 1600,
  "chat_template_kwargs": {"enable_thinking": false}
}
```

If `UAM_MEMORY_LLM_API_KEY` or `SPARK_API_KEY` is set, the adapter sends
`Authorization: Bearer <key>`.

`UAM_MEMORY_LLM_CONTEXT_TOKENS=131072` records the production context window
expected from the `.10` Qwen gateway. It is not sent as OpenAI payload by
default; it is exposed through config/status so planners and future benchmark
checks can budget long-context memory jobs correctly.

For Qwen3.6 on the current vLLM gateway, memory workers disable thinking by
default with `chat_template_kwargs.enable_thinking=false`. Without this, short
maintenance calls may spend the whole `max_tokens` budget on reasoning and
return `content=null`.

## Intended use

This LLM is for memory maintenance, not ordinary agent chat:

- grounded recall planning by Навигатор памяти;
- proposal classification and curation by Куратор памяти;
- compact summaries from raw conversation ledger;
- graph relation candidate extraction;
- memory quality reports.

The default temperature is intentionally low (`0.1`) because memory maintenance
should be conservative and evidence-bound.

The current runtime client is `MemoryLLMClient` in
`src/memory_plane/adapters/llm.py`:

- `chat(messages)` returns plain assistant text;
- `chat_json(messages)` requests `response_format={"type":"json_object"}`,
  accepts fenced JSON, and rejects non-object output;
- endpoint/protocol failures are normalized as `MemoryLLMError`, so workers can
  fail soft without breaking the user-facing agent flow.

## Live regression gate

If using this self-hosted backend for a release, run the memory LLM regression:

```bash
python scripts/real_memory_llm_eval.py \
  --base-url http://192.168.0.10:8000/v1 \
  --model qwen3.6-35b-a3b \
  --json-report ./ops/memory-llm.json
```

The report format is `obelisk-memory-llm-eval-v1`. It verifies normal chat
completion, JSON-object curation, and that the model preserves the current
Jina/Q8 embedding instruction instead of the obsolete fake-embedding claim.

## Live endpoint note

On 2026-07-09 the `.10` gateway advertised:

```text
qwen3.6-35b-a3b
nvidia/Qwen3.6-35B-A3B-NVFP4
```

Qwen3.6 may emit reasoning tokens before final `content` when thinking is
enabled. Tiny health checks such as `max_tokens=8` or `max_tokens=64` can
therefore finish with `content=null` even though the endpoint is healthy. Use
`UAM_MEMORY_LLM_ENABLE_THINKING=false` for memory workers and keep the
configured `UAM_MEMORY_LLM_MAX_TOKENS=1600`.

## Separation from embeddings

Do not reuse:

```text
http://192.168.0.10:8002/v1/embeddings
```

for memory LLM calls. That endpoint is the Jina embedding wrapper and returns
vectors, not chat completions.
