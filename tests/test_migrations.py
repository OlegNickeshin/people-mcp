"""Upgrade a synthetic pre-agents schema and verify data and OAuth continuity."""
import asyncio
import json
import unittest
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from server.config import MODEL_NAME, Settings
from server.db import Repository
from server.oauth import PublisherOAuth, digest


class AgentMigrationSmoke(unittest.TestCase):
    def test_upgrade_is_repeatable_preserves_legacy_data_and_enforces_one_kind(self):
        base = Settings().database_url
        schema = "agent_migration_test_" + uuid4().hex
        with psycopg.connect(base) as conn:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

        def cleanup():
            # Only the uniquely named schema created by this test; never public.
            with psycopg.connect(base) as conn:
                conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        self.addCleanup(cleanup)
        dsn = make_conninfo(base, options=f"-c search_path={schema},public")
        provider = PublisherOAuth(Settings(database_url=dsn))
        publisher, profile, project, agent, grant = [uuid4() for _ in range(5)]
        raw_token = uuid4().hex
        tables = ("profiles", "profile_chunks", "projects", "project_chunks", "index_metadata",
                  "oauth_publishers", "oauth_clients", "oauth_grants", "oauth_tokens", "oauth_browser_sessions")

        def snapshot(conn):
            rows = {table: conn.execute(sql.SQL("SELECT row_to_json(t)::text FROM {} t").format(
                sql.Identifier(table))).fetchall() for table in tables}
            rows["owners"] = conn.execute("SELECT publisher_id,profile_id,project_id FROM publication_owners "
                                           "ORDER BY profile_id NULLS LAST").fetchall()
            return rows

        with psycopg.connect(dsn) as conn:
            migrations = Path(__file__).resolve().parent.parent / "migrations"
            for name in ("001_initial.sql", "002_publishers.sql", "003_connection_codes.sql"):
                conn.execute((migrations / name).read_text())
            conn.execute("INSERT INTO index_metadata VALUES ('embedding_model',%s)", (MODEL_NAME,))
            conn.execute("INSERT INTO oauth_publishers (id) VALUES (%s)", (publisher,))
            for kind, identifier, chunk_table, owner_column in (
                ("profiles", profile, "profile_chunks", "profile_id"),
                ("projects", project, "project_chunks", "project_id"),
            ):
                conn.execute(sql.SQL("INSERT INTO {} VALUES (%s,%s,%s,%s,'2020-01-01','2021-02-03')").format(
                    sql.Identifier(kind)), (identifier, "legacy-" + kind, "Existing public context", "legacy@example.org"))
                conn.execute(sql.SQL("INSERT INTO {} VALUES (%s,%s,%s,%s::vector)").format(sql.Identifier(chunk_table)),
                             (uuid4(), identifier, "Existing public context", json.dumps([1.0] + [0.0] * 383)))
                conn.execute(sql.SQL("INSERT INTO publication_owners (publisher_id,{}) VALUES (%s,%s)").format(
                    sql.Identifier(owner_column)), (publisher, identifier))
            conn.execute("INSERT INTO oauth_clients (client_id,metadata) VALUES ('legacy-client','{}')")
            conn.execute("INSERT INTO oauth_grants (id,publisher_id,client_id,resource,scopes) "
                         "VALUES (%s,%s,'legacy-client',%s,ARRAY['publish'])", (grant, publisher, provider.resource))
            conn.execute("INSERT INTO oauth_tokens VALUES (%s,%s,'access',9999999999,false)", (digest(raw_token), grant))
            conn.execute("INSERT INTO oauth_browser_sessions VALUES (%s,%s,9999999999)", (digest("synthetic-cookie"), publisher))
            before = snapshot(conn)
        self.assertEqual(asyncio.run(provider.load_access_token(raw_token)).subject, str(publisher))

        repository = Repository(dsn)
        repository.migrate()
        repository.migrate()  # Startup re-runs every migration, so replay must be safe.
        with psycopg.connect(dsn) as conn:
            self.assertEqual(snapshot(conn), before)
            conn.execute("INSERT INTO agents (id,slug,content,contact) VALUES (%s,'test-agent','Public agent','test@example.org')", (agent,))
            conn.execute("INSERT INTO agent_chunks VALUES (%s,%s,'Public agent',%s::vector)",
                         (uuid4(), agent, json.dumps([1.0] + [0.0] * 383)))
            conn.execute("INSERT INTO publication_owners (publisher_id,agent_id) VALUES (%s,%s)", (publisher, agent))
            for fields in ((None, None, None), (profile, None, agent), (None, project, agent), (profile, project, None)):
                with self.assertRaises(psycopg.errors.CheckViolation):
                    with conn.transaction():
                        conn.execute("INSERT INTO publication_owners (publisher_id,profile_id,project_id,agent_id) "
                                     "VALUES (%s,%s,%s,%s)", (publisher, *fields))
            conn.execute("DELETE FROM agents WHERE id=%s", (agent,))
            self.assertEqual(conn.execute("SELECT count(*) FROM agent_chunks WHERE agent_id=%s", (agent,)).fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM publication_owners WHERE agent_id=%s", (agent,)).fetchone()[0], 0)
            self.assertEqual(snapshot(conn), before)
        access = asyncio.run(provider.load_access_token(raw_token))
        self.assertEqual(access.subject, str(publisher))
        self.assertEqual(access.resource, provider.resource)
