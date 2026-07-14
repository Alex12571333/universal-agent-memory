# Каталог публичных функций

Это карта ответственности, побочных эффектов и контрактов. Приватные helpers с
префиксом `_` документируются docstring рядом с кодом.

## Domain — `domain/models.py`

| Функция | Назначение | Побочные эффекты / ошибки |
|---|---|---|
| `MemoryItem.__post_init__()` | Проверяет текст, score ranges, validity и thread scope | `ValueError` при нарушении инварианта |
| `MemoryItem.is_valid_at(moment)` | Point-in-time проверка temporal validity | Нет |
| `MemoryItem.supersede(text, confidence=...)` | Создаёт новый immutable revision с `supersedes_id` | Генерирует UUID/time; старый item не меняет |
| `MemoryStatus` | Lifecycle state for recall/review | rejected/archived hidden, disputed/stale demoted |
| `MemoryRevisionConflictError` | Ошибка stale CAS для MemoryItem | Содержит `expected`, `actual` |
| `Observation.__post_init__()` | Запрещает belief без summary/evidence | `ValueError` |
| `ContextPackage.render_markdown()` | Детерминированно рендерит sections для LLM | Нет |

## Audit — `domain/audit.py`, `services/audit.py`

| Функция | Назначение | Гарантия |
|---|---|---|
| `AuditEvent.__post_init__()` | Проверяет actor/action/resource/status | `ValueError` при бесполезной audit-записи |
| `AuditLogService.record(...)` | API/workers → append-only audit event | Не мутирует domain objects |
| `AuditLogService.list_events(...)` | Operator export с фильтрами | Ограничивает `limit` диапазоном `1..500` |
| `scripts/export_audit.py` | Recent/ranged audit events → JSONL forensic bundle | Пишет/проверяет `audit-events.jsonl`, `manifest.json`, `manifest.sha256`, `manifest.sig` |

## API keys — `domain/api_key.py`, `services/api_keys.py`

| Функция | Назначение | Гарантия |
|---|---|---|
| `ApiKeyRecord.__post_init__()` | Валидирует name/fingerprint/scopes | Secret не хранится |
| `ApiKeyRegistryService.ensure_configured_key(...)` | Env key → registry metadata | Создаёт/обновляет fingerprint без bearer secret |
| `ApiKeyRegistryService.touch(...)` | Successful auth → `last_used_at` | Не меняет scopes/revocation |
| `ApiKeyRegistryService.revoke(...)` | Operator revocation | Сохраняет row для audit/forensics |
| `PrincipalBinding` / `_parse_principal_bindings()` | Principal name → tenant/workspace/agent UUIDs | Strict startup rejects every unbound agent principal |
| `_required_scope_for_request()` | HTTP path/method → minimum capability | Control-plane and review routes are operator-only unless explicitly agent-safe |
| `_agent_binding_error()` | Authenticated request + principal + identity registry → allow/deny | Agent cannot forge scope IDs or use a foreign thread |
| `thread_belongs_to_agent()` | Tenant/workspace/agent/thread → boolean | Implemented by in-memory and PostgreSQL identity registries |

## Contracts — `contracts/dto.py`, `contracts/events.py`

| Функция | Назначение | Ограничение |
|---|---|---|
| `RecallQuery.__post_init__()` | Валидирует query и `top_k` | `1..1000` |
| `ContextRecipe.__post_init__()` | Валидирует token budget | минимум 128 |
| `IntegrationEvent.__post_init__()` | Требует версию в имени события | пример `memory.retained.v1` |

## Retention — `services/retention.py`

| Функция | Вход → выход | Гарантия |
|---|---|---|
| `RetentionService.__init__(store)` | Atomic retention port → service | Не открывает соединения |
| `RetentionService.retain(command)` | `RetainCommand` → `RetainResult` | Append-only; memory и outbox фиксируются одной транзакцией |
| `RetentionService.supersede(command)` | `SupersedeMemoryCommand` → `RetainResult` | CAS append; stale revision → `MemoryRevisionConflictError` |

## Privacy — `services/privacy.py`

| Функция | Вход → выход | Гарантия |
|---|---|---|
| `PrivacyGuard.from_env()` | `UAM_PRIVACY_*` env → guard | Default action is `redact` |
| `PrivacyGuard.scan(text)` | Text → findings | Deterministic non-overlapping detector hits |
| `PrivacyGuard.apply(text)` | Text → sanitized decision | Redact/reject/metadata-only/allow policy |
| `_luhn_valid(raw)` | Candidate card string → bool | Reduces payment-card false positives |

## Ingestion — `services/ingestion.py`

| Функция | Вход → выход | Гарантия |
|---|---|---|
| `IngestDocumentCommand.__post_init__()` | Parameters → validated command | Проверяет текст, размер и overlap |
| `TextChunker.split(text, size, overlap)` | Text → stable `(start,end,chunk)` | Paragraph/sentence-aware, deterministic |
| `IngestionService.__init__(retention, chunker=None)` | Retain seam → service | Parser можно заменить независимо |
| `IngestionService.ingest_text(command)` | Document → `IngestResult` | SHA-256 provenance и idempotency на каждый chunk |
| `MarkdownParser.parse(data)` | Markdown bytes → readable text | Не исполняет HTML/code |
| `PdfParser.parse_pages(data)` | PDF bytes → page texts | Optional pypdf; rejects image-only PDF |
| `DocumentIngestor.ingest_markdown()` | Binary source → memory chunks | Binary checksum и stable origin |
| `DocumentIngestor.ingest_pdf()` | PDF pages → memory chunks | `#page=N` provenance, общий checksum |

## Retrieval — `services/retrieval.py`

| Функция | Вход → выход | Гарантия |
|---|---|---|
| `RetrievalService.__init__(sources, weights)` | Sources/weights → service | Требует source; веса = 1.0 |
| `RetrievalService.recall(query)` | `RecallQuery` → ranked `RecallResult` | Tenant/workspace/validity filters применяются после каждого adapter |
| `RetrievalService._status_multiplier(status)` | status → score multiplier | Demotes uncertain states, boosts pinned core memory |

## Context — `services/context.py`

| Функция | Вход → выход | Гарантия |
|---|---|---|
| `ContextCompiler.compile(recall, recipe)` | Ranked recall → `ContextPackage` | Не превышает budget; core/working имеют приоритет |
| `ContextCompiler.estimate_tokens(text)` | text → integer | Portable heuristic `ceil(chars/4)` |

## Reflection — `services/reflection.py`

| Функция | Вход → выход | Гарантия |
|---|---|---|
| `ReflectionService.__init__(ledger, observations)` | Ports → service | Нет I/O до вызова |
| `ReflectionService.reflect(tenant, workspace)` | scope → observations | Raw evidence не меняется; repeated/conflicting slots → observations |
| `_extract_slot(text)` | Text → subject/predicate/value | Deterministic fixtures для `X is Y`, `A owns B`, `X releases on D` |
| `_confidence(rows, conflict=...)` | Evidence → score | Повторы усиливают, конфликтующие значения штрафуются |

## Conflicts — `domain/conflict.py`, `services/conflicts.py`

| Функция | Вход → выход | Гарантия |
|---|---|---|
| `ConflictCase.review_status` | Case → status | Defaults to `unresolved` without persisted review |
| `ConflictService.list_cases()` | Tenant/workspace → conflict inbox | Deterministic grouping from append-only semantic evidence |
| `ConflictService.decide()` | Case decision → persisted review | Requires `winner_value` for accepted/overridden decisions |
| `_extract_slot(text)` | Memory text → subject/predicate/value | Conservative deterministic patterns matching reflection v2 |
| `_candidate_confidence(rows, is_active=...)` | Evidence rows → score | Repeated evidence and active newest value get bounded boost |

## Graph — `domain/graph.py`, `services/graph.py`

| Функция | Вход → выход | Гарантия |
|---|---|---|
| `MemoryEdge.__post_init__()` | Edge fields → validated edge | Rejects self-loop, bad weight and invalid validity range |
| `GraphService.link()` | Endpoint IDs + edge type → persisted edge | Verifies both memories exist in the same workspace |
| `GraphService.neighbors()` | Memory ID → incoming/outgoing edges | Optional `edge_type` filter |

## In-memory adapter — `adapters/in_memory.py`

| Функция | Назначение |
|---|---|
| `InMemoryMemoryStore.append()` | Thread-safe append и idempotency |
| `supersede_if_current()` | Thread-safe CAS supersede и outbox event |
| `get()` | Tenant-safe lookup |
| `list_for_workspace()` | Канонический fallback/listing |
| `search()` | Dependency-free lexical retrieval + metadata filters |
| `publish()` | In-memory idempotent outbox |
| `collect_metrics()` | Local counters for tests/dev `/metrics` |
| `save()` / `list_observations()` | Derived observation storage |
| `InMemoryObservationRepository.*` | Адаптирует observation port без конфликта имён |
| `InMemoryConflictReviewRepository.*` | Human conflict-review decisions | Replaces decision by `(tenant_id, case_id)` |
| `InMemoryGraphRepository.*` | Memory graph edges | In/out neighbor lookup by memory ID |

## Workers — `workers/handlers.py`

| Функция | Назначение | Семантика |
|---|---|---|
| `RetainedEventRouter.__init__(handlers)` | Регистрирует handlers по job name | Handler можно тестировать отдельно |
| `RetainedEventRouter.handle(event)` | Dispatch jobs из `memory.retained.v1` | Неизвестные event/job пропускаются |

## Outbox — `services/outbox.py`, `services/consumer.py`

| Функция | Назначение | Семантика |
|---|---|---|
| `OutboxRelay.run_once()` | Lease → JetStream publish → ack/release | At-least-once, bounded batch |
| `IdempotentEventConsumer.handle()` | Защищает handler от completed/concurrent duplicates | Failed handler освобождает lease |
| `PostgresMemoryLedger.claim_outbox()` | Конкурентно выдаёт due events | `FOR UPDATE SKIP LOCKED` |
| `mark_outbox_published()` | Подтверждает событие | Только текущий lease owner |
| `release_outbox()` | Retry или dead-letter | Порог по attempts |
| `claim_event_processing()` | Consumer dedupe lease | acquired/completed/busy |
| `NatsJetStreamSink.send()` | Публикует versioned event | Ждёт server ack, `Nats-Msg-Id=event.id` |
| `NatsPullWorker.run_once()` | Pull → decode → handler → ack/nak | Busy/error delivery не подтверждается |
| `migrate(dsn, app_user, app_password)` | Применяет forward-only SQL migrations и runtime-role grants | Advisory lock; повторный запуск и password rotation безопасны |
| `read_database_dsn(...)` | URL/`*_URL_FILE` или DB-компоненты + password file → escaped DSN | Не требует secret interpolation в Compose |
| `filter_recallable_heads(tenant_id, ids)` | Batch canonical head validation | Отсекает superseded/rejected/archived до vector fusion |
| `ConflictReviewRepository.apply_resolution(...)` | Winner/loser revisions + events + review | Atomic multi-root CAS; immutable idempotent decision |

## Metrics/ops — `services/metrics.py`, `scripts/backup.py`, `scripts/restore.py`

| Функция | Назначение | Семантика |
|---|---|---|
| `render_prometheus(metrics)` | Numeric mapping → Prometheus text | Stable sort, `uam_` prefix |
| `backup.py` | Запускает `pg_dump --format=custom` | URL из `UAM_BACKUP_DATABASE_URL`/admin/database env |
| `restore.py` | Запускает `pg_restore` | Расшифровывает `.dump.enc` в защищённый временный файл; `--clean` opt-in, production-restore проверяет signed backup bundle |
| `check_branch_protection.py` | Проверяет GitHub release gate для `main` | Требует PR, status checks, strict mode и admin enforcement |
| `check_metrics_health.py` | `/metrics`/file/stdin → JSON health gate | Fail по outbox lag/dead-letter; webhook alert |
| `deployment_preflight.py` | Public/backend URLs → JSON deployment-boundary gate | Requires HTTPS public health/security headers and blocked direct backend |
| `observability_preflight.py` | Dashboard/alert artifacts → JSON observability gate | Requires Grafana and Prometheus coverage for production metrics |
| `ops_schedule_preflight.py` | Schedule files/artifact roots/env → JSON ops gate | Requires backup/audit/metrics schedules, alert routes and durable artifact roots |
| `secret_files_preflight.py` | `.env.production` → JSON secret-manager gate | Requires raw secret env values empty and `*_FILE` paths readable under allowed prefix |
| `validate_production_env.py` | `.env.production` → deployment gate | Rejects placeholders, weak secrets, local TLS defaults, fake embeddings |
| `generate_release_evidence_manifest.py` | Reports + commit/image/deployment identity → signed `release-evidence.json` v2 | SHA-256 per artifact, safe relative paths, HMAC-SHA256 manifest signature |
| `verify_release_evidence.py` | Signed release bundle → pass/fail | Verifies identity, freshness, signature, paths, hashes and report semantics |
| `generate_release_notes.py` | Git refs → release changelog and rollback JSON evidence | Records previous/current commits plus restore/redeploy rollback steps |
| `scheduled_backup.py` | Backup → AES-256-GCM → restore drill → audit export → JSON report | Ключ только из secret env/file; webhook alert при fail; подходит для cron/systemd |
| `audit_retention.py` | Audit export → verify → optional prune → JSON report | Dry-run by default; `--apply` requires signed export |
| `agent_soak_eval.py` | Live OpenClaw/Hermes soak gate → JSON report | Retain/recall/idempotency/leakage checks against a running server |
| `conversation_pipeline_eval.py` | Live raw transcript → curation → recall gate | Verifies raw turns do not leak into recall before explicit curation |
| `load_smoke_eval.py` | Concurrent retain/recall load smoke → JSON report | p95 latency, error-rate and backlog gate |
| `real_embedding_eval.py` | Live OpenAI-compatible embedding gate → JSON report | Dimension check plus semantic recall scenarios; does not decide fact freshness |
| `real_memory_flow_eval.py` | Live end-to-end embedding memory check | Proves retain → CAS supersede → index → recall returns only the active head |
| `real_memory_llm_eval.py` | Live OpenAI-compatible memory LLM gate → JSON report | Chat completion + JSON curation regression |

## Vault — `services/vault.py`, `scripts/export_vault.py`, `scripts/import_vault.py`

| Функция | Назначение | Семантика |
|---|---|---|
| `VaultExporter.export()` | Workspace → in-memory Markdown vault snapshot | Stable file names; deterministic file ordering |
| `VaultExporter.export_workspace()` | Workspace → folder on disk | Safe relative paths; memory/observation counts |
| `VaultExporter.plan_import()` | Markdown vault files → safe import plan | Dry-run; detects changed/unchanged/conflict/error files |
| `VaultExporter.apply_import()` | Markdown vault files → CAS supersede writes | Creates new revisions only; never overwrites rows |
| `VaultExporter._memory_file()` | `MemoryItem` → Obsidian note | Frontmatter + provenance + supersede backlinks |
| `VaultExporter._observation_file()` | `Observation` → reflection note | Evidence backlinks to `mem-*` notes |
| `vault_manifest.py` | Markdown vault folder → manifest/checksum/signature verification | SHA-256 per file plus optional HMAC signature |
| `export_vault.py` | PostgreSQL workspace → folder | Deterministic materialized export; writes manifest/checksum/signature |
| `import_vault.py` | Folder → dry-run/apply import | Dry-run by default; can require manifest/signature before writes and emit `obelisk-vault-import-report-v1` evidence |

## Native integrations — `agent-integrations/`

| Файл | Назначение | Семантика |
|---|---|---|
| `shared/lifecycle.py` | Runtime-agnostic plugin contract | before-run, after-event, run-complete hooks |
| `shared/config.py` | Env/config for native plugins | Reads `UAM_*` runtime flags |
| `shared/identity.py` | Stable UUID resolution | Explicit env UUID or deterministic UUIDv5 fallback |
| `shared/client.py` | Stdlib HTTP client for plugin runtimes | Bearer auth, retain/recall/checkpoint/reflect |
| `shared/plugin.py` | Runtime-neutral memory plugin core | Maps lifecycle events to retain/recall calls |
| `openclaw/plugin/index.js` | Installable OpenClaw native plugin | `agent_turn_prepare`, `after_tool_call`, `agent_end` hooks |
| `openclaw/plugin/package.json` | OpenClaw extension metadata | `openclaw.extensions: ["./index.js"]` |
| `hermes/universal_agent_memory/__init__.py` | Hermes `MemoryProvider` | `prefetch`, `sync_turn`, `on_session_end`, explicit tools |
| `hermes/universal_agent_memory/plugin.yaml` | Hermes provider metadata | User-installed memory provider manifest |
| `scripts/agent_soak_eval.py` | Runtime evidence for OpenClaw/Hermes contract | Parallel agent markers plus cross-workspace leakage probes |

## Composition/API

| Функция | Назначение |
|---|---|
| `build_in_memory_container()` | Собирает полностью рабочий local/test graph |
| `build_postgres_container(...)` | Собирает durable standalone server graph |
| `create_app(container=None)` | Создаёт FastAPI app; позволяет dependency injection |
| `GET /health` | Liveness, не readiness |
| API-key middleware | Защищает все non-health routes при `UAM_API_KEY` |
| `GET /metrics` | Prometheus counters/lag; защищён API key |
| `GET /ready` | Canonical PostgreSQL readiness + retrieval source state | `503` only when canonical storage is unavailable; optional vector outage is `200 degraded` |
| `GET /ui` | Local operator console | Same API-key middleware as API routes |
| `GET /v1/system/status` | Реальное process/storage/runtime состояние для UI | Не подменяет dependency readiness |
| `GET /v1/settings/models` | Текущие и desired model settings | Не раскрывает API keys |
| `PUT /v1/settings/models` | Сохраняет desired embedding settings | Требует restart/reindex для применения |
| `POST /v1/settings/models/test` | Проверяет operator-supplied embedding endpoint | Exact-origin allowlist, запрет redirects; production также требует egress policy |
| `GET /v1/audit/events` | Operator audit export | Operator/admin scope only; tenant/workspace/action filters |
| `GET /v1/keys` | Operator API-key registry | Non-secret fingerprints, scopes, last-used/revoked state |
| `POST /v1/keys/{id}/revoke` | Revoke one configured key | Future requests with that bearer are denied |
| `POST /v1/identities/provision` | Atomic agent + optional thread bootstrap | Operator-only; idempotent; rejects cross-scope ID reuse |
| `GET /v1/workspaces/{id}/memories` | Operator memory list | Optional layer/status/label filters |
| `POST /v1/memory/retain` | REST boundary для retain |
| `POST /v1/conversations/turns` | Append immutable raw transcript turn | Не создаёт recallable memory автоматически |
| `GET /v1/conversations/turns` | Operator transcript listing | Workspace/thread/namespace filters и bounded limit |
| `POST /v1/conversations/turns/{id}/curate` | Raw turn → evidence-backed proposal | По умолчанию auto-accept только для строгой evidence policy; иначе proposal вне recall. `auto_accept:false` принудительно оставляет review; `curated_only` затем purge raw text |
| `POST /v1/workspaces/{id}/conversations/purge-expired` | Purge expired `curated_only` staging text | Operator-only, bounded batch, audit event |
| `POST /v1/memory/proposals` | Создаёт evidence-backed memory proposal | Proposal остаётся вне recall до accept |
| `GET /v1/memory/proposals` | Proposal review listing | Namespace/status filters; bounded stable keyset cursor |
| `POST /v1/memory/proposals/{id}/accept` | Accept proposal → `MemoryItem` | Идемпотентный append с provenance |
| `POST /v1/memory/proposals/{id}/reject` | Reject proposal | Не создаёт recallable memory |
| `PUT /v1/memory/{id}/supersede` | CAS replacement; stale revision → `409 revision_conflict` |
| `POST /v1/memory/recall` | Recall + context compilation | Status/scope filters и token budget |
| `POST /v1/workspaces/{id}/vault/import` | Dry-run/apply edited vault notes | Applies through `supersede`; conflicts on stale revisions |
| `GET /v1/workspaces/{id}/vault` | Human-readable Markdown projection | PostgreSQL остаётся source of truth |
| `POST /v1/workspaces/{id}/vault/archive` | Non-destructive archive revision | Сохраняет history и audit event |
| `POST /v1/ingest/text` | Детерминированный text ingestion |
| `POST /v1/ingest/document` | Base64 Markdown/PDF ingestion, лимит 20 MiB |
| `POST /v1/workspaces/{id}/reflect` | Запуск baseline sleep/reflection |
| `GET /v1/workspaces/{id}/conflicts` | Conflict review inbox | Derived cases; `include_resolved=true` optional |
| `PUT /v1/workspaces/{id}/conflicts/{case_id}/decision` | Persist human review decision | accepted/overridden/dismissed/unresolved |
| `POST /v1/graph/edges` | Create typed graph edge | Validates endpoint memories and workspace |
| `GET /v1/memory/{id}/neighbors` | List graph neighbors | Optional edge type filter |
| `POST /v1/workspaces/{id}/reindex` | Запускает workspace reindex | Scoped sync preserves every other workspace and removes stale IDs after successful upsert |
| `POST /v1/checkpoints` | Создаёт checkpoint revision | First save использует CAS expected head `0` |
| `GET /v1/checkpoints` | Workspace checkpoint heads | Tenant/workspace scoped |
| `GET /v1/checkpoints/{thread_id}` | Последний checkpoint thread | Возвращает `404`, если head отсутствует |
| `GET /v1/checkpoints/{thread_id}/revisions/{revision}` | Конкретная checkpoint revision | Историческое чтение без мутации |
| `PUT /v1/checkpoints/{thread_id}` | CAS checkpoint update | PostgreSQL advisory lock; stale expected revision → conflict |
| `POST /v1/checkpoints/{thread_id}/compact` | Удаляет старые checkpoint revisions | Явная bounded retention operation |

## PostgreSQL adapter

| Функция | Назначение | Гарантия |
|---|---|---|
| `PostgresMemoryLedger.connect()` | Проверяет соединение и наличие schema | Не оставляет открытое соединение |
| `ensure_standalone_scope(...)` | Создаёт fixed server/project namespace | Идемпотентно |
| `retain(item, event, key)` | Записывает item, provenance, key и outbox | Одна транзакция; concurrent idempotency через advisory lock |
| `supersede_if_current(item, event, expected_revision, key)` | CAS-запись новой ревизии | `FOR UPDATE` parent + recursive head check; одна outbox-транзакция |
| `append(item, key)` | Импортирует memory без события | Append-only и tenant-bound |
| `_stored_memory_text(connection, item)` | Готовит plaintext или pgcrypto ciphertext для `memory_items.text` | Supports all-row encryption or selected `MemoryScope` values via `UAM_MEMORY_TEXT_ENCRYPTION_SCOPES` |
| `_stored_sensitive_json(connection, value)` | Шифрует JSONB operational evidence | Прозрачная backward-compatible wrapper для audit metadata и checkpoint state |
| `get(tenant, item)` | Загружает memory с provenance | Устанавливает RLS tenant context |
| `list_for_workspace(...)` | Детализация workspace с layer filter | Детерминированный порядок |
| `search(query)` | PostgreSQL lexical fallback | Project/thread/label/time filters |
| `save(observation)` | Хранит reflection и evidence links | Evidence не меняется |
| `save_conflict_review(decision)` | Upsert human decision | RLS tenant-bound; no mutation of raw evidence |
| `list_conflict_reviews(...)` | Read persisted review decisions | Workspace-scoped and deterministic |
| `save_edge(edge)` | Insert graph edge | Uses existing `memory_edges` table |
| `list_neighbors(...)` | Read incoming/outgoing edges | RLS tenant-bound; optional type filter |
| `append_audit_event(event)` | Append operator/agent audit record | RLS tenant-bound; immutable event row |
| `list_audit_events(...)` | Export recent audit records | Filters workspace/action/resource; newest first |
| `save_api_key_record(record)` | Upsert key registry row | Stores fingerprint, never secret |
| `touch_api_key(...)` | Update `last_used_at` | RLS tenant-bound |
| `revoke_api_key(...)` | Set `revoked_at/reason` | Does not delete row |
| `collect_metrics(tenant)` | Считает counters и outbox lag | Устанавливает RLS tenant context |

## Checkpoint domain — `domain/checkpoint.py`

| Тип | Назначение | Инвариант |
|---|---|---|
| `Checkpoint` | Frozen ревизионный snapshot | `revision >= 1`, immutable |
| `StaleRevisionError` | CAS conflict exception | Содержит `expected`, `actual` |

## CheckpointService — `services/checkpoint.py`

| Функция | Назначение | Гарантия |
|---|---|---|
| `save(tenant_id, workspace_id, thread_id, state)` | Auto-increment save | CAS-protected через store |
| `update(tenant_id, workspace_id, thread_id, state, expected_revision)` | CAS update | Raises `StaleRevisionError` |
| `restore(tenant_id, thread_id)` | Load latest checkpoint | None если нет |
| `restore_revision(tenant_id, thread_id, revision)` | Load specific revision | None если нет |
| `compact(tenant_id, thread_id, keep_last)` | Удаляет старые ревизии | Возвращает count удалённых |
| `list_for_workspace(tenant_id, workspace_id)` | Head checkpoints по workspace | Детерминированный порядок |

## CheckpointStore port — `ports/checkpoint_store.py`

| Метод | Назначение | Гарантия |
|---|---|---|
| `save(checkpoint)` | Unconditional append | Без CAS проверки |
| `save_if_head(checkpoint, expected_revision)` | CAS append | Raises `StaleRevisionError` |
| `get_head(tenant_id, thread_id)` | Latest revision | None если нет |
| `get_revision(tenant_id, thread_id, revision)` | Specific revision | None если нет |
| `list_for_workspace(tenant_id, workspace_id)` | Head per thread | Tenant-scoped |
| `compact(tenant_id, thread_id, keep_last)` | Удаление старых | Возвращает count |

## SDK — `sdk/python`, `sdk/typescript`

| Функция | Назначение | Гарантия |
|---|---|---|
| `MemoryClient.retain()` | Сохраняет memory | Один generated idempotency key на все retries |
| `MemoryClient.recall()` | Возвращает typed results и context | Standalone defaults остаются server-side |
| `MemoryClient.ingest_text()` / `ingestText()` | Загружает текст | Typed checksum и memory IDs |
| retry loop | Повторяет network/429/502/503/504 | Bounded exponential backoff, `Retry-After` |
| typed errors | Нормализует HTTP failures | Сохраняет status code |

## Qdrant adapter — `adapters/qdrant.py`

| Функция | Назначение | Гарантия |
|---|---|---|
| `QdrantCandidateSource.__init__(url, collection, dense_dim, api_key, ledger, payload_text)` | Capture Qdrant endpoint, vector config and payload policy | Нет I/O до `connect()` |
| `connect()` | Create QdrantClient, ensure collection with dense+sparse vectors | Идемпотентно; requires `qdrant-client` |
| `search(query)` | Hybrid search with project-scoped filtering | Tenant/workspace/layer/label filters |
| `upsert(item, dense_vector, sparse_indices?, sparse_values?)` | Insert/update vector point with filter metadata | Can redact raw text from Qdrant payload and hydrate from ledger |
| `delete(item_id)` | Remove point by memory item ID | Нет ошибки если не существует |
| `reindex(items)` | Drop collection and re-insert from scratch | Блокирующий; batch по 100 |
| `_use_in_memory_backend()` | Activate test-only in-memory fallback | Нет зависимости на qdrant-client |

## Embedding ports — `ports/embeddings.py`

| Функция / Свойство | Назначение | Гарантия |
|---|---|---|
| `EmbeddingClient.model_name` | Название и версия модели | Уникальный строковый ID |
| `EmbeddingClient.dimension` | Размерность выходного вектора | Фиксированный `int` |
| `EmbeddingClient.embed(text)` | Генерация dense вектора | Возвращает `list[float]` |

## Embedding adapters — `adapters/embeddings.py`

| Класс / Функция | Назначение | Гарантия |
|---|---|---|
| `FakeEmbeddingClient` | Генерация детерминированных мок-векторов | Векторы воспроизводимы по MD5 от текста |
| `OpenAICompatibleEmbeddingClient` | Generic `/v1/embeddings` gateway | Optional bearer auth; sends provider-neutral `input`/`model` by default |
| `OpenAIEmbeddingClient` | OpenAI-hosted `/v1/embeddings` profile | Requires bearer auth; sends `input`, `model`, `dimensions` |
| `OllamaEmbeddingClient` | Local Ollama `/api/embeddings` | Uses `prompt` payload; no API key required |
| `TEIEmbeddingClient` | TEI/vLLM-style `/v1/embeddings` | OpenAI-compatible payload; optional bearer key |
| `EmbeddingProviderConfig.from_env()` | Env → provider config | Reads `UAM_EMBEDDING_*` variables |
| `build_embedding_client()` | Provider config → concrete client | Rejects unsupported providers and invalid dimensions |

## Memory LLM adapter — `adapters/llm.py`

| Класс / Функция | Назначение | Гарантия |
|---|---|---|
| `MemoryLLMConfig.from_env()` | Env → OpenAI-compatible memory LLM config | Provider-neutral compact 8k curation window |
| `MemoryLLMConfig.public_dict()` | Safe status payload | Does not expose API key |
| `MemoryLLMClient.chat(messages)` | Calls OpenAI-compatible `/chat/completions` | Returns assistant text or raises `MemoryLLMError` |
| `MemoryLLMClient.chat_json(messages)` | Requests JSON object output for memory workers | Strips fenced JSON and rejects non-object JSON |
| `build_memory_llm_client()` | Builds the default memory LLM client | Uses `UAM_MEMORY_LLM_*` runtime config |
| `MemoryLLMError` | Normalized endpoint/protocol failure | Keeps worker error handling provider-agnostic |

## Embedding service — `services/embedding.py`

| Функция | Назначение | Гарантия |
|---|---|---|
| `process_memory_retained(tenant, id)` | Асинхронная обработка и индексация памяти | Загружает память и делает upsert в Qdrant |
| `reindex_all(tenant, workspace)` | Полная переиндексация воркспейса | Precomputes vectors, serializes per workspace and calls scoped Qdrant sync |
| `sync_workspace(tenant, workspace, items)` | Crash-recoverable workspace vector replacement | Upsert replacement batches first; delete stale IDs only after all batches succeed; a retry converges after partial Qdrant failure |
| `migrate_vector_collection.py` | Build new model-specific collection + exact count report | Never deletes or mutates the active source collection |
| `collect_metrics()` | Process-local embedding health counters | Operations/failures/latency/reindex metrics for `/metrics` |
| `_validate_dimension(vector)` | Provider output guard | Mismatch aborts before Qdrant write |
