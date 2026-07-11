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
| Hermes Agent 0.17.0 + Qwen | One-shot answer uses recalled marker | Pass after configuration fix | Hermes returned the exact accepted marker in a fresh native one-shot run. |

## Interpretation

The initial one-shot failure was not a server recall or ACL failure: the same
Hermes key and thread received the accepted marker directly from Obelisk. The
Qwen 3 vLLM endpoint was returning reasoning without a final `content` field
when thinking was enabled. Hermes now uses its supported custom-provider
`extra_body.chat_template_kwargs.enable_thinking=false` setting for this local
endpoint. The adapter also supplies compact ranked records to explicit tool
calls and avoids replaying old episodic transcripts into the automatic
prefetch context.

The native one-shot test was repeated after both changes and returned the
exact synthetic marker. This closes the short functional Hermes consumption
check, not the multi-hour/multi-agent soak gate.

## Safety boundary verified

The Hermes proposal was not recallable before acceptance. It became
recallable only after an operator accepted it, preserving the
proposal-first boundary against an LLM promoting its own generated statement
directly into durable memory.
