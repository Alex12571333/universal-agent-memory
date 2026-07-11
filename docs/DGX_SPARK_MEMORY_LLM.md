# Self-hosted DGX Spark memory LLM

DGX Spark can host an optional memory-reasoning model behind the same
provider-neutral OpenAI-compatible contract used by hosted or gateway
providers. Obelisk Memory requires `POST /v1/chat/completions`; it does not
require Qwen, a specific accelerator, or a fixed local address.

The memory LLM is separate from embeddings. It supports conservative curation,
proposal classification, compact conversation summaries and future grounded
graph extraction. Agent chat models remain configured in their own runtimes.

## Deployment variables

```bash
export MEMORY_LLM_GATEWAY_URL='https://memory-llm.internal.example/v1'
export MEMORY_LLM_MODEL_ID='<served-model-id>'
```

Configure Obelisk Memory:

```dotenv
UAM_MEMORY_LLM_PROVIDER=openai-compatible
UAM_MEMORY_LLM_MODEL=<served-model-id>
UAM_MEMORY_LLM_BASE_URL=https://memory-llm.internal.example/v1
UAM_MEMORY_LLM_API_KEY_FILE=/run/secrets/memory_llm_gateway_key
UAM_MEMORY_LLM_TIMEOUT_SECONDS=120
UAM_MEMORY_LLM_TEMPERATURE=0.1
UAM_MEMORY_LLM_CONTEXT_TOKENS=32768
UAM_MEMORY_LLM_MAX_TOKENS=1600
```

The adapter appends `/chat/completions` to the configured base URL. The generic
profile sends the standard fields `model`, `messages`, `temperature`,
`max_tokens` and optional `response_format`. Provider-specific payload fields
belong behind a compatible gateway or an explicit adapter capability; they must
not leak into the generic contract.

## Runtime behavior

`MemoryLLMClient` provides:

- `chat(messages)` for plain assistant text;
- `chat_json(messages)` for a JSON object used by deterministic workers;
- normalized `MemoryLLMError` failures so the caller can apply a reviewed
  fail-soft fallback.

Memory output is not authoritative by itself. Durable facts require evidence,
provenance and the proposal/conflict policy described in the architecture.

## Release gate

Run the live regression against the exact endpoint/model used by the release:

```bash
python scripts/real_memory_llm_eval.py \
  --base-url "$MEMORY_LLM_GATEWAY_URL" \
  --model "$MEMORY_LLM_MODEL_ID" \
  --json-report ./ops/memory-llm.json
```

Seal `ops/memory-llm.json` into the signed release evidence bundle. A passing
endpoint probe does not replace manual review of proposal quality, multilingual
behavior, temporal conflicts and prompt-injection resistance.

Never point `UAM_MEMORY_LLM_BASE_URL` at an embedding endpoint: embeddings
return vectors, while the memory LLM must return chat-completion content.
