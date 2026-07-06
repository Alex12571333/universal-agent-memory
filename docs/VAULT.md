# Obsidian vault export

Universal Agent Memory keeps PostgreSQL as the source of truth, but exposes a
human-readable vault projection for review, backup and Obsidian-style browsing.

The first implementation is intentionally one-way: it exports deterministic
Markdown files through the API and through the Docker ops `vault-export` service.
It does not import edits back into memory yet. Import/edit should use a later
CAS/supersede workflow so manual changes never destructively overwrite history.

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

## Next step: safe import

Import is deliberately not part of this first export endpoint. The planned
workflow:

1. parse Markdown frontmatter;
2. detect changed files by `id`;
3. require expected `revision`;
4. create new memory through `supersede`;
5. never directly mutate or delete canonical memory rows.
