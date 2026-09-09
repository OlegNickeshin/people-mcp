# Remote MCP

Hosted endpoint: `https://people-mcp.194-87-35-210.sslip.io/mcp` (Streamable HTTP).
Discovery is live and requires no authentication. The maintainer confirmed
successful ChatGPT connector creation with this hostname and No Auth.
API docs: [https://194.87.35.210/docs](https://194.87.35.210/docs).
Use `/mcp` in clients, not `/docs`. Prefer the hostname for MCP connections.
The direct-IP endpoint `https://194.87.35.210/mcp` passes protocol-level tests,
but ChatGPT connector creation failed with that URL; use the hostname instead.

Step-by-step connection guides: [ChatGPT](../README.md#chatgpt),
[Claude](../README.md#claude), and
[Claude Code / other agents](../README.md#claude-code-and-other-agents).
ChatGPT uses developer mode; Claude uses a custom connector. No local server
is needed to use the hosted index. The main guide includes a test query and
separates verified MCP behavior and ChatGPT connection setup from pending
in-chat search and Claude interface checks.

For your own local instance: `http://localhost:8000/mcp`.
Publishing requires `Authorization: Bearer <WRITE_TOKEN>` on the HTTP connection.

The six tools are `upsert_profile`, `get_profile`, `search_people`, `upsert_project`,
`get_project` and `search_projects`. Upserts identify an existing publication by
slug; pass its optional UUID `id` when renaming. Get tools accept UUID or slug.

Example configuration for clients supporting the `mcpServers` HTTP format
(other clients may require different keys):

```json
{
  "mcpServers": {
    "people-mcp": {
      "type": "http",
      "url": "https://people-mcp.194-87-35-210.sslip.io/mcp"
    }
  }
}
```

For publishing, add a `headers` object with `Authorization: Bearer YOUR_WRITE_TOKEN`
if your client supports custom headers. Configure credentials in the client,
never in a profile or tool argument. Clients without custom headers can still search.
The hosted instance does not accept the local development token. There is no
OAuth login or per-user ownership in this MVP; do not distribute the operator
token, which can update any publication.

The adapter is `server/mcp_adapter.py`. It forwards requests through the actual
FastAPI routes using HTTPX's ASGI transport, including validation and caller
authentication. There is no separate search, indexing or database implementation.
Its Python module is outside this folder to avoid shadowing the official `mcp` SDK.
