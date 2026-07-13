# Target OpenClaw contract validation — 2026-07-13

## Scope

This is a redacted, point-in-time validation from the real OpenClaw node at
`.14` to the local self-hosted Obelisk appliance at `192.168.0.39:6798`.
Neither a public domain nor a VPN was introduced.

The check read the credential only from the environment of the already-running
OpenClaw gateway. It did not print, copy, or persist that credential. Its
short SHA-256 fingerprint matched the locally configured `openclaw` principal.

## Deployment identity

| Field | Value |
|---|---|
| Source commit | `fcc292d152de04dff10c4e5f830cbeba226cbc9d` |
| Image digest | `sha256:22216069e6dfa84b1b80aaae49e8a8bbf5a5945c5447fde40218d552528e145a` |
| Deployment | `local-lan-20260713-recovery` |
| API readiness | `ready` |
| Canonical store | `healthy` |
| Retrieval sources | `postgres_lexical`, `qdrant_hybrid` healthy |

## Result

The installed `universal-agent-memory` OpenClaw extension was enabled and
bound to the configured tenant, workspace and agent identity. A synthetic,
scoped lifecycle marker was retained using the same endpoint and gateway
credential resolution path as the extension, then recalled in the same scope.

| Check | Result |
|---|---|
| `.14` can reach local appliance `/ready` | PASS |
| Gateway credential matches local scoped registry | PASS (fingerprint only) |
| Scoped retain accepted | PASS |
| Scoped recall returned the retained item | PASS |
| Hybrid retrieval source used | PASS (`qdrant_hybrid`) |
| Workspace index fresh | PASS (`index_stale=false`) |

## Boundary of this evidence

This proves the live OpenClaw integration contract, not a multi-hour native
gateway soak. It does not substitute for Hermes validation, long-running
traffic retention evidence, alert-route evidence, or a signed release bundle.
