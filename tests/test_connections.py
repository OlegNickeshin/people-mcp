"""Real DB/API/MCP checks for private linking codes; synthetic credentials only."""
import asyncio
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit

import httpx
import psycopg
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from server.config import Settings
from server.linking import normalize_code
from server.oauth import PublisherOAuth, digest
from tests import test_oauth as helpers
from tests.test_smoke import BASE, TOKEN


class ConnectionSmoke(unittest.TestCase):
    setUp = helpers.OAuthSmoke.setUp
    tearDown = helpers.OAuthSmoke.tearDown
    register = helpers.OAuthSmoke.register
    start = helpers.OAuthSmoke.start
    consent = helpers.OAuthSmoke.consent
    token = helpers.OAuthSmoke.token
    login = helpers.OAuthSmoke.login
    create = helpers.OAuthSmoke.create

    def separate(self):
        browser = httpx.Client(timeout=120, follow_redirects=False)
        self.addCleanup(browser.close)
        client, tokens = self.login(browser)
        return client, tokens, browser

    def headers(self, tokens):
        return {"Authorization": "Bearer " + tokens["access_token"]}

    def owner(self, tokens):
        access = asyncio.run(PublisherOAuth(Settings()).load_access_token(tokens["access_token"]))
        return access.subject if access else None

    def issue(self, tokens):
        response = self.http.post("/connections/code", json={"confirm": True}, headers=self.headers(tokens))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json()["expires_in"], 300)
        self.assertTrue(response.json()["single_use"])
        return response.json()["code"]

    def redeem(self, tokens, code, **extra):
        return self.http.post("/connections/redeem", json={"code": code, "confirm": True, **extra}, headers=self.headers(tokens))

    def test_personal_auth_explicit_consent_and_hash_only(self):
        _, tokens = self.login()
        for path, payload in (("/connections/code", {"confirm": True}),
                              ("/connections/redeem", {"confirm": True, "code": "NOT-A-CODE"})):
            for headers, expected in (({}, 401), ({"Authorization": "Bearer invalid"}, 401),
                                      ({"Authorization": "Bearer " + TOKEN}, 403)):
                self.assertEqual(self.http.post(path, json=payload, headers=headers).status_code, expected)
        self.assertEqual(self.http.post("/connections/code", json={"confirm": False}, headers=self.headers(tokens)).status_code, 422)
        code = self.issue(tokens)
        with psycopg.connect(Settings().database_url) as conn:
            rows = conn.execute("SELECT code_hash FROM oauth_connection_codes WHERE publisher_id=%s", (self.owner(tokens),)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0][0] == digest(normalize_code(code)))
        self.assertFalse(rows[0][0] == code)
        # The mounted MCP router may turn a partial FastAPI method match into 404.
        self.assertIn(self.http.get("/connections/code").status_code, (404, 405))

    def test_empty_duplicate_rebinds_all_connections_without_deleting_content(self):
        _, source = self.login()
        record = self.create(source)
        target_client, target, browser = self.separate()
        _, target_second = self.login(browser)
        original_owner, duplicate = self.owner(source), self.owner(target)
        stale_code = self.issue(target)
        code = self.issue(source)
        response = self.redeem(target, code.lower().replace("-", " "))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "linked")
        for tokens in (source, target, target_second):
            self.assertEqual(self.owner(tokens), original_owner)
            changed = self.http.patch("/profiles/" + record["id"], json={"contact": "oauth-test@example.org", "publish": True},
                                      headers=self.headers(tokens))
            self.assertEqual(changed.status_code, 200)
        with psycopg.connect(Settings().database_url) as conn:
            self.assertIsNone(conn.execute("SELECT id FROM oauth_publishers WHERE id=%s", (duplicate,)).fetchone())
        self.assertEqual(self.http.get("/profiles/" + record["id"]).json(), record)
        _, reconnected = self.login(browser)
        self.assertEqual(self.owner(reconnected), original_owner)
        refreshed = self.token(target_client, grant_type="refresh_token", refresh_token=target["refresh_token"])
        self.assertEqual(refreshed.status_code, 200)
        self.assertEqual(self.owner(refreshed.json()), original_owner)
        _, outsider, _ = self.separate()
        self.assertEqual(self.redeem(outsider, stale_code).status_code, 400)
        self.assertEqual(self.redeem(outsider, code).status_code, 400)
        self.assertNotEqual(self.owner(outsider), original_owner)

    def test_nonempty_duplicate_requires_separate_confirmation_preserves_every_record(self):
        _, source = self.login()
        original = self.create(source)
        _, target, _ = self.separate()
        records = [self.create(target, kind) for kind in ("profiles", "projects")]
        target_owner = self.owner(target)
        code = self.issue(source)
        denied = self.redeem(target, code)
        self.assertEqual(denied.status_code, 409)
        self.assertEqual(denied.json()["error"], "merge_confirmation_required")
        self.assertEqual(denied.json()["publications_to_transfer"], {"profiles": 1, "projects": 1})
        self.assertEqual(self.owner(target), target_owner)
        self.assertEqual(self.redeem(target, code, confirm_merge_publications="true").status_code, 422)
        accepted = self.redeem(target, code, confirm_merge_publications=True)
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(self.owner(target), self.owner(source))
        for kind, record in (("profiles", original), ("profiles", records[0]), ("projects", records[1])):
            self.assertEqual(self.http.get(f"/{kind}/{record['id']}").json(), record)
            with psycopg.connect(Settings().database_url) as conn:
                table = "profile_chunks" if kind == "profiles" else "project_chunks"
                owner = "profile_id" if kind == "profiles" else "project_id"
                self.assertGreater(conn.execute(f"SELECT count(*) FROM {table} WHERE {owner}=%s", (record["id"],)).fetchone()[0], 0)

    def test_replacement_expiration_revocation_and_rate_limits(self):
        client, source = self.login()
        _, target, _ = self.separate()
        first = self.issue(source)
        code = self.issue(source)
        self.assertEqual(self.redeem(target, first).status_code, 400)
        with psycopg.connect(Settings().database_url) as conn:
            conn.execute("UPDATE oauth_connection_codes SET expires_at=%s WHERE code_hash=%s",
                         (int(time.time())-1, digest(normalize_code(code))))
        self.assertEqual(self.redeem(target, code).status_code, 400)
        code = self.issue(source)
        revoked = self.http.post("/revoke", data={"client_id": client["client_id"], "token": source["refresh_token"]})
        self.assertEqual(revoked.status_code, 200)
        self.assertEqual(self.redeem(target, code).status_code, 400)
        for _ in range(2):
            self.assertEqual(self.redeem(target, "INVALID").status_code, 400)
        blocked = self.redeem(target, "INVALID")
        self.assertEqual(blocked.status_code, 429)
        self.assertIn("retry-after", blocked.headers)
        _, fresh, _ = self.separate()
        for _ in range(5):
            self.issue(fresh)
        self.assertEqual(self.http.post("/connections/code", json={"confirm": True}, headers=self.headers(fresh)).status_code, 429)

    def test_concurrent_redemption_has_exactly_one_winner(self):
        _, source = self.login()
        _, first, _ = self.separate()
        _, second, _ = self.separate()
        code = self.issue(source)
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda tokens: self.redeem(tokens, code), (first, second)))
        self.assertEqual(sorted(r.status_code for r in responses), [200, 400])
        self.assertEqual(sum(self.owner(t) == self.owner(source) for t in (first, second)), 1)

    def test_web_consent_links_fresh_browser_and_cancel_does_not_consume_code(self):
        _, source = self.login()
        code = self.issue(source)
        with httpx.Client(timeout=120, follow_redirects=False) as browser:
            client = self.register()
            form, _ = self.start(client, browser)
            denied = browser.post(self.origin + "/oauth/consent", data={**form, "connection_code": code, "decision": "deny"},
                                  headers={"Origin": self.origin})
            self.assertEqual(denied.status_code, 303)
            form, verifier = self.start(client, browser)
            allowed = browser.post(self.origin + "/oauth/consent", data={**form, "connection_code": code, "decision": "allow"},
                                   headers={"Origin": self.origin})
            self.assertEqual(allowed.status_code, 303)
            self.assertNotIn(code, allowed.headers["location"])
            auth_code = parse_qs(urlsplit(allowed.headers["location"]).query)["code"][0]
            tokens = self.token(client, grant_type="authorization_code", code=auth_code, code_verifier=verifier)
            self.assertEqual(tokens.status_code, 200)
            self.assertEqual(self.owner(tokens.json()), self.owner(source))

    def test_web_nonempty_confirmation_csrf_and_error_page(self):
        _, source = self.login()
        _, target, browser = self.separate()
        record = self.create(target, "projects")
        code = self.issue(source)
        client = self.register()
        form, verifier = self.start(client, browser)
        path = self.origin + "/oauth/consent"
        self.assertEqual(browser.post(path, data={**form, "csrf": "invalid", "connection_code": code, "decision": "allow"}).status_code, 403)
        response = browser.post(path, data={**form, "connection_code": code, "decision": "allow"}, headers={"Origin": self.origin})
        self.assertEqual(response.status_code, 409)
        self.assertNotIn(code, response.text)
        form = helpers.Inputs(response.text).values
        approved = browser.post(path, data={**form, "connection_code": code, "decision": "allow", "confirm_merge_publications": "true"},
                                headers={"Origin": self.origin})
        self.assertEqual(approved.status_code, 303)
        auth_code = parse_qs(urlsplit(approved.headers["location"]).query)["code"][0]
        self.assertEqual(self.token(client, grant_type="authorization_code", code=auth_code, code_verifier=verifier).status_code, 200)
        self.assertEqual(self.owner(source), self.owner(target))
        self.assertEqual(self.http.get("/projects/" + record["id"]).json(), record)

    def test_web_attempt_limit_survives_reloading_consent(self):
        with httpx.Client(timeout=120, follow_redirects=False) as browser:
            client = self.register()
            form, _ = self.start(client, browser)
            for expected in [400]*5 + [429]:
                response = browser.post(self.origin + "/oauth/consent", data={**form, "connection_code": "INVALID", "decision": "allow"},
                                        headers={"Origin": self.origin})
                self.assertEqual(response.status_code, expected)
                form = helpers.Inputs(response.text).values

    def test_mcp_issue_and_redeem_without_reconnecting(self):
        _, source = self.login()
        _, target, _ = self.separate()

        async def call(tokens, name, arguments):
            async with httpx.AsyncClient(headers=self.headers(tokens), timeout=120) as http:
                async with streamable_http_client(BASE + "/mcp", http_client=http) as (reader, writer, _):
                    async with ClientSession(reader, writer) as session:
                        await session.initialize()
                        return await session.call_tool(name, arguments)
        issued = asyncio.run(call(source, "create_connection_code", {"confirm": True}))
        self.assertFalse(issued.isError)
        redeemed = asyncio.run(call(target, "redeem_connection_code", {"confirm": True, "code": issued.structuredContent["code"]}))
        self.assertFalse(redeemed.isError)
        self.assertEqual(self.owner(source), self.owner(target))
        for name in ("create_connection_code", "redeem_connection_code"):
            response = self.http.post("/mcp", headers={"Accept": "application/json, text/event-stream"},
                json={"jsonrpc": "2.0", "id": 99, "method": "tools/call", "params": {"name": name, "arguments": {"confirm": True}}})
            self.assertEqual(response.status_code, 401)
            self.assertIn("resource_metadata", response.headers["www-authenticate"])
