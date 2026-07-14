# Phase 7 — Adaptive recall and prompt-budget control

Status: implemented and validated locally and on isolated target
`192.168.0.14`. Evidence:
`docs/TARGET_ADAPTIVE_RECALL_VALIDATION_2026_07_14.md`.

## Problem statement

OpenClaw previously called recall before every turn and integrations defaulted
to an 8192-token memory budget. That made a greeting, arithmetic expression or
fully specified request pay retrieval latency and potentially ingest unrelated
history. Ranking also accepted score `0`, so a full budget could be filled by
weak candidates.

## Production contract

1. Automatic recall is `adaptive` by default and uses no LLM.
2. OpenClaw, Hermes and the runtime-neutral core share the same RU/EN fixture
   outcomes.
3. `off` disables automatic recall only; explicit search remains available.
4. `always` and explicit `force_full_recall` select a configurable research
   tier rather than an implicit 8192-token injection.
5. Compact defaults are `top_k=6`, 1200 tokens, 3 records per layer and score
   floor `0.45`.
6. Research defaults are `top_k=10`, 2500 tokens and 6 records per layer.
7. Every injected record is untrusted reference data and cannot close its own
   wrapper delimiter.
8. Decision logs and in-process metrics contain only outcome, bounded reason,
   tier, token counts and latency—never the query.
9. Recall remains fail-soft when the memory server is unavailable.

## Threshold calibration

The current fusion baseline for a fresh active record with default importance
and trust is approximately `0.22`. A dense similarity of `0.70` therefore
scores approximately `0.465` and remains eligible; dense noise at `0.50` scores
approximately `0.395` and is rejected. An exact lexical/entity match scores
approximately `0.57`. RU and EN exact/weak lexical pairs plus dense boundary
cases are executable regressions in `tests/test_api.py` and
`tests/test_memory_plane.py`.

The threshold is configurable with `UAM_RECALL_MINIMUM_SCORE`; lowering it is an
operator choice, not an integration default.

## Verification matrix

| Requirement | Local evidence | Target evidence |
|---|---|---|
| JS/Python gate parity | shared JSON fixture + Node/Pytest | PASS on `.14` |
| no recall for simple turn | mocked OpenClaw + shared-core tests | PASS on `.14` |
| compact request fields | OpenClaw/Hermes/shared assertions | PASS on `.14` |
| full tier override | OpenClaw/shared assertions | PASS on `.14` |
| RU/EN score floor | API calibration regression | PASS locally |
| dense score boundary | retrieval regression | PASS locally |
| untrusted wrapper | integration assertions | PASS on `.14` |
| reason/token/latency metrics | integration metric snapshots | PASS on `.14` |
| fail-soft runtime | existing plugin behavior + regression | PASS on `.14` |

Target validation must use an isolated workspace/thread and synthetic markers;
it must not read or mutate the agents' working memory.
