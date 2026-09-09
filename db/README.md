# Storage

PostgreSQL 17 with pgvector; six domain tables: profiles, profile_chunks,
projects, project_chunks, agents and agent_chunks. `index_metadata` records the
embedding model. The application runs all numbered SQL files in `migrations/`
at startup in one transaction. The SQL is idempotent.

`004_agents.sql` adds the agent tables and `publication_owners.agent_id` without
rewriting profiles, projects, vectors or OAuth grants. It replaces the old
two-kind ownership check with `num_nonnulls(profile_id, project_id, agent_id) = 1`.
Each publication still has at most one owner, with foreign-key cascades on
publication deletion. Back up before upgrading; no destructive downgrade
migration is provided. The existing Docker volume and URLs remain unchanged.

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

Backups also contain private authorization state. Keep them access-restricted
and outside the Git repository.
