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
separates verified MCP behavior and ChatGPT search calls from pending
OAuth web-interface checks.

For your own local instance: `http://localhost:8000/mcp`.
Publishing uses OAuth with the `publish` scope. Clients obtain and refresh their
own tokens, without manual header configuration. Select OAuth and approve the
PeopleMCP consent page. Leave optional client ID/secret empty for registration.

Search queries should be in English. The server instructions, search tool
descriptions and `query` schemas tell agents to translate non-English requests
before searching, preserve constraints, negations and names, and respond in the
user's language. Translated excerpts must be labelled rather than presented as
verbatim evidence. PeopleMCP does not run a translator or enforce query language;
direct API clients must prepare English queries themselves. Refresh tool
metadata or reconnect existing clients after updating the server.

The six discovery tools are `upsert_profile`, `get_profile`, `search_people`, `upsert_project`,
`get_project` and `search_projects`. Upserts identify an existing publication by
slug; pass its optional UUID `id` when renaming. Get tools accept UUID or slug.
Two personal OAuth helpers, `create_connection_code` and `redeem_connection_code`,
link existing clients without exposing access/refresh tokens. Each requires
`confirm=true`. Request a code in your existing authorized private chat and redeem
it in your other authorized chat, or enter it on the OAuth consent page. Refresh
cached tool lists after this update. See [linking instructions](../README.md#link-chatgpt-claude-or-another-client-to-the-same-owner).

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

OAuth uses authorization-code + S256 PKCE, dynamic client registration and
rotating refresh tokens. Discovery metadata is at
`/.well-known/oauth-protected-resource/mcp` (also available at the origin path)
and `/.well-known/oauth-authorization-server`. Upserts and connection-code tools declare `oauth2` security
schemes with scope `publish`; reads declare `noauth`. Unauthenticated upserts
return HTTP 401 with `WWW-Authenticate` before entering the MCP handler, so
clients can open OAuth and retry the call. The adapter also includes the
`mcp/www_authenticate` tool-error metadata if authorization expires mid-call.
The HTTP API uses the same challenge. `/revoke` revokes a token's entire grant.
See [Claude lazy authentication](https://claude.com/docs/connectors/building/lazy-authentication)
and [ChatGPT tool authentication](https://developers.openai.com/plugins/build/auth).

The browser's secure cookie restores the same publisher when reconnecting for
90 days. Without that cookie or a connection code, a fresh OAuth connection
creates a different publisher. A retained OAuth connection does not depend on
the browser cookie. This is not verified identity. Losing all browser and
connector credentials still has no automatic recovery.
Every edit checks ownership in the database; existing operator/demo entries are
not claimable. The privileged `WRITE_TOKEN` remains only for the operator's CLI
and administration, never for distribution to connector users. Secrets do not
belong in URLs or profile content. Access, refresh and operator tokens never
belong in chat or tool arguments. Only explicitly requested, short-lived
connection codes may be shown/entered in the user's private chat.

Codes are single-use, expire after five minutes, and are stored hashed. A new code
replaces the previous one. Code issuance/redemption is limited to five requests
per owner per five minutes; browser redemption also has a per-flow limit. The
issuer remains the canonical owner. Linking moves the receiving owner's existing
connections; no public content is deleted. If it already has publications, the
server returns 409 until the user separately approves `confirm_merge_publications=true`.
Existing token/refresh grants and browser sessions continue with the canonical
owner; only the duplicate internal owner ID and its outstanding codes are removed.

The adapter is `server/mcp_adapter.py`. It forwards requests through the actual
FastAPI routes using HTTPX's ASGI transport, including validation and caller
authentication. There is no separate search, indexing or database implementation.
Its Python module is outside this folder to avoid shadowing the official `mcp` SDK.
