"""One-time codes link consenting publishers; OAuth tokens stay in their clients."""
import re
import secrets
import time
from datetime import datetime, timezone

from psycopg import sql

from server.db import TABLES, lock_publishers
from server.oauth import digest, threaded

LINK_TTL = 300
MAX_ATTEMPTS = 5
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 32 symbols, 12 characters = 60 bits.


def failure(status, error, detail, **extra):
    return {"http_status": status, "error": error, "detail": detail, **extra}


def normalize_code(code):
    return re.sub(r"[\s-]", "", code).upper()


class PublisherLinks:
    def __init__(self, oauth):
        self.oauth = oauth

    def principal(self, conn, token):
        return conn.execute(
            "SELECT g.publisher_id,g.id AS grant_id FROM oauth_tokens t JOIN oauth_grants g ON g.id=t.grant_id "
            "WHERE t.token_hash=%s AND t.kind='access' AND NOT t.used AND t.expires_at>%s "
            "AND NOT g.revoked AND g.resource=%s AND 'publish'=ANY(g.scopes)",
            (digest(token), int(time.time()), self.oauth.resource),
        ).fetchone()

    def limit(self, conn, publisher_id, action):
        now = int(time.time())
        row = conn.execute(
            "INSERT INTO oauth_connection_limits VALUES (%s,%s,%s,1) "
            "ON CONFLICT (publisher_id,action) DO UPDATE SET "
            "attempts=CASE WHEN oauth_connection_limits.window_start<=%s THEN 1 ELSE oauth_connection_limits.attempts+1 END, "
            "window_start=CASE WHEN oauth_connection_limits.window_start<=%s THEN %s ELSE oauth_connection_limits.window_start END "
            "RETURNING attempts,window_start", (publisher_id, action, now, now-LINK_TTL, now-LINK_TTL, now),
        ).fetchone()
        if row["attempts"] > MAX_ATTEMPTS:
            return failure(429, "connection_rate_limited", "Too many connection-code requests. Try again later.",
                           retry_after=max(1, row["window_start"]+LINK_TTL-now))
        return None

    @threaded
    def issue(self, token):
        with self.oauth.connection() as conn:
            lock_publishers(conn)
            actor = self.principal(conn, token)
            if not actor:
                return failure(401, "invalid_token", "Personal OAuth publishing access is required")
            blocked = self.limit(conn, actor["publisher_id"], "issue")
            if blocked:
                return blocked
            now = int(time.time())
            # Only the latest issued code is valid. A code does not outlive its grant.
            conn.execute("DELETE FROM oauth_connection_codes WHERE publisher_id=%s OR expires_at<=%s",
                         (actor["publisher_id"], now))
            raw = "".join(secrets.choice(ALPHABET) for _ in range(12))
            conn.execute("INSERT INTO oauth_connection_codes (code_hash,publisher_id,grant_id,expires_at) VALUES (%s,%s,%s,%s)",
                         (digest(raw), actor["publisher_id"], actor["grant_id"], now+LINK_TTL))
        return {"code": "-".join(raw[i:i+4] for i in range(0, 12, 4)), "expires_in": LINK_TTL,
                "expires_at": datetime.fromtimestamp(now+LINK_TTL, timezone.utc).isoformat(),
                "single_use": True, "instructions": "Use only in your other authorized PeopleMCP chat or its OAuth consent page. "
                "Anyone with this code can join your publishing access. Do not publish it or send it to another person. "
                "This code replaces any previously issued code; it is not an access/refresh/operator token."}

    def redeem_in_transaction(self, conn, target_id, code, confirm_merge_publications=False, flow_hash=None):
        """Caller holds lock_publishers; return failures so attempt counters commit."""
        now = int(time.time())
        if flow_hash:
            row = conn.execute("UPDATE oauth_pending SET link_attempts=link_attempts+1 WHERE flow_hash=%s "
                               "RETURNING link_attempts,expires_at", (flow_hash,)).fetchone()
            if not row or row["link_attempts"] > MAX_ATTEMPTS:
                return failure(429, "connection_rate_limited", "Too many attempts. Restart the connection and use a fresh code.")
        if target_id:
            if not conn.execute("SELECT 1 FROM oauth_publishers WHERE id=%s", (target_id,)).fetchone():
                return failure(409, "connection_changed", "Connection changed; retry from your client")
            blocked = self.limit(conn, target_id, "redeem")
            if blocked:
                return blocked
        raw = normalize_code(code)
        row = conn.execute(
            "SELECT c.* FROM oauth_connection_codes c JOIN oauth_grants g ON g.id=c.grant_id "
            "WHERE c.code_hash=%s AND c.used_at IS NULL AND c.expires_at>%s AND NOT g.revoked "
            "AND g.publisher_id=c.publisher_id AND g.resource=%s AND 'publish'=ANY(g.scopes)",
            (digest(raw), now, self.oauth.resource),
        ).fetchone()
        if not row:
            return failure(400, "invalid_connection_code", "Code is invalid, expired, replaced or already used. Request a fresh code.")
        source_id = row["publisher_id"]
        counts = dict.fromkeys(TABLES, 0)
        different = target_id is not None and str(target_id) != str(source_id)
        if different:
            fields = sql.SQL(", ").join(
                sql.SQL("count({}) AS {}").format(sql.Identifier(owner), sql.Identifier(kind))
                for kind, (_, _, owner) in TABLES.items())
            counts = conn.execute(sql.SQL("SELECT {} FROM publication_owners WHERE publisher_id=%s").format(fields),
                                  (target_id,)).fetchone()
            if any(counts.values()) and not confirm_merge_publications:
                return failure(409, "merge_confirmation_required",
                    "This connection already owns " + ", ".join(f"{count} {kind}" for kind, count in counts.items()) + ". "
                    "Nothing was transferred. Ask the user explicitly before retrying with confirm_merge_publications=true. "
                    "All its publications and existing connections will join the code issuer's owner; content will be preserved.",
                    publications_to_transfer=counts)
            # Invalidate the duplicate owner's outstanding transfer credentials,
            # then rebind ALL existing connections before removing only its ID.
            conn.execute("DELETE FROM oauth_connection_codes WHERE publisher_id=%s", (target_id,))
            for table in ("publication_owners", "oauth_grants", "oauth_browser_sessions", "oauth_pending"):
                # Table names are fixed internal constants, never user input.
                conn.execute(f"UPDATE {table} SET publisher_id=%s WHERE publisher_id=%s", (source_id, target_id))
            conn.execute("DELETE FROM oauth_publishers WHERE id=%s", (target_id,))
        conn.execute("UPDATE oauth_connection_codes SET used_at=%s WHERE code_hash=%s", (now, digest(raw)))
        return {"status": "already_linked" if target_id and not different else "linked", "publisher_id": str(source_id),
                "transferred_publications": counts,
                "message": "Both connections now use the code issuer's owner. Existing connections and all public content are preserved."}

    @threaded
    def redeem(self, token, code, confirm_merge_publications=False):
        with self.oauth.connection() as conn:
            lock_publishers(conn)
            actor = self.principal(conn, token)
            if not actor:
                return failure(401, "invalid_token", "Personal OAuth publishing access is required")
            result = self.redeem_in_transaction(conn, actor["publisher_id"], code, confirm_merge_publications)
        result.pop("publisher_id", None)  # Internal identity is not public profile data.
        return result
