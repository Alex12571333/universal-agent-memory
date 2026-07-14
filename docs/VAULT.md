# Obsidian vault export/import

Obelisk Memory keeps PostgreSQL as the source of truth, but exposes a
human-readable vault projection for review, backup and Obsidian-style browsing.

The vault is a human-readable projection of canonical memory. Export is
deterministic; import is conservative and safe: edited memory notes create new
revisions through CAS `supersede`, never destructive overwrites.

## Export endpoint

```http
GET /v1/workspaces/{workspace_id}/vault?tenant_id={tenant_id}
```

The response is JSON:

```json
{
  "tenant_id": "...",
  "workspace_id": "...",
  "file_count": 3,
  "files": [
    {
      "path": "core/mem-00000000-0000-0000-0000-000000000000.md",
      "content": "---\ntype: \"memory\"\n...\n"
    }
  ]
}
```

Clients can materialize this response into a local folder and open that folder
as an Obsidian vault. Docker users can run:

```bash
docker compose --profile ops run --rm vault-export
```

which writes the current workspace projection to `./vault`.

## Deterministic vault-health check

Before reviewing or importing a vault, an operator can inspect the canonical
projection without invoking an LLM, embedding model or graph extractor:

```http
GET /v1/workspaces/{workspace_id}/vault/health?tenant_id={tenant_id}
```

The response is tenant/workspace-scoped and contains counts plus diagnostics.
`error` means a canonical reference is broken (for example a graph endpoint or
observation evidence item is missing). `warning` currently includes an
`unlinked_memory_head`: an active recallable memory that has neither a typed
graph edge nor active observation evidence. It is deliberately not considered
corruption and is never automatically linked or rewritten.

The check is read-only. It does not expose raw conversation content, vectors or
embedding payloads, and it does not change vault files or canonical memories.

## Opening in Obsidian

1. For an editable trusted-environment export, omit the integrity manifest:

   ```bash
   docker compose --profile ops run --rm vault-export \
     python scripts/export_vault.py /vault --no-manifest
   ```

2. In Obsidian, choose **Open folder as vault** and select `./vault`.
3. Edit only `mem-*` memory notes when you want to propose memory changes.
4. Keep the YAML frontmatter intact, especially `id`, `type`, `tenant_id`,
   `workspace_id` and `revision`.
5. Re-import with dry-run first, then apply if the plan is safe.

This manifest-free path is not signed production evidence. A normal export writes
a manifest, and the importer verifies it whenever it is present; editing any
covered file intentionally invalidates that bundle.

## Layout

Memory items are grouped by memory layer:

```text
README.md
core/
working/
semantic/
episodic/
procedural/
social/
reflection/
error/
reflections/
```

`reflections/` contains derived observations created by the reflection service.

## Frontmatter

Every memory file includes conservative YAML frontmatter:

```yaml
---
id: "..."
type: "memory"
tenant_id: "..."
workspace_id: "..."
layer: "semantic"
scope: "workspace"
kind: "fact"
revision: 1
status: "active"
labels: ["architecture"]
confidence: 0.91
supersedes_id: null
source_kind: "api"
origin_uri: null
checksum_sha256: null
extraction_version: "manual-v1"
---
```

The Markdown body contains the memory text, provenance and Obsidian wiki links
for revision chains.

## Status semantics

The current export preserves `revision`, `supersedes_id`, `valid_from`,
`valid_to`, confidence, memory status and reflection `stale`. A memory note can
carry `active`, `stale`, `deprecated`, `disputed`, `hypothesis`, `rejected`,
`archived` or `pinned`; an item with a newer revision is projected as
`superseded`.

The export never hides superseded/stale data. Obsidian should show both the
current belief and the audit trail that explains how it changed.

## Stable filenames

File names are deterministic enough for repeated exports:

```text
{layer}/mem-{uuid}.md
reflections/obs-{uuid}.md
```

The UUID-based filename makes Obsidian links stable across repeated exports.

## Safe import endpoint

```http
POST /v1/workspaces/{workspace_id}/vault/import
```

Request:

```json
{
  "tenant_id": "00000000-0000-0000-0000-000000000001",
  "dry_run": true,
  "files": [
    {
      "path": "semantic/mem-00000000-0000-0000-0000-000000000000.md",
      "content": "---\ntype: \"memory\"\n...\n"
    }
  ]
}
```

Response:

```json
{
  "dry_run": true,
  "supersede_count": 1,
  "changes": [
    {
      "path": "semantic/mem-....md",
      "action": "supersede",
      "item_id": "...",
      "expected_revision": 1,
      "new_item_id": null,
      "message": "memory text changed"
    }
  ]
}
```

Set `"dry_run": false` to apply planned changes.

The operator UI uses a narrower endpoint for an actual edit:

```http
PATCH /v1/workspaces/{workspace_id}/vault/memories/{item_id}
```

Send `expected_revision` and exactly one of `replace_body` or
`replace_section` (an existing heading plus replacement content). `confidence`
is the only service field accepted by this targeted contract. A successful
change creates an append-only superseding revision and enqueues its embedding
through the transactional outbox. It never accepts vectors, tenant/workspace
frontmatter, provenance, status, or arbitrary metadata. A concurrent edit
returns HTTP `409`; retrying the identical patch is idempotent.

```json
{
  "tenant_id": "00000000-0000-0000-0000-000000000001",
  "expected_revision": 3,
  "replace_section": {
    "heading": "Решение",
    "content": "Использовать локальный OpenAI-compatible endpoint."
  }
}
```

Docker users can dry-run the materialized `./vault` folder:

```bash
docker compose --profile ops run --rm vault-import
```

Apply requires an explicit command override:

```bash
docker compose --profile ops run --rm vault-import \
  python scripts/import_vault.py /vault --apply
```

## Signed vault manifests

The CLI exporter writes a tamper-evident manifest next to the Markdown files:

```text
.uam-vault-manifest.json
.uam-vault-manifest.sha256
.uam-vault-manifest.sig   # when UAM_VAULT_SIGNING_KEY or --signing-key is set
```

The manifest records every `*.md` path, byte size and SHA-256 checksum. The
checksum file protects the manifest itself. The optional signature is
`hmac-sha256` over the canonical manifest JSON.

For production exports, sign the bundle with an operator-held key:

```bash
UAM_VAULT_SIGNING_KEY=... python scripts/export_vault.py ./vault
```

For integrity-only production imports, require the signature before planning or
applying:

```bash
UAM_VAULT_SIGNING_KEY=... python scripts/import_vault.py ./vault \
  --require-signature

UAM_VAULT_SIGNING_KEY=... python scripts/import_vault.py ./vault \
  --apply \
  --require-signature
```

If any Markdown file is edited after export, verification fails before the
import service is called. The current CLI has no operator command for reviewing
an edited vault and re-signing its manifest. Consequently, a signed bundle can
prove the integrity of an unchanged export, but it cannot currently carry a
human edit through a signature-required production import.

Human editing remains available through a manifest-free export followed by
dry-run/apply import in a trusted development or controlled operator environment.
Do not describe that path as a signed production workflow. A production-grade
editable bundle still needs an explicit review-and-re-sign command, signer
authorization policy and audit event before `--require-signature` can be combined
with edited Markdown.

The workflow is:

1. parse Markdown frontmatter;
2. detect changed files by `id`;
3. require expected `revision`;
4. create new memory through `supersede`;
5. never directly mutate or delete canonical memory rows.

Import actions:

- `unchanged` — memory body matches the canonical row;
- `supersede` — memory body changed and can create a new revision;
- `conflict` — exported revision is stale, so the edit must be rebased;
- `skip` — non-memory notes such as `README.md` or `obs-*`;
- `error` — malformed frontmatter, missing memory id or tenant/workspace
  mismatch.

Only the main note body is imported. Generated sections such as `## Provenance`,
`## Quote`, `## Links` and `## Evidence` remain audit output and are ignored by
the importer.
