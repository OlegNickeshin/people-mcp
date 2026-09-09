# Remote MCP

Endpoint: `http://localhost:8000/mcp` (Streamable HTTP).
Use your public HTTPS URL for a remote client. Discovery requires no authentication.
Publishing requires `Authorization: Bearer <WRITE_TOKEN>` on the HTTP connection.

The six tools are `upsert_profile`, `get_profile`, `search_people`, `upsert_project`,
`get_project` and `search_projects`. Upserts identify an existing publication by
slug; pass its optional UUID `id` when renaming. Get tools accept UUID or slug.

Example configuration for clients supporting the `mcpServers` URL format:

```json
{
  "mcpServers": {
    "people-mcp": {
      "url": "https://YOUR_HOST/mcp"
    }
  }
}
```

For publishing, add a `headers` object with `Authorization: Bearer YOUR_WRITE_TOKEN`
if your client supports custom headers. Configure credentials in the client,
never in a profile or tool argument. Clients without custom headers can still search.

The adapter is `server/mcp_adapter.py`. It forwards requests through the actual
FastAPI routes using HTTPX's ASGI transport, including validation and caller
authentication. There is no separate search, indexing or database implementation.
Its Python module is outside this folder to avoid shadowing the official `mcp` SDK.
