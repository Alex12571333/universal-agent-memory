# Target agent revalidation — 2026-07-12

Target: the private LAN agent node `192.168.0.14`. Obelisk is the local Docker
server at `192.168.0.39:6798`; no public domain, VPN, or reverse proxy is in
the path. This is follow-up evidence for the image built from
`f2909d728b80528f93e788381fbe329449618e71`.

## Scope and result

All markers below were synthetic. No API key, transcript, or user memory text
is recorded here.

| Runtime | Native path exercised | Result | Evidence |
| --- | --- | --- | --- |
| OpenClaw 2026.6.11 | Gateway `universal-agent-memory` lifecycle hook | Pass | A unique marker in a real `openclaw agent` run was subsequently returned by `/v1/memory/recall` using the same bound OpenClaw identity and configured thread. |
| Hermes Agent 0.17.0 | `universal_agent_memory_add` | Pass | A real `hermes -z` run created an open proposal through the native provider. |
| Hermes Agent 0.17.0 | Proposal acceptance boundary | Pass | Operator acceptance created canonical immutable memory; the same bound Hermes key received the marker from direct Obelisk recall. |
| Hermes Agent 0.17.0 + Qwen | One-shot answer uses recalled marker | **Fail / gate remains open** | After acceptance, Hermes/Qwen answered that the marker was absent even though the provider's exact direct recall returned it. |

## Interpretation

The last row is deliberately not counted as a server recall or ACL failure:
the identical Hermes agent identity, thread, query, and key received the
accepted marker from Obelisk. It is an agent-runtime consumption failure: the
Qwen one-shot execution did not reliably use the provider context/tool result
in its final answer.

Consequently, the Hermes integration must not be marked end-to-end production
ready merely because the provider is installed or its HTTP contract passes.
The required follow-up gate is a deterministic native-runtime test which
captures provider-tool output and verifies that the final Hermes answer uses
the recalled synthetic fact. The test must pass repeatedly with the deployed
model configuration before the integration can be treated as release-ready.

## Safety boundary verified

The Hermes proposal was not recallable before acceptance. It became
recallable only after an operator accepted it, preserving the
proposal-first boundary against an LLM promoting its own generated statement
directly into durable memory.
