# Target validation — 2026-07-11

Target: the local LAN agent node `192.168.0.14`. The Obelisk server is the
local self-hosted Docker deployment at `192.168.0.39:6798`; no public domain,
VPN, or reverse proxy is involved.

## What was verified

| Integration | Check | Result |
| --- | --- | --- |
| Obelisk server | `GET /ready` from `.14` | Ready; canonical PostgreSQL, lexical retrieval, and Qdrant hybrid retrieval healthy. |
| OpenClaw | Native `universal-agent-memory` extension | Enabled and loaded by the gateway. |
| OpenClaw | Write → recall in a fresh session | Passed. A marker written in one isolated run was returned in a different session. |
| Hermes | Native `universal_agent_memory` provider | Installed, active, and available. |
| Hermes | Write → recall in a fresh session | Passed. A marker written in one isolated run was returned in a different session. |

The markers used in this validation are synthetic and are not product data.
Secrets, memory contents, and agent transcripts were not recorded in this
document.

## Issue found and corrected

The pre-existing OpenClaw environment file on `.14` contained an empty
`UAM_API_KEY`. Because the extension is fail-soft, agent turns still completed,
but retention and recall requests were unauthorized. The deployment now keeps
the configured key in a mode-`0600` environment file and loads it through a
systemd user-service drop-in. The OpenClaw gateway was restarted and the
end-to-end test was repeated successfully.

Both deployed adapters were refreshed from this repository. Their context
budget is configured as `8192` tokens, rather than injecting a 128k context.
Long source documents are chunked before embedding by the server; agent
recall receives only the bounded, selected context.

## Remaining production gates

This is a functional target validation, not a release certificate. The
remaining operations gates are: a scheduled retention job, a target-side
restore-and-semantic-reindex drill with retained evidence, alert delivery
verification, and a multi-hour/multi-agent soak run.
