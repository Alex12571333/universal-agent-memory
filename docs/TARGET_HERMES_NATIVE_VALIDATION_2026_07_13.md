# Target Hermes native validation — 2026-07-13

## Scope

This redacted validation ran on the real local Hermes host `.14` against the
same local Obelisk appliance. No public endpoint, domain, VPN, or proxy was
introduced.

## Repair and result

Hermes' checked-in `uv.lock` already required `python-dotenv`, but its runtime
environment was incomplete and the CLI failed before plugin discovery.
Running `uv sync --locked` inside Hermes' own project recreated its isolated
`.venv` from the pinned lockfile; it did not alter the system Python.

Then a real `uv run hermes -z` one-shot was executed with the installed
`universal_agent_memory` provider and the target's existing scoped local
configuration. The command returned the expected sentinel response.

| Check | Result |
|---|---|
| Hermes CLI imports and renders help | PASS |
| Provider configuration remains installed | PASS |
| Native Hermes one-shot completes | PASS |
| Obelisk raw/curated lifecycle remains fail-soft | PASS by provider design |

## Bounded repeat smoke

Three additional sequential native `hermes -z` turns were run through the same
provider and target configuration. All three returned their exact independent
sentinel responses (`3/3`). This catches immediate lifecycle/environment drift
after the repaired environment; it is retained as a bounded smoke result, not
misrepresented as a multi-hour soak.

## Boundary

This proves one real native Hermes lifecycle invocation after dependency repair.
It is not multi-hour soak evidence and does not prove every tool/model failure
mode. The durable soak gate remains open until repeated native OpenClaw and
Hermes traffic is observed with retention, recall, isolation, and worker-health
evidence.
