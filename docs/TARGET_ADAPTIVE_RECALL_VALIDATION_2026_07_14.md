# Target adaptive-recall validation — 2026-07-14

Target: private LAN agent node `192.168.0.14`.

## Isolation

The implementation archive was extracted under
`/tmp/uam-adaptive-recall-59bb990`. OpenClaw used a new temporary `HOME`; Hermes
used a new temporary `HOME` and `HERMES_HOME`. The probe used an ephemeral HTTP
stub bound to `127.0.0.1` and synthetic UUIDs. It did not read the installed
agents' configuration, credentials, conversations, or working Obelisk memory.
No public endpoint, domain, VPN, or external model was involved.

## Target identity

| Component | Observed |
|---|---|
| Host kernel | Linux 7.0.0-15-generic x86_64 |
| Docker | 29.5.0 |
| Node.js | 24.15.0 |
| Python | 3.14.4 |
| OpenClaw | 2026.6.11 (`e085fa1`) |
| Implementation commit | `59bb990` |
| Reproducible target probe commit | `5e17633` |

## OpenClaw result

The real `openclaw plugins install` and `plugins enable` commands ran inside the
temporary home. `openclaw plugins inspect universal-agent-memory` reported:

- status `loaded`;
- format `openclaw`;
- source `~/.openclaw/extensions/universal-agent-memory/index.js`;
- version `0.1.0`.

The installed copy then ran its Node contract suite: 3 tests passed, 0 failed.
The suite proves shared RU/EN decision parity, zero fetch for a greeting,
compact request fields, untrusted-data framing, text-free decision logs and the
explicit research tier.

## Hermes result

`scripts/adaptive_recall_target_probe.py` ran with the Python interpreter from
the real Hermes installation. The provider's immediate base class resolved to
`agent.memory_provider.MemoryProvider`, proving that the runtime did not use the
repository fallback class.

| Check | Result |
|---|---|
| Greeting skipped recall HTTP | PASS |
| Project query recalled and injected untrusted reference data | PASS |
| Compact fields `6 / 1200 / 3 / 0.45` | PASS |
| Explicit full fields `10 / 2500 / 6` | PASS |
| Metrics contain reasons/counts/tokens/latency, no query | PASS |
| Unreachable recall endpoint is fail-soft | PASS |

The report recorded two successful recall requests and 74 injected estimated
tokens. Its format is `obelisk-adaptive-recall-target-probe-v1`, `ok=true`, and
SHA-256 is
`55f79971085cc20a1a05c019b1f9ede78c27f4dab92d3eeef6b6bf01b063c4cf`.

## Local regression accompanying target evidence

- repository: 462 passed, 58 skipped;
- Python SDK: 6 passed;
- TypeScript SDK: 5 passed;
- OpenClaw plugin: 3 passed;
- changed-file Ruff checks: passed;
- development and production Compose parsing: passed.

The repository-wide Ruff and MyPy commands still report pre-existing baseline
issues in unchanged SDK/type-annotation files. No new failure is in the adaptive
recall change set.

## Boundary

This proves deterministic gate behaviour, real plugin/provider loading,
compact/full request construction, safe injection, local metrics and fail-soft
handling on the target agent host. It does not claim that heuristic recall is
perfect for every future language or that a multi-hour traffic soak has been
performed.
