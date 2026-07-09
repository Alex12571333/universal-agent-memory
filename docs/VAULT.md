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

## Opening in Obsidian

1. Run the Docker export command above.
2. In Obsidian, choose **Open folder as vault** and select `./vault`.
3. Edit only `mem-*` memory notes when you want to propose memory changes.
4. Keep the YAML frontmatter intact, especially `id`, `type`, `tenant_id`,
   `workspace_id` and `revision`.
5. Re-import with dry-run first, then apply if the plan is safe.

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

The current export preserves lifecycle signals already present in the memory
server: `revision`, `supersedes_id`, `valid_from`, `valid_to`, confidence and
reflection `stale`. A later lifecycle/status work package should add explicit
`active`, `superseded`, `disputed` and `rejected` status fields.

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

Docker users can dry-run the materialized `./vault` folder:

```bash
docker compose --profile ops run --rm vault-import
```

Apply requires an explicit command override:

```bash
docker compose --profile ops run --rm vault-import \
  python scripts/import_vault.py /vault --apply
```

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
