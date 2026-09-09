# Storage

PostgreSQL 17 with pgvector; four domain tables: profiles, profile_chunks,
projects and project_chunks. `index_metadata` records the embedding model.
The application runs `migrations/001_initial.sql` at startup. The SQL is idempotent.

Only `content` is embedded. `contact` is public but not embedded. There is no
private profile storage or external context ingestion. Updates replace the
publication and its chunks in one database transaction. Contact-only edits do
not regenerate embeddings. Identical updates leave `updated_at` unchanged.

Search uses exact cosine distance (`<=>`) on pgvector columns. Each entity's
score is its best chunk score, with up to three matching excerpts. The entire
small corpus is considered, so long profiles cannot exhaust a global chunk
candidate limit. No freshness weighting or generative LLM is involved.

Postgres is not published on a host port. Data persists in the Compose volume
`people-mcp_postgres-data`. Use `docker compose down` to stop without deleting it.

Backup (run in the repository on the server):

```sh
docker compose exec -T db pg_dump -U peoplemcp peoplemcp > peoplemcp.sql
```

Backups contain public content and should still be stored outside the Git repository.
