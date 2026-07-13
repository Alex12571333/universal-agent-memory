# Production gap audit addendum — 2026-07-13

This addendum records changes made after
[PRODUCTION_GAP_AUDIT_2026_07_10.md](PRODUCTION_GAP_AUDIT_2026_07_10.md).
It narrows specific findings; it does not claim that the full production gate
is closed.

## Corrected P1 findings

### Desired model settings survive local appliance restarts

The local `docker-compose.yml` now mounts a dedicated named volume at
`/var/lib/obelisk` and configures
`UAM_MODEL_SETTINGS_PATH=/var/lib/obelisk/model-settings.json`. An init service
assigns that directory to the unprivileged API user before the server starts.

Only non-secret desired settings are written. The model-settings writer uses an
atomic mode-`0600` file and deliberately excludes provider API keys. A live
save → restart → readback proof on the local appliance confirmed persistence,
absence of a persisted API key and `0600` ownership. The change is merged in
PR #220.

### Multi-layer hybrid Qdrant retrieval is implemented

The Qdrant adapter writes both dense and sparse vectors and performs hybrid
fusion. When a recall specifies multiple layers it queries every requested
layer with a tenant/workspace filter instead of silently retaining a single
layer. The remaining release work is target traffic evidence, score-quality
evaluation and collection-migration evidence—not implementation of a
single-layer dense-only path.

### Conservative bilingual preference conflicts

Conflict and reflection maintenance now share one deterministic extractor for
English and Russian present-tense preferences, for example:

- `User prefers Qwen.`
- `Пользователь предпочитает Qwen.`

Two different values for the same subject are surfaced as an operator-review
conflict. The extractor refuses temporal-transition language such as
`раньше`, `сейчас`, `formerly`, `now` and `switched`; such statements remain
opaque evidence for the proposal/curation pipeline and cannot be silently
converted into a durable timeless winner. This is deliberately limited
coverage, not a general multilingual NLU claim.

## Remaining gate

No deterministic extractor can safely infer all paraphrases, negations or
temporal relations. Proposal-first curation, evidence quotes, non-temporal
auto-accept policy and operator conflict review remain mandatory. A production
claim still requires preserved target evidence for long-running agents,
recovery, worker reliability, backup retention, monitoring and the other gates
listed in the base audit.
