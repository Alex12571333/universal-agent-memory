# Self-hosted DGX Spark embeddings

DGX Spark can host an optional OpenAI-compatible embedding endpoint. Obelisk
Memory is not coupled to this hardware or model; the production contract is
`POST /v1/embeddings` with a stable model ID and output dimension.

One suitable profile is a retrieval-oriented multilingual Jina embedding GGUF
served through llama.cpp plus an OpenAI-compatible pooling/normalization
wrapper. Quantization, model path and port are deployment choices.

## Deployment variables

```bash
export EMBEDDING_GATEWAY_URL='https://embeddings.internal.example/v1'
export EMBEDDING_MODEL_ID='jina-embeddings-v4'
export EMBEDDING_DIMENSION=2048
export EMBEDDING_MODEL_PATH='/srv/models/jina-embeddings-v4-Q8_0.gguf'
export EMBEDDING_MMPROJ_PATH='/srv/models/mmproj-jina-v4-retrieval-BF16.gguf'
```

Use Q8 when accelerator memory and storage permit it. Validate the exact
artifact checksum before deployment and record the model/version in release
evidence. A wrapper is required when a bare llama.cpp endpoint returns
token-level embeddings instead of one normalized vector per input.

## Obelisk configuration

Use the generic OpenAI-compatible profile unless the gateway explicitly
requires another adapter:

```dotenv
UAM_EMBEDDING_PROVIDER=openai-compatible
UAM_EMBEDDING_BASE_URL=https://embeddings.internal.example/v1
UAM_EMBEDDING_MODEL=jina-embeddings-v4
UAM_EMBEDDING_DIM=2048
UAM_EMBEDDING_SEND_DIMENSIONS=false
UAM_EMBEDDING_API_KEY_FILE=/run/secrets/embedding_gateway_key
```

The base URL must not include `/embeddings`; the client appends that path.

## Release checks

Probe the endpoint, then run the semantic regression against the same model and
dimension configured in the worker:

```bash
curl -fsS "$EMBEDDING_GATEWAY_URL/models" \
  -H "Authorization: Bearer $EMBEDDING_GATEWAY_KEY"

python scripts/real_embedding_eval.py \
  --provider openai-compatible \
  --base-url "$EMBEDDING_GATEWAY_URL" \
  --model "$EMBEDDING_MODEL_ID" \
  --dimension "$EMBEDDING_DIMENSION" \
  --json-report ./ops/embedding.json
```

Changing model or dimension requires a failure-safe full reindex and a verified
rollback path. Do not mix vectors from different models or dimensions in one
collection.
