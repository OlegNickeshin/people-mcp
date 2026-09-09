"""Optional Chromium regression tests; run only against a disposable test database.

Install tests/requirements-browser.txt and Playwright's Chromium separately.
No browser dependency is included in the production image or default smoke suite.
"""
import base64
import hashlib
import os
import re
import secrets
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import psycopg
from playwright.sync_api import sync_playwright


class BrowserConsent(unittest.TestCase):
    def setUp(self):
        self.database_url = os.environ["DATABASE_URL"]
        self.http = httpx.Client(base_url=os.environ.get("API_BASE_URL", "http://127.0.0.1:8000"), timeout=30)
        self.resource = self.http.get("/.well-known/oauth-protected-resource/mcp").json()["resource"]
        self.origin = self.resource.removesuffix("/mcp")
        self.clients = []
        self.addCleanup(self.cleanup_clients)
        self.runtime = sync_playwright().start()
        self.addCleanup(self.runtime.stop)
        self.browser = self.runtime.chromium.launch()
        self.addCleanup(self.browser.close)

    def cleanup_clients(self):
        with psycopg.connect(self.database_url) as conn:
            owners = conn.execute("SELECT DISTINCT publisher_id FROM oauth_pending WHERE client_id=ANY(%s) "
                                  "AND publisher_id IS NOT NULL", (self.clients,)).fetchall()
            conn.execute("DELETE FROM oauth_clients WHERE client_id=ANY(%s)", (self.clients,))
            for owner in owners:
                conn.execute("DELETE FROM oauth_publishers WHERE id=%s", owner)
        self.http.close()

    def check_submission(self, decision, *, recreate_old_policy=False):
        callbacks, callback_referrers = [], []

        class CallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                callbacks.append(parse_qs(urlsplit(self.path).query))
                callback_referrers.append(self.headers.get("Referer"))
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(b"<h1>OAuth callback received</h1>")

            def log_message(self, *args):
                pass  # Never log authorization codes or callback URLs.

        listener = ThreadingHTTPServer(("127.0.0.1", 0), CallbackHandler)
        self.addCleanup(listener.server_close)
        self.addCleanup(listener.shutdown)
        Thread(target=listener.serve_forever, daemon=True).start()
        callback = f"http://127.0.0.1:{listener.server_port}/callback"
        registration = self.http.post("/register", json={
            "client_name": "PeopleMCP synthetic browser consent test", "redirect_uris": [callback],
            "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "scope": "publish",
        })
        self.assertEqual(registration.status_code, 201)
        client_id = registration.json()["client_id"]
        self.clients.append(client_id)
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        authorize = self.origin + "/authorize?" + urlencode({
            "response_type": "code", "client_id": client_id, "redirect_uri": callback,
            "scope": "publish", "state": "browser-regression", "resource": self.resource,
            "code_challenge": challenge, "code_challenge_method": "S256",
        })
        page = self.browser.new_page()
        self.addCleanup(page.close)
        origins, statuses, policy_errors = [], [], []
        if recreate_old_policy:
            def old_policy(route):
                response = route.fetch()
                route.fulfill(response=response, headers={**response.headers, "referrer-policy": "no-referrer"})
            page.route(self.origin + "/oauth/consent?*", old_policy)

        def observe_request(request):
            if request.method == "POST" and request.url == self.origin + "/oauth/consent":
                origins.append(request.headers.get("origin"))

        def observe_response(response):
            if response.request.method == "POST" and response.url == self.origin + "/oauth/consent":
                statuses.append(response.status)

        page.on("request", observe_request)
        page.on("response", observe_response)
        page.on("console", lambda message: policy_errors.append("form-action CSP violation")
                if "form-action" in message.text else None)
        redirect = self.http.get(authorize)
        self.assertEqual(redirect.status_code, 302)
        # Interception applies to this navigation, not an earlier redirect chain.
        page.goto(redirect.headers["location"])
        self.assertEqual(page.get_by_role("button").all_text_contents(), ["Allow", "Cancel"])
        page.get_by_role("button", name="Allow" if decision == "allow" else "Cancel", exact=True).click()
        if recreate_old_policy:
            self.assertEqual(origins, ["null"])
            self.assertEqual(statuses, [403])
            self.assertIn("Invalid consent request", page.locator("body").inner_text())
            self.assertEqual(callbacks, [])
            return
        page.get_by_role("heading", name="OAuth callback received").wait_for(timeout=10000)
        self.assertEqual(origins, [self.origin], "The browser must send its real Origin without test overrides")
        self.assertEqual(statuses, [303], str(policy_errors))
        self.assertEqual(len(callbacks), 1, "Browser must follow the redirect; CSP must not block the OAuth callback")
        self.assertEqual(callback_referrers, [None], "The callback must not receive the consent flow as a referrer")
        params = callbacks[0]
        self.assertEqual(params["state"], ["browser-regression"])
        if decision == "deny":
            self.assertEqual(params["error"], ["access_denied"])
            self.assertNotIn("code", params)
        else:
            exchange = self.http.post("/token", data={
                "grant_type": "authorization_code", "client_id": client_id, "code": params["code"][0],
                "code_verifier": verifier, "redirect_uri": callback, "resource": self.resource,
            })
            self.assertEqual(exchange.status_code, 200)
            self.assertTrue("access_token" in exchange.json())

    def test_old_no_referrer_policy_reproduces_browser_rejection(self):
        self.check_submission("allow", recreate_old_policy=True)

    def test_allow_redirect_and_code_exchange(self):
        self.check_submission("allow")

    def test_cancel_redirect(self):
        self.check_submission("deny")


class RedactedResult(unittest.TextTestResult):
    def _exc_info_to_string(self, err, test):
        # Playwright navigation failures can include URLs with OAuth credentials.
        return re.sub(r"(https?://[^\s?'\"<>]+)\?[^\s'\"<>]+", r"\1?[redacted]",
                      super()._exc_info_to_string(err, test))


if __name__ == "__main__":
    unittest.main(testRunner=unittest.TextTestRunner(verbosity=2, resultclass=RedactedResult))
