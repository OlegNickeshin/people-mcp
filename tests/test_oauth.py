"""Real OAuth, PKCE and publisher isolation tests. No credentials printed."""
import asyncio
import base64
import hashlib
import logging
import secrets
import time
import unittest
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, urlsplit
from uuid import uuid4

import httpx
import psycopg
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientMetadata

from server.config import Settings
from server.oauth import PublisherOAuth, digest
from tests.test_smoke import BASE, delete_created


class Inputs(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.values = {}
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("type") != "checkbox":
            self.values[attrs["name"]] = attrs["value"]


class OAuthSmoke(unittest.TestCase):
    def setUp(self):
        logging.getLogger("httpx").setLevel(logging.WARNING)
        self.http = httpx.Client(base_url=BASE, timeout=120, follow_redirects=False)
        self.clients, self.created = [], []
        self.resource = self.http.get("/.well-known/oauth-protected-resource/mcp").json()["resource"]
        self.origin = self.resource.removesuffix("/mcp")

    def tearDown(self):
        delete_created(self.created)
        with psycopg.connect(Settings().database_url) as conn:
            owners = conn.execute("SELECT DISTINCT publisher_id FROM oauth_pending WHERE client_id=ANY(%s) "
                                  "AND publisher_id IS NOT NULL", (self.clients,)).fetchall()
            conn.execute("DELETE FROM oauth_clients WHERE client_id=ANY(%s)", (self.clients,))
            for owner in owners:
                conn.execute("DELETE FROM oauth_publishers WHERE id=%s", owner)
        self.http.close()

    def register(self, method="none", **extra):
        response = self.http.post("/register", json={
            "client_name": "PeopleMCP synthetic OAuth test", "redirect_uris": ["https://client.example.org/callback"],
            "token_endpoint_auth_method": method, "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "scope": "publish", **extra,
        })
        self.assertEqual(response.status_code, 201)
        client = response.json()
        self.clients.append(client["client_id"])
        return client

    def start(self, client, browser=None, **extra):
        browser = browser or self.http
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        params = {"response_type": "code", "client_id": client["client_id"], "redirect_uri": client["redirect_uris"][0],
                  "scope": "publish", "state": "test-state", "resource": self.resource,
                  "code_challenge": challenge, "code_challenge_method": "S256", **extra}
        response = browser.get(self.origin + "/authorize", params=params)
        self.assertEqual(response.status_code, 302)
        location = response.headers["location"]
        self.assertTrue(location.startswith(self.origin + "/oauth/consent?"))
        page = browser.get(location)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.headers["referrer-policy"], "strict-origin")
        callback = urlsplit(client["redirect_uris"][0])
        callback_origin = quote(f"{callback.scheme}://{callback.netloc}", safe=":/[]")
        self.assertIn(f"form-action 'self' {callback_origin};", page.headers["content-security-policy"])
        self.assertIn("frame-ancestors 'none'", page.headers["content-security-policy"])
        self.assertIn("HttpOnly", page.headers["set-cookie"])
        if self.origin.startswith("https:"):
            self.assertIn("Secure", page.headers["set-cookie"])
        return Inputs(page.text).values, verifier

    def consent(self, client, browser=None, decision="allow"):
        browser = browser or self.http
        form, verifier = self.start(client, browser)
        response = browser.post(self.origin + "/oauth/consent", data={**form, "decision": decision},
                                headers={"Origin": self.origin})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
        params = parse_qs(urlsplit(response.headers["location"]).query)
        self.assertEqual(params["state"], ["test-state"])
        return params, verifier

    def token(self, client, **data):
        fields = {"client_id": client["client_id"], "resource": self.resource, **data}
        if fields.get("grant_type") == "authorization_code":
            fields.setdefault("redirect_uri", client["redirect_uris"][0])
        auth = None
        if client["token_endpoint_auth_method"] == "client_secret_post":
            fields["client_secret"] = client["client_secret"]
        elif client["token_endpoint_auth_method"] == "client_secret_basic":
            fields.pop("client_id")
            auth = (client["client_id"], client["client_secret"])
        return self.http.post("/token", data=fields, auth=auth)

    def login(self, browser=None, method="none"):
        client = self.register(method)
        params, verifier = self.consent(client, browser)
        response = self.token(client, grant_type="authorization_code", code=params["code"][0], code_verifier=verifier)
        self.assertEqual(response.status_code, 200)
        return client, response.json()

    def create(self, tokens, kind="profiles"):
        payload = {"slug": "oauth-test-" + uuid4().hex, "content": "Synthetic test of ocean ecology collaboration.",
                   "contact": "oauth-test@example.org", "publish": True}
        response = self.http.post(f"/{kind}", json=payload, headers={"Authorization": "Bearer " + tokens["access_token"]})
        self.assertEqual(response.status_code, 201)
        self.created.append((kind, response.json()["id"]))
        return response.json()

    def test_discovery_and_anonymous_write_challenge(self):
        metadata = self.http.get("/.well-known/oauth-authorization-server").json()
        self.assertEqual(metadata["code_challenge_methods_supported"], ["S256"])
        self.assertIn("none", metadata["token_endpoint_auth_methods_supported"])
        self.assertEqual(self.http.get("/.well-known/oauth-protected-resource").json()["resource"], self.resource)
        denied = self.http.post("/profiles", json={"slug": "never-create", "content": "Public", "contact": "x", "publish": True})
        self.assertEqual(denied.status_code, 401)
        self.assertIn('resource_metadata="', denied.headers["www-authenticate"])
        self.assertEqual(self.http.get("/profiles/demo-oleg-mcp").status_code, 200)

    def test_owner_isolation_same_browser_restore_and_hash_storage(self):
        client, tokens = self.login()
        _, restored = self.login()  # A different connector, same browser owner.
        with httpx.Client(timeout=120, follow_redirects=False) as other_browser:
            _, stranger = self.login(other_browser)
        for kind in ("profiles", "projects"):
            record = self.create(tokens, kind)
            path = f"/{kind}/{record['id']}"
            patch = {"content": "Synthetic test of ocean ecology collaboration.", "publish": True}
            for credential, status in ((stranger, 403), (restored, 200), (tokens, 200)):
                self.assertEqual(self.http.patch(path, json=patch, headers={
                    "Authorization": "Bearer " + credential["access_token"]}).status_code, status)
            self.assertNotIn("publisher_id", self.http.get(path).json())
        denied = self.http.patch("/profiles/demo-oleg-mcp", json={"contact": "cannot-claim@example.org", "publish": True},
                                headers={"Authorization": "Bearer " + tokens["access_token"]})
        self.assertEqual(denied.status_code, 403)
        with psycopg.connect(Settings().database_url) as conn:
            hashes = conn.execute("SELECT token_hash FROM oauth_tokens WHERE grant_id IN "
                                  "(SELECT id FROM oauth_grants WHERE client_id=%s)", (client["client_id"],)).fetchall()
        self.assertIn((digest(tokens["access_token"]),), hashes)
        self.assertNotIn((tokens["access_token"],), hashes)
        self.assertIsNotNone(asyncio.run(PublisherOAuth(Settings()).load_access_token(tokens["access_token"])))

    def test_pkce_resource_binding_and_code_replay(self):
        client = self.register()
        params, verifier = self.consent(client)
        args = {"grant_type": "authorization_code", "code": params["code"][0], "code_verifier": verifier}
        self.assertEqual(self.token(client, **{**args, "code_verifier": "x" * 64}).status_code, 400)
        self.assertEqual(self.token(client, **{**args, "resource": "https://wrong.example.org/mcp"}).status_code, 400)
        other = self.register()
        self.assertEqual(self.token(other, **args).status_code, 400)
        response = self.token(client, **args)
        self.assertEqual(response.status_code, 200)
        access = response.json()["access_token"]
        self.assertEqual(self.token(client, **args).status_code, 400)
        self.assertIsNone(asyncio.run(PublisherOAuth(Settings()).load_access_token(access)))

    def test_refresh_rotation_replay_and_revocation(self):
        client, tokens = self.login()
        args = {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]}
        self.assertEqual(self.token(client, **{**args, "resource": "https://wrong.example.org/mcp"}).status_code, 400)
        rotated = self.token(client, **args)
        self.assertEqual(rotated.status_code, 200)
        self.assertNotEqual(tokens["refresh_token"], rotated.json()["refresh_token"])
        provider = PublisherOAuth(Settings())
        self.assertIsNone(asyncio.run(provider.load_access_token(tokens["access_token"])))
        self.assertIsNotNone(asyncio.run(provider.load_access_token(rotated.json()["access_token"])))
        self.assertEqual(self.token(client, **args).status_code, 400)
        self.assertIsNone(asyncio.run(provider.load_access_token(rotated.json()["access_token"])))
        client, tokens = self.login()
        self.assertEqual(self.http.post("/revoke", data={"client_id": client["client_id"],
                                                        "token": tokens["refresh_token"]}).status_code, 200)
        self.assertIsNone(asyncio.run(provider.load_access_token(tokens["access_token"])))

    def test_csrf_origin_deny_and_expiration(self):
        client = self.register()
        form, _ = self.start(client)
        for data, origin in (({**form, "csrf": "wrong", "decision": "allow"}, self.origin),
                             ({**form, "decision": "allow"}, "null"),
                             ({**form, "decision": "allow"}, "https://evil.example.org")):
            self.assertEqual(self.http.post(self.origin + "/oauth/consent", data=data,
                                           headers={"Origin": origin}).status_code, 403)
        with httpx.Client(timeout=30) as without_cookie:
            self.assertEqual(without_cookie.post(self.origin + "/oauth/consent",
                data={**form, "decision": "allow"}, headers={"Origin": self.origin}).status_code, 403)
        params, _ = self.consent(client, decision="deny")
        self.assertEqual(params["error"], ["access_denied"])
        self.assertNotIn("code", params)
        params, verifier = self.consent(client)
        with psycopg.connect(Settings().database_url) as conn:
            conn.execute("UPDATE oauth_pending SET expires_at=%s WHERE code_hash=%s",
                         (int(time.time()) - 1, digest(params["code"][0])))
        self.assertEqual(self.token(client, grant_type="authorization_code", code=params["code"][0],
                                    code_verifier=verifier).status_code, 400)

    def test_callback_query_is_not_interpolated_into_consent_policy(self):
        client = self.register(redirect_uris=[
            "https://client.example.org:8443/callback?next=https://unrelated.example.org/&policy=script-src%20*"
        ])
        form, _ = self.start(client)
        page = self.http.get(self.origin + "/oauth/consent", params={"flow": form["flow"]})
        policy = page.headers["content-security-policy"]
        self.assertIn("form-action 'self' https://client.example.org:8443;", policy)
        self.assertNotIn("unrelated.example.org", policy)
        self.assertNotIn("script-src", policy)
        self.assertNotIn("*", policy)

    def test_confidential_clients_and_registration_validation(self):
        for method in ("client_secret_post", "client_secret_basic"):
            client, tokens = self.login(method=method)
            with psycopg.connect(Settings().database_url) as conn:
                metadata, secret_hash = conn.execute("SELECT metadata,secret_hash FROM oauth_clients WHERE client_id=%s",
                                                     (client["client_id"],)).fetchone()
            self.assertNotIn("client_secret", metadata)
            self.assertEqual(secret_hash, digest(client["client_secret"]))
            denied = self.token({**client, "client_secret": "wrong"}, grant_type="refresh_token", refresh_token=tokens["refresh_token"])
            self.assertEqual(denied.status_code, 401)
        for uri in ("http://evil.example.org/callback", "https://good.example.org/#fragment", "https://user:pass@example.org/"):
            self.assertEqual(self.http.post("/register", json={"redirect_uris": [uri],
                "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]}).status_code, 400)

    def test_mcp_oauth_challenge_and_personal_publication(self):
        _, tokens = self.login()

        async def exercise():
            for token in (None, tokens["access_token"]):
                headers = {"Authorization": "Bearer " + token} if token else {}
                async with httpx.AsyncClient(headers=headers, timeout=120) as http:
                    async with streamable_http_client(BASE + "/mcp", http_client=http) as (reader, writer, _):
                        async with ClientSession(reader, writer) as session:
                            await session.initialize()
                            for tool in (await session.list_tools()).tools:
                                schemes = tool.model_dump(by_alias=True)["securitySchemes"]
                                self.assertEqual(schemes[0]["type"], "noauth" if tool.annotations.readOnlyHint else "oauth2")
                            payload = {"slug": "oauth-test-" + uuid4().hex, "content": "Synthetic open source ecology project.",
                                       "contact": "oauth-test@example.org", "publish": True}
                            if token:
                                response = await session.call_tool("upsert_profile", payload)
                                self.assertFalse(response.isError)
                                self.created.append(("profiles", response.structuredContent["id"]))
                                repeat = await session.call_tool("upsert_profile", payload)
                                self.assertEqual(repeat.structuredContent["id"], response.structuredContent["id"])
                            else:
                                response = await http.post(BASE + "/mcp", json={"jsonrpc": "2.0", "id": 99,
                                    "method": "tools/call", "params": {"name": "upsert_profile", "arguments": payload}},
                                    headers={"Accept": "application/json, text/event-stream"})
                                self.assertEqual(response.status_code, 401)
                                self.assertIn("resource_metadata", response.headers["www-authenticate"])
                                self.assertIn("mcp/www_authenticate", response.json()["_meta"])
        asyncio.run(exercise())

    def test_standard_sdk_automatically_authorizes_and_retries(self):
        outer = self

        class MemoryStorage:
            tokens = None
            client = None

            async def get_tokens(self):
                return self.tokens

            async def set_tokens(self, tokens):
                self.tokens = tokens

            async def get_client_info(self):
                return self.client

            async def set_client_info(self, client):
                self.client = client
                outer.clients.append(client.client_id)

        async def exercise():
            storage = MemoryStorage()
            callback = None
            async with httpx.AsyncClient(timeout=120, follow_redirects=False) as browser:
                async def redirect(url):
                    nonlocal callback
                    response = await browser.get(url)
                    self.assertEqual(response.status_code, 302)
                    page = await browser.get(response.headers["location"])
                    form = Inputs(page.text).values
                    approved = await browser.post(self.origin + "/oauth/consent", data={**form, "decision": "allow"},
                                                  headers={"Origin": self.origin})
                    self.assertEqual(approved.status_code, 303)
                    query = parse_qs(urlsplit(approved.headers["location"]).query)
                    callback = (query["code"][0], query["state"][0])

                async def receive_callback():
                    return callback

                auth = OAuthClientProvider(server_url=self.resource, storage=storage, redirect_handler=redirect,
                    callback_handler=receive_callback, client_metadata=OAuthClientMetadata(
                        client_name="PeopleMCP SDK synthetic test", redirect_uris=["https://client.example.org/callback"],
                        grant_types=["authorization_code", "refresh_token"], response_types=["code"],
                        token_endpoint_auth_method="none", scope="publish"))
                async with httpx.AsyncClient(auth=auth, timeout=120) as http:
                    async with streamable_http_client(self.resource, http_client=http) as (reader, writer, _):
                        async with ClientSession(reader, writer) as session:
                            await session.initialize()
                            self.assertIsNone(storage.tokens)  # Public setup did not force consent.
                            payload = {"slug": "oauth-test-" + uuid4().hex, "content": "Synthetic SDK OAuth publishing test.",
                                       "contact": "oauth-test@example.org", "publish": True}
                            response = await session.call_tool("upsert_project", payload)
                            self.assertFalse(response.isError)
                            self.created.append(("projects", response.structuredContent["id"]))
                            self.assertIsNotNone(storage.tokens)
                            old_refresh = storage.tokens.refresh_token
                            with psycopg.connect(Settings().database_url) as conn:
                                conn.execute("UPDATE oauth_tokens SET expires_at=0 WHERE token_hash=%s",
                                             (digest(storage.tokens.access_token),))
                            repeated = await session.call_tool("upsert_project", payload)
                            self.assertFalse(repeated.isError)
                            self.assertEqual(repeated.structuredContent["id"], response.structuredContent["id"])
                            self.assertNotEqual(old_refresh, storage.tokens.refresh_token)
        asyncio.run(exercise())
