# DGX Spark embeddings

Recommended local embedding runtime for the memory server:

- host: `192.168.0.10`
- runtime: `qwen3-tts-service/jina_embedding_server.py` wrapper over
  `jina-llama.cpp`
- model: `jinaai/jina-embeddings-v4-text-retrieval-GGUF`
- preferred quantization: `Q8_0`
- fallback quantization: `Q4_K_M`
- embedding dimension: `2048`
- serving API: OpenAI-compatible wrapper `/v1/embeddings`

## Why this model

`jina-embeddings-v4-text-retrieval` is retrieval-oriented, multilingual, and
small enough to keep resident on the DGX Spark GB10 GPU. It is a better fit for
long-term memory search than reusing large chat models, because memory recall
mostly needs stable semantic vectors, not generation.

Use `Q8_0` when VRAM/storage allow it. Use `Q4_K_M` only as the fast fallback.

## Download Q8

```bash
python3 -c 'from huggingface_hub import hf_hub_download; hf_hub_download(
    repo_id="jinaai/jina-embeddings-v4-text-retrieval-GGUF",
    filename="jina-embeddings-v4-text-retrieval-Q8_0.gguf",
    local_dir="/home/alex1257/models/embeddings/jina-v4",
)'
```

## Start OpenAI-compatible embedding wrapper

```bash
cd /home/alex1257/qwen3-tts-service

JINA_MODEL_PATH=/home/alex1257/models/embeddings/jina-v4/jina-embeddings-v4-text-retrieval-Q8_0.gguf \
JINA_MMPROJ_PATH=/home/alex1257/models/embeddings/jina-v4/mmproj-jina-v4-retrieval-BF16.gguf \
JINA_BACKEND_PORT=18002 \
HF_HOME=/home/alex1257/qwen3-tts-service/hf-cache \
LD_LIBRARY_PATH=/home/alex1257/jina-llama.cpp/build/bin:/usr/local/cuda-13.0/lib64 \
PATH=/home/alex1257/qwen3-tts-service/bin:/home/alex1257/qwen3-tts-service/.venv/bin:/usr/local/cuda-13.0/bin:/usr/bin:/bin \
/home/alex1257/qwen3-tts-service/.venv/bin/uvicorn \
  jina_embedding_server:app \
  --host 0.0.0.0 \
  --port 8002 \
  --workers 1
```

The wrapper starts `jina-llama.cpp/build/bin/llama-server` as a private backend
on `127.0.0.1:18002`, requests token-level embeddings from llama.cpp, then
returns pooled and normalized OpenAI-compatible vectors.

Do not point UAM directly at bare `llama-server` for this model: the server can
load the Q8 GGUF, but its public embedding endpoint expects token-level output.
The wrapper is the compatibility layer that returns one vector per input.

## Configure Universal Agent Memory

```text
UAM_EMBEDDING_PROVIDER=tei
UAM_EMBEDDING_BASE_URL=http://192.168.0.10:8002
UAM_EMBEDDING_MODEL=jina-embeddings-v4
UAM_EMBEDDING_DIM=2048
```

## Smoke test

```bash
curl -sS http://192.168.0.10:8002/v1/models

curl -sS http://192.168.0.10:8002/v1/embeddings \
  -H 'content-type: application/json' \
  -d '{"model":"jina-embeddings-v4","input":"память агентов","input_type":"query"}'
```

Expected: one embedding vector with `2048` floats.

Current verified process on `.10`:

- wrapper PID file:
  `/home/alex1257/llama-logs/uam-jina-wrapper-q8-8002.pid`;
- wrapper log:
  `/home/alex1257/llama-logs/uam-jina-wrapper-q8-8002.log`;
- backend llama.cpp port: `127.0.0.1:18002`;
- public embedding port: `0.0.0.0:8002`.
