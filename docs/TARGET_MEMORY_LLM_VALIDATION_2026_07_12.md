# Target memory-LLM validation — 2026-07-12

The local Qwen server on the private LAN node `.10` was validated through its
OpenAI-compatible endpoint before being configured for the Obelisk memory
pipeline. No prompt transcript, chain-of-thought, API key, or curated proposal
is recorded here.

## Endpoint and safety configuration

- Endpoint: `http://192.168.0.10:8000/v1`
- Model: `nvidia/Qwen3.6-35B-A3B-NVFP4`
- Memory context window: 8192 tokens
- Temperature: 0
- Provider request option: `chat_template_kwargs.enable_thinking=false`

The last option is required for this Qwen/vLLM deployment. Without it the
server can spend the completion budget on reasoning and return `content: null`.
With it, only the final answer is returned to the memory client. Obelisk never
uses a provider reasoning field as proposal text.

## Live result

`obelisk-memory-llm-eval-v1` passed on 2026-07-11T15:32:17Z:

- chat completion returned a non-empty final answer;
- JSON curation selected the newer `365 days` retention fact and rejected the
  superseded `30 days` value.

The Docker memory server was then restarted with that exact non-secret
configuration. Its operator status confirms the model, local endpoint, 8192
token context, zero temperature and configured provider extra body; `/ready`
remained healthy.

This validates the provider boundary only. Generated text remains a proposal
with evidence and cannot become recallable durable memory until an operator
accepts it.
