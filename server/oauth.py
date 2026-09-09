"""Anonymous publisher authorization: SDK OAuth/PKCE, PostgreSQL state, one consent page.

This proves possession of a browser/connector credential, not a person's identity.
No password, external identity provider, URL bearer token or server-side LLM.
"""
import base64
import hashlib
import html
import json
import re
import secrets
import time
from contextlib import contextmanager
from functools import wraps
from urllib.parse import quote, unquote, urlsplit
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import AnyHttpUrl
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData
from starlette.datastructures import Headers
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route, request_response

from mcp.server.auth.handlers.revoke import RevocationHandler
from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.middleware.client_auth import AuthenticationError
from mcp.server.auth.provider import (
    AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError,
    RefreshToken, RegistrationError, TokenError, construct_redirect_uri,
)
from mcp.server.auth.routes import build_metadata, create_auth_routes, create_protected_resource_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import RequestBodyLimitMiddleware
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from server.db import lock_publishers

SCOPE = "publish"
PROTECTED_TOOLS = frozenset({"upsert_profile", "upsert_project", "upsert_agent", "create_connection_code", "redeem_connection_code"})
ACCESS_TTL = 3600
SESSION_TTL = 90 * 86400
FLOW_TTL = 600
CODE_TTL = 120
SAFE_HEADERS = {
    "Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
}


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def threaded(method):
    @wraps(method)
    async def call(*args, **kwargs):
        return await run_in_threadpool(method, *args, **kwargs)
    return call


class PublisherOAuth:
    def __init__(self, settings):
        self.database_url = settings.database_url
        self.origin = settings.public_base_url
        self.resource = self.origin + "/mcp"
        self.metadata_url = self.origin + "/.well-known/oauth-protected-resource/mcp"
        self.secure = self.origin.startswith("https://")
        prefix = "__Host-" if self.secure else ""
        self.browser_cookie = prefix + "peoplemcp-publisher"
        self.csrf_cookie = prefix + "peoplemcp-consent"

    @contextmanager
    def connection(self):
        with psycopg.connect(self.database_url, row_factory=dict_row, connect_timeout=5) as conn:
            yield conn

    def challenge(self):
        return (f'Bearer resource_metadata="{self.metadata_url}", scope="{SCOPE}", '
                'error="invalid_token", error_description="Connect PeopleMCP with OAuth to publish your own context"')

    @threaded
    def get_client(self, client_id):
        with self.connection() as conn:
            row = conn.execute("SELECT metadata FROM oauth_clients WHERE client_id=%s", (client_id,)).fetchone()
        return OAuthClientInformationFull.model_validate(row["metadata"]) if row else None

    @threaded
    def client_secret_hash(self, client_id):
        with self.connection() as conn:
            row = conn.execute("SELECT secret_hash FROM oauth_clients WHERE client_id=%s", (client_id,)).fetchone()
        return row["secret_hash"] if row else None

    @threaded
    def register_client(self, client_info):
        if not 1 <= len(client_info.redirect_uris or []) <= 10:
            raise RegistrationError("invalid_redirect_uri", "Provide between one and ten redirect URIs")
        for uri in client_info.redirect_uris:
            parts = urlsplit(str(uri))
            if (len(str(uri)) > 2048 or not parts.hostname or parts.fragment or parts.username or parts.password
                    or (parts.scheme != "https" and not (
                        parts.scheme == "http" and parts.hostname in {"localhost", "127.0.0.1", "::1"}))):
                raise RegistrationError("invalid_redirect_uri", "Use HTTPS or an HTTP loopback callback, without credentials or fragments")
        if (len(client_info.client_name or "") > 200
                or client_info.token_endpoint_auth_method not in {"none", "client_secret_post", "client_secret_basic"}
                or set(client_info.grant_types) != {"authorization_code", "refresh_token"}
                or set(client_info.response_types) != {"code"}):
            raise RegistrationError("invalid_client_metadata", "Unsupported client metadata")
        data = client_info.model_dump(mode="json", include={
            "client_id", "client_id_issued_at", "client_secret_expires_at", "redirect_uris",
            "token_endpoint_auth_method", "grant_types", "response_types", "client_name", "scope",
        })
        secret_hash = digest(client_info.client_secret) if client_info.client_secret else None
        with self.connection() as conn:
            conn.execute("INSERT INTO oauth_clients (client_id,metadata,secret_hash) VALUES (%s,%s,%s)",
                         (client_info.client_id, Jsonb(data), secret_hash))

    @threaded
    def authorize(self, client, params):
        if params.resource is not None and params.resource != self.resource:
            raise AuthorizeError("invalid_request", "The resource must be this PeopleMCP endpoint")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", params.code_challenge):
            raise AuthorizeError("invalid_request", "A valid S256 PKCE challenge is required")
        if len(params.state or "") > 1024 or set(params.scopes or [SCOPE]) != {SCOPE}:
            raise AuthorizeError("invalid_scope", "Only the publish scope is supported")
        params = params.model_copy(update={"resource": self.resource, "scopes": [SCOPE]})
        flow = secrets.token_urlsafe(32)
        now = int(time.time())
        with self.connection() as conn:
            conn.execute("DELETE FROM oauth_pending WHERE expires_at < %s", (now - 60,))
            conn.execute("DELETE FROM oauth_browser_sessions WHERE expires_at < %s", (now,))
            conn.execute("INSERT INTO oauth_pending (flow_hash,client_id,params,expires_at) VALUES (%s,%s,%s,%s)",
                         (digest(flow), client.client_id, Jsonb(params.model_dump(mode="json")), now + FLOW_TTL))
        return self.origin + "/oauth/consent?flow=" + flow

    @threaded
    def prepare_consent(self, flow, csrf):
        with self.connection() as conn:
            row = conn.execute(
                "UPDATE oauth_pending SET csrf_hash=%s WHERE flow_hash=%s AND expires_at>%s "
                "AND code_hash IS NULL RETURNING client_id,params",
                (digest(csrf), digest(flow), int(time.time())),
            ).fetchone()
            if row:
                client = conn.execute("SELECT metadata FROM oauth_clients WHERE client_id=%s", (row["client_id"],)).fetchone()
                return row, client["metadata"]
        return None

    @threaded
    def finish_consent(self, flow, csrf, browser_secret, allow, connection_code="",
                       confirm_merge_publications=False, links=None):
        with self.connection() as conn:
            lock_publishers(conn)
            now = int(time.time())
            row = conn.execute("SELECT * FROM oauth_pending WHERE flow_hash=%s FOR UPDATE", (digest(flow),)).fetchone()
            if (not row or row["expires_at"] <= now or row["code_hash"] is not None
                    or not row["csrf_hash"] or not secrets.compare_digest(row["csrf_hash"], digest(csrf))):
                return None
            params = AuthorizationParams.model_validate(row["params"])
            if not allow:
                conn.execute("DELETE FROM oauth_pending WHERE flow_hash=%s", (digest(flow),))
                return construct_redirect_uri(str(params.redirect_uri), error="access_denied", state=params.state), None
            session = conn.execute(
                "SELECT publisher_id FROM oauth_browser_sessions WHERE token_hash=%s AND expires_at>%s",
                (digest(browser_secret or ""), now),
            ).fetchone()
            linked_id = None
            if connection_code:
                linked = links.redeem_in_transaction(conn, session["publisher_id"] if session else None,
                    connection_code, confirm_merge_publications, flow_hash=digest(flow))
                if "error" in linked:
                    return linked
                linked_id = linked["publisher_id"]
            if session:
                publisher_id = linked_id or session["publisher_id"]
                conn.execute("UPDATE oauth_browser_sessions SET expires_at=%s WHERE token_hash=%s",
                             (now + SESSION_TTL, digest(browser_secret)))
            else:
                publisher_id = linked_id or uuid4()
                browser_secret = secrets.token_urlsafe(32)
                if not linked_id:
                    conn.execute("INSERT INTO oauth_publishers (id) VALUES (%s)", (publisher_id,))
                conn.execute("INSERT INTO oauth_browser_sessions VALUES (%s,%s,%s)",
                             (digest(browser_secret), publisher_id, now + SESSION_TTL))
            code = secrets.token_urlsafe(32)
            conn.execute("UPDATE oauth_pending SET code_hash=%s,publisher_id=%s,expires_at=%s WHERE flow_hash=%s",
                         (digest(code), publisher_id, now + CODE_TTL, digest(flow)))
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state), browser_secret

    @threaded
    def load_authorization_code(self, client, authorization_code):
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM oauth_pending WHERE code_hash=%s AND client_id=%s",
                               (digest(authorization_code), client.client_id)).fetchone()
        if not row:
            return None
        params = AuthorizationParams.model_validate(row["params"])
        return AuthorizationCode(code=authorization_code, client_id=client.client_id,
                                 expires_at=row["expires_at"], subject=str(row["publisher_id"]),
                                 **params.model_dump(exclude={"state"}))

    def mint_tokens(self, conn, grant_id):
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = int(time.time())
        conn.execute("INSERT INTO oauth_tokens (token_hash,grant_id,kind,expires_at) VALUES (%s,%s,'access',%s)",
                     (digest(access), grant_id, now + ACCESS_TTL))
        conn.execute("INSERT INTO oauth_tokens (token_hash,grant_id,kind,expires_at) VALUES (%s,%s,'refresh',%s)",
                     (digest(refresh), grant_id, now + SESSION_TTL))
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=ACCESS_TTL,
                          refresh_token=refresh, scope=SCOPE)

    @threaded
    def exchange_authorization_code(self, client, authorization_code):
        tokens = None
        with self.connection() as conn:
            lock_publishers(conn)
            row = conn.execute("SELECT * FROM oauth_pending WHERE code_hash=%s AND client_id=%s FOR UPDATE",
                               (digest(authorization_code.code), client.client_id)).fetchone()
            if row and row["expires_at"] > time.time():
                if row["grant_id"]:
                    conn.execute("UPDATE oauth_grants SET revoked=true WHERE id=%s", (row["grant_id"],))
                else:
                    grant_id = uuid4()
                    conn.execute("INSERT INTO oauth_grants (id,publisher_id,client_id,resource,scopes) VALUES (%s,%s,%s,%s,%s)",
                                 (grant_id, row["publisher_id"], client.client_id, self.resource, [SCOPE]))
                    conn.execute("UPDATE oauth_pending SET grant_id=%s WHERE code_hash=%s",
                                 (grant_id, digest(authorization_code.code)))
                    tokens = self.mint_tokens(conn, grant_id)
        if tokens is None:
            raise TokenError("invalid_grant", "Authorization code expired or already used")
        return tokens

    def token_row(self, token, kind, *, include_used=False):
        with self.connection() as conn:
            row = conn.execute(
                "SELECT t.*,g.client_id,g.publisher_id,g.resource,g.scopes FROM oauth_tokens t "
                "JOIN oauth_grants g ON g.id=t.grant_id WHERE t.token_hash=%s AND t.kind=%s "
                "AND t.expires_at>%s AND NOT g.revoked AND g.resource=%s",
                (digest(token), kind, int(time.time()), self.resource),
            ).fetchone()
        return row if row and (include_used or not row["used"]) else None

    @threaded
    def load_access_token(self, token):
        row = self.token_row(token, "access")
        if not row or SCOPE not in row["scopes"]:
            return None
        return AccessToken(token=token, client_id=row["client_id"], scopes=row["scopes"],
                           expires_at=row["expires_at"], resource=row["resource"], subject=str(row["publisher_id"]))

    @threaded
    def load_refresh_token(self, client, refresh_token):
        row = self.token_row(refresh_token, "refresh", include_used=True)
        if not row or row["client_id"] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, client_id=row["client_id"], scopes=row["scopes"],
                            expires_at=row["expires_at"], resource=row["resource"], subject=str(row["publisher_id"]))

    @threaded
    def exchange_refresh_token(self, client, refresh_token, scopes):
        tokens = None
        with self.connection() as conn:
            lock_publishers(conn)
            row = conn.execute(
                "SELECT t.*,g.revoked,g.scopes,g.resource FROM oauth_tokens t JOIN oauth_grants g ON g.id=t.grant_id "
                "WHERE t.token_hash=%s AND t.kind='refresh' AND g.client_id=%s FOR UPDATE OF t,g",
                (digest(refresh_token.token), client.client_id),
            ).fetchone()
            if row and row["expires_at"] > time.time() and not row["revoked"] and row["resource"] == self.resource:
                if row["used"]:
                    conn.execute("UPDATE oauth_grants SET revoked=true WHERE id=%s", (row["grant_id"],))
                elif set(scopes) == {SCOPE} and set(scopes).issubset(row["scopes"]):
                    conn.execute("UPDATE oauth_tokens SET used=true WHERE grant_id=%s AND kind='access'", (row["grant_id"],))
                    conn.execute("UPDATE oauth_tokens SET used=true WHERE token_hash=%s", (digest(refresh_token.token),))
                    tokens = self.mint_tokens(conn, row["grant_id"])
        if tokens is None:
            raise TokenError("invalid_grant", "Refresh token expired, revoked, reused or has invalid scopes")
        return tokens

    @threaded
    def revoke_token(self, token):
        with self.connection() as conn:
            lock_publishers(conn)
            conn.execute("UPDATE oauth_grants SET revoked=true WHERE client_id=%s AND id IN "
                         "(SELECT grant_id FROM oauth_tokens WHERE token_hash=%s)",
                         (token.client_id, digest(token.token)))


def basic_credentials(request):
    header = request.headers.get("authorization", "")
    if not header.startswith("Basic "):
        return None
    try:
        decoded = base64.b64decode(header[6:], validate=True).decode()
        client_id, secret = decoded.split(":", 1)
        return unquote(client_id), unquote(secret)
    except (ValueError, UnicodeDecodeError):
        raise AuthenticationError("Invalid Basic credentials")


class MCPWriteAuthMiddleware:
    """Lazy OAuth: reads stay public; protected calls challenge before MCP responds.

    Claude needs transport-level 401, not a 200 wrapping isError. Tool metadata
    and the adapter's challenge also support clients using per-tool OAuth.
    """
    def __init__(self, app, provider, operator_token):
        self.app, self.provider, self.operator_token = app, provider, operator_token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"].rstrip("/") != "/mcp":
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        authorization = headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        token = token if scheme.lower() == "bearer" else ""
        authenticated = bool(token) and (secrets.compare_digest(token.encode(), self.operator_token.encode())
                                        or await self.provider.load_access_token(token) is not None)
        protected = False
        downstream_receive = receive
        if scope["method"] == "POST":
            body = bytearray()
            while True:
                event = await receive()
                if event["type"] == "http.disconnect":
                    return
                body.extend(event.get("body", b""))
                if len(body) > 1024 * 1024:
                    return await JSONResponse({"error": "Request too large"}, status_code=413)(scope, receive, send)
                if not event.get("more_body", False):
                    break
            try:
                parsed = json.loads(body)
                messages = parsed if isinstance(parsed, list) else [parsed]
                protected = any(isinstance(msg, dict) and msg.get("method") == "tools/call"
                                and isinstance(msg.get("params"), dict)
                                and msg["params"].get("name") in tuple(PROTECTED_TOOLS) for msg in messages)
            except (ValueError, UnicodeDecodeError):
                pass  # Protocol validation remains the MCP SDK's job.
            sent = False

            async def replay():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()
            downstream_receive = replay
        if not authenticated and (protected or authorization):
            challenge = self.provider.challenge()
            response = JSONResponse({"error": "invalid_token", "error_description": "Connect with OAuth to publish",
                                     "_meta": {"mcp/www_authenticate": [challenge]}}, status_code=401,
                                    headers={**SAFE_HEADERS, "WWW-Authenticate": challenge})
            return await response(scope, receive, send)
        async def no_store(event):
            if event["type"] == "http.response.start" and protected:
                event = {**event, "headers": [*event.get("headers", []), (b"cache-control", b"no-store")]}
            await send(event)
        await self.app(scope, downstream_receive, no_store)


class HashedClientAuthenticator:
    """The SDK compares raw secrets; keep only hashes in our database instead."""
    def __init__(self, provider):
        self.provider = provider

    async def authenticate_request(self, request):
        form = await request.form()
        basic = basic_credentials(request)
        client_id = form.get("client_id") or (basic[0] if basic else None)
        if not isinstance(client_id, str):
            raise AuthenticationError("Missing client_id")
        client = await self.provider.get_client(client_id)
        if not client:
            raise AuthenticationError("Unknown OAuth client")
        method = client.token_endpoint_auth_method
        if method == "client_secret_basic":
            if not basic or basic[0] != client_id:
                raise AuthenticationError("Client ID or authentication method mismatch")
            secret = basic[1]
        elif method == "client_secret_post":
            if basic:
                raise AuthenticationError("Use the registered authentication method")
            secret = form.get("client_secret")
        elif method == "none":
            if basic:
                raise AuthenticationError("Use the registered authentication method")
            return client
        else:
            raise AuthenticationError("Unsupported client authentication")
        expected = await self.provider.client_secret_hash(client_id)
        if (not isinstance(secret, str) or not expected or not secrets.compare_digest(digest(secret), expected)
                or (client.client_secret_expires_at and client.client_secret_expires_at <= time.time())):
            raise AuthenticationError("Invalid or expired client credentials")
        return client


def oauth_routes(provider, links):
    registration = ClientRegistrationOptions(enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE])
    revocation = RevocationOptions(enabled=True)
    issuer = AnyHttpUrl(provider.origin)
    documentation = AnyHttpUrl(provider.origin + "/docs")
    routes = create_auth_routes(provider, issuer, documentation, registration, revocation)
    metadata = build_metadata(issuer, documentation, registration, revocation).model_dump(mode="json", exclude_none=True)
    metadata["token_endpoint_auth_methods_supported"] = ["none", "client_secret_post", "client_secret_basic"]
    metadata["revocation_endpoint_auth_methods_supported"] = metadata["token_endpoint_auth_methods_supported"]
    authenticator = HashedClientAuthenticator(provider)
    token_handler = TokenHandler(provider, authenticator)
    revoke_handler = RevocationHandler(provider, authenticator)

    async def metadata_response(request):
        return JSONResponse(metadata, headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"})

    async def exchange(request):
        form = await request.form()
        try:
            basic = basic_credentials(request)
        except AuthenticationError:
            return JSONResponse({"error": "invalid_client"}, status_code=401, headers=SAFE_HEADERS)
        # OAuth Basic clients need not duplicate client_id in the form. The SDK
        # TokenRequest model requires it; normalize the cached parsed form once.
        if basic and not form.get("client_id"):
            request._form = FormData([*form.multi_items(), ("client_id", basic[0])])
            form = request._form
        # SDK 1.30 declares this nullable field as required for revocation,
        # while OAuth public clients legitimately have no client secret.
        if request.url.path == "/revoke" and "client_secret" not in form:
            request._form = FormData([*form.multi_items(), ("client_secret", None)])
            form = request._form
        resource = form.get("resource")
        if resource is not None and resource != provider.resource:
            return JSONResponse({"error": "invalid_target", "error_description": "Wrong PeopleMCP resource"},
                                status_code=400, headers=SAFE_HEADERS)
        if form.get("grant_type") == "authorization_code" and not re.fullmatch(
                r"[A-Za-z0-9._~-]{43,128}", str(form.get("code_verifier", ""))):
            return JSONResponse({"error": "invalid_grant", "error_description": "Invalid PKCE verifier"},
                                status_code=400, headers=SAFE_HEADERS)
        return await (revoke_handler.handle(request) if request.url.path == "/revoke" else token_handler.handle(request))

    async def consent_page(flow, error=None, status=200):
        csrf = secrets.token_urlsafe(32)
        prepared = await provider.prepare_consent(flow, csrf)
        if not prepared:
            return HTMLResponse("Connection request expired. Start again from your MCP client.", status_code=400, headers=SAFE_HEADERS)
        row, client = prepared
        callback_url = str(row["params"]["redirect_uri"])
        callback = html.escape(callback_url)
        parts = urlsplit(callback_url)
        # Use only the validated registered callback's origin. Percent-encode
        # CSP metacharacters (including wildcards); never interpolate its query.
        callback_origin = quote(f"{parts.scheme}://{parts.netloc}", safe=":/[]")
        consent_csp = SAFE_HEADERS["Content-Security-Policy"].replace(
            "form-action 'self';", f"form-action 'self' {callback_origin};")
        name = html.escape(client.get("client_name") or "MCP client")
        banner = f'<p role="alert">{html.escape(error)}</p>' if error else ""
        page = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Connect PeopleMCP</title><style>body{{font:17px system-ui;max-width:600px;margin:8vh auto;padding:24px;background:#10141d;color:#eee}}button{{padding:12px 20px;margin:10px 10px 0 0;cursor:pointer}}code{{overflow-wrap:anywhere}}small{{color:#bbc}}</style>
<h1>Connect PeopleMCP</h1>{banner}<p><strong>{name}</strong> requests permission to publish and edit your own public profiles, projects and agent descriptions.</p>
<p>No GitHub login or password is needed. This browser remembers your publishing access for 90 days. Search stays public.</p>
<p><strong>Only approve if you started this connection.</strong> Client names are unverified. Return address: <code>{callback}</code></p>
<p>Publishing still requires your explicit consent for each publication. This connection cannot edit other publishers' content.</p>
<p><small>This is anonymous access, not verified identity. If you lose both this browser's cookie and your connector credentials, automatic recovery is unavailable.</small></p>
<form method="post" action="/oauth/consent"><input type="hidden" name="flow" value="{html.escape(flow, quote=True)}"><input type="hidden" name="csrf" value="{csrf}">
<p><label>Already connected elsewhere? Enter your one-time PeopleMCP code (optional):<br>
<input name="connection_code" value="" maxlength="64" autocomplete="off" spellcheck="false" placeholder="XXXX-XXXX-XXXX"></label></p>
<p><small>Request it with create_connection_code in your existing authorized chat. The code lasts 5 minutes.
Only use your own code. All connections of this browser's current owner will join the code issuer's owner.</small></p>
<p><label><input type="checkbox" name="confirm_merge_publications" value="true">
I also explicitly agree to transfer this browser owner's existing publications, if any. No content will be deleted.</label></p>
<button name="decision" value="allow">Allow / Разрешить</button><button name="decision" value="deny">Cancel / Отмена</button></form></html>'''
        # no-referrer makes browser form POSTs send Origin: null, which our CSRF
        # origin check correctly rejects. Send only the origin (never the flow
        # query string); redirects and other OAuth responses stay no-referrer.
        # Chromium applies form-action to the 303 callback redirect as well.
        response = HTMLResponse(page, status_code=status, headers={
            **SAFE_HEADERS, "Referrer-Policy": "strict-origin", "Content-Security-Policy": consent_csp})
        response.set_cookie(provider.csrf_cookie, csrf, max_age=FLOW_TTL, secure=provider.secure,
                            httponly=True, samesite="lax", path="/")
        return response

    async def consent(request: Request):
        if request.method == "GET":
            return await consent_page(request.query_params.get("flow", ""))
        form = await request.form()
        csrf = form.get("csrf", "")
        cookie = request.cookies.get(provider.csrf_cookie, "")
        if (request.headers.get("origin") not in {None, provider.origin}
                or not isinstance(csrf, str) or not csrf or not cookie
                or not secrets.compare_digest(csrf.encode(), cookie.encode())
                or form.get("decision") not in ("allow", "deny")):
            return JSONResponse({"error": "Invalid consent request"}, status_code=403, headers=SAFE_HEADERS)
        flow = form.get("flow")
        connection_code = form.get("connection_code", "")
        if not isinstance(flow, str) or not isinstance(connection_code, str) or len(connection_code) > 64:
            return JSONResponse({"error": "Invalid consent request"}, status_code=400, headers=SAFE_HEADERS)
        result = await provider.finish_consent(flow, csrf, request.cookies.get(provider.browser_cookie), form["decision"] == "allow",
            connection_code.strip(), form.get("confirm_merge_publications") == "true", links)
        if isinstance(result, dict):
            return await consent_page(flow, result["detail"], result["http_status"])
        if not result:
            return JSONResponse({"error": "Expired or already used consent request"}, status_code=400, headers=SAFE_HEADERS)
        location, browser_secret = result
        response = RedirectResponse(location, status_code=303, headers=SAFE_HEADERS)
        response.delete_cookie(provider.csrf_cookie, path="/", secure=provider.secure, httponly=True, samesite="lax")
        if browser_secret:
            response.set_cookie(provider.browser_cookie, browser_secret, max_age=SESSION_TTL,
                                secure=provider.secure, httponly=True, samesite="lax", path="/")
        return response

    for index, route in enumerate(routes):
        if route.path == "/.well-known/oauth-authorization-server":
            routes[index] = Route(route.path, metadata_response, methods=["GET", "OPTIONS"])
        elif route.path in {"/token", "/revoke"}:
            endpoint = CORSMiddleware(RequestBodyLimitMiddleware(request_response(exchange), 65536),
                                      allow_origins=["*"], allow_methods=["POST", "OPTIONS"],
                                      allow_headers=["Authorization", "Content-Type"])
            routes[index] = Route(route.path, endpoint, methods=["POST", "OPTIONS"])
    routes += create_protected_resource_routes(AnyHttpUrl(provider.resource), [issuer], [SCOPE], "PeopleMCP", documentation)
    # Support both RFC path-specific discovery and clients probing the origin.
    resource_route = routes[-1]
    routes.append(Route("/.well-known/oauth-protected-resource", resource_route.app, methods=["GET", "OPTIONS"]))
    routes.append(Route("/oauth/consent", RequestBodyLimitMiddleware(request_response(consent), 16384), methods=["GET", "POST"]))
    return routes
