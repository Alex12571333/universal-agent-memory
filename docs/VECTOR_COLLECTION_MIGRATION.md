# Vector collection migration

Obelisk Memory assigns every Qdrant collection one immutable embedding identity:
the dense-vector dimension and exact model name. Startup fails when configured
identity differs from collection metadata. This prevents silent mixing of
vectors produced by incompatible models.

An ordinary workspace reindex never recreates or renames the active collection.
Use a new collection for every model or dimension change.

## Migration procedure

1. Keep the active API and workers on the current collection.
2. Configure the new embedding provider, model and dimension in the operator
   environment used for the migration command.
3. Choose a new immutable collection name, for example
   `memory_items_jina_v3_1024`.
4. Populate and verify it without changing the running deployment:

   ```bash
   PYTHONPATH=src python scripts/migrate_vector_collection.py \
     --target-collection memory_items_jina_v3_1024 \
     --report ./ops/vector-collection-migration.json
   ```

   The command reads the normal PostgreSQL, Qdrant and embedding configuration,
   indexes canonical active heads through the production service, and compares
   the expected workspace count with an exact Qdrant count. It never deletes the
   source collection.

5. Preserve the report, set
   `UAM_QDRANT_COLLECTION=memory_items_jina_v3_1024` for both API and embedding
   worker, then restart them together.
6. Verify `/ready`, semantic recall, embedding metrics and the real embedding
   regression suite before accepting the release.

## Rollback

Restore the previous embedding configuration and previous
`UAM_QDRANT_COLLECTION`, then restart API and worker together. The source
collection remains unchanged until an explicit post-release retention procedure
removes it.

Do not point two embedding models at one collection. Do not delete the previous
collection until the rollback window has closed and its signed migration and
recall evidence has been archived.
