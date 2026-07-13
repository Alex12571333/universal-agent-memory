# Target native burst validation — 2026-07-13

## Scope

This redacted validation ran on the real private-LAN agent host `.14` against
the local self-hosted Obelisk appliance. It exercised the installed native
OpenClaw extension through `openclaw agent` and the installed Hermes memory
provider through `uv run hermes -z`. No public domain, VPN, reverse proxy,
agent answer, API key, transcript, or memory text is included here.

## Result

A bounded burst ran six isolated OpenClaw/Hermes pairs with a 30-second gap
between pairs. Each invocation used a new session identifier; both agents ran
in parallel within a pair.

| Check | Result |
| --- | --- |
| OpenClaw native turns | 6/6 exited successfully |
| Hermes native turns | 6/6 exited successfully |
| Non-zero agent exits | 0 |
| Outbox pending after burst | 0 |
| Outbox dead-letter after burst | 0 |
| Outbox lag / in-flight after burst | 0 / 0 |

The status log records only UTC timestamps, round number, agent name and exit
code. It remains in the target's local restricted evidence directory.

The canonical audit trail was also queried read-only after the burst start. It
contained at least six `conversation.turn.append` and six `memory.recall`
events for each native agent, plus at least six OpenClaw `memory.retain`
events. A concurrent longer soak had already started another round, so the
observed aggregate was seven for those actions; this document deliberately
claims only the six events attributable to the completed burst. This confirms
that successful CLI exits were accompanied by real Obelisk lifecycle calls,
rather than a fail-soft no-memory path.

## Boundary

This proves repeated real native lifecycle invocations and a clean server queue
after a short burst. It does not prove multi-hour stability, every model/tool
failure mode, or a remembered result from every turn. A separate twelve-pair,
ten-minute-interval native soak remains in progress and is the longer-duration
evidence gate.
