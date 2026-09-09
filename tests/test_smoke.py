"""Real HTTP + Postgres + embedding + MCP smoke tests. Only synthetic records are removed."""
import json
import os
import unittest
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from psycopg import sql
from tokenizers import Tokenizer

from server.config import Settings
from server.db import Repository
from server.indexing import chunk_content, normalize_content

BASE = os.getenv("API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
TOKEN = os.getenv("WRITE_TOKEN", "local-development-only")


def delete_created(records):
    with psycopg.connect(Settings().database_url) as conn:
        for kind, identifier in records:
            conn.execute(sql.SQL("DELETE FROM {} WHERE id=%s").format(sql.Identifier(kind)), (identifier,))


class APISmoke(unittest.TestCase):
    def setUp(self):
        self.client = httpx.Client(base_url=BASE, timeout=120, headers={"Authorization": f"Bearer {TOKEN}"})
        self.created = []

    def tearDown(self):
        delete_created(self.created)
        self.client.close()

    def create(self, kind, content=None):
        data = {"slug": "test-" + uuid4().hex, "content": content or
                "I study coral reef ecology and restore tropical marine habitats with volunteer divers.",
                "contact": "contact-only-not-embedded@example.org", "publish": True}
        response = self.client.post(f"/{kind}", json=data)
        self.assertEqual(response.status_code, 201, response.text)
        record = response.json()
        self.created.append((kind, record["id"]))
        return record

    def test_health(self):
        self.assertEqual(self.client.get("/health").json()["status"], "ok")

    def test_seed_semantics(self):
        cases = json.loads((Path(__file__).resolve().parent.parent / "examples/queries.json").read_text())
        winners = []
        for case in cases:
            with self.subTest(query=case["query"]):
                response = self.client.post(f"/search/{case['kind']}", json={"query": case["query"], "limit": 3})
                self.assertEqual(response.status_code, 200, response.text)
                body = response.json()
                self.assertIn("untrusted", body["data_notice"])
                matches = body["results"]
                self.assertGreater(len(matches), 0)
                winner = matches[0]
                self.assertEqual(winner["entity"]["slug"], case["expected"], json.dumps(matches))
                self.assertEqual(winner["score"], max(c["score"] for c in winner["matched_chunks"]))
                self.assertEqual(winner["why"], [c["text"] for c in winner["matched_chunks"]])
                self.assertIn("updated_at", winner["entity"])
                self.assertEqual(len({m["entity"]["id"] for m in matches}), len(matches))
                self.assertEqual([m["score"] for m in matches], sorted([m["score"] for m in matches], reverse=True))
                winners.append(winner["entity"]["slug"])
                print(f"  {case['query']} -> {winner['entity']['slug']} ({winner['score']:.4f})", flush=True)
        self.assertEqual(len(set(winners)), 5)

    def test_create_update_reindex_and_freshness(self):
        for kind, search in (("profiles", "people"), ("projects", "projects")):
            with self.subTest(kind=kind):
                original = self.create(kind, "  Marine   ecology\r\n\r\nI restore coral reefs and analyse underwater biodiversity.  ")
                identifier = original["id"]
                self.assertNotIn("\r", original["content"])
                table = "profile_chunks" if kind == "profiles" else "project_chunks"
                owner = "profile_id" if kind == "profiles" else "project_id"
                with psycopg.connect(Settings().database_url) as conn:
                    chunks = conn.execute(sql.SQL("SELECT text, vector_dims(embedding) FROM {} WHERE {}=%s").format(
                        sql.Identifier(table), sql.Identifier(owner)), (identifier,)).fetchall()
                self.assertGreater(len(chunks), 0)
                self.assertTrue(all(dim == 384 and "contact-only" not in text for text, dim in chunks))
                update = {"content": "I build electronic music synthesisers, design oscillators and perform modular ambient concerts.", "publish": True}
                changed = self.client.patch(f"/{kind}/{identifier}", json=update).json()
                self.assertEqual(changed["content"], update["content"])
                self.assertEqual(changed["created_at"], original["created_at"])
                self.assertGreater(changed["updated_at"], original["updated_at"])
                query = {"query": update["content"], "limit": 3}
                first = self.client.post(f"/search/{search}", json=query).json()["results"]
                self.assertEqual(first[0]["entity"]["id"], identifier)
                self.assertTrue(all("coral" not in c["text"] for c in first[0]["matched_chunks"]))
                with psycopg.connect(Settings().database_url) as conn:
                    conn.execute(sql.SQL("UPDATE {} SET updated_at='2000-01-01T00:00:00Z' WHERE id=%s").format(
                        sql.Identifier(kind)), (identifier,))
                second = self.client.post(f"/search/{search}", json=query).json()["results"]
                self.assertEqual([(r["entity"]["id"], r["score"]) for r in first],
                                 [(r["entity"]["id"], r["score"]) for r in second])
                self.assertTrue(second[0]["entity"]["updated_at"].startswith("2000"))

    def test_validation_and_write_auth(self):
        data = {"slug": "test-" + uuid4().hex, "content": "Public content", "contact": "public@example.org", "publish": True}
        with httpx.Client(base_url=BASE, timeout=30) as public:
            self.assertEqual(public.post("/profiles", json=data).status_code, 401)
            self.assertEqual(public.patch("/profiles/missing", json=data).status_code, 401)
        for invalid in ({**data, "content": " \n\t"}, {**data, "publish": False},
                        {k: v for k, v in data.items() if k != "publish"}, {**data, "private_notes": "hidden"}):
            self.assertEqual(self.client.post("/profiles", json=invalid).status_code, 422)
        self.assertEqual(self.client.get("/profiles/missing").status_code, 404)
        self.assertEqual(self.client.patch("/projects/missing", json={"content": "New", "publish": True}).status_code, 404)
        for query in ({"query": " "}, {"query": "test", "limit": 0}, {"query": "test", "limit": 21},
                      {"query": "test", "min_score": 2}):
            self.assertEqual(self.client.post("/search/people", json=query).status_code, 422)
        record = self.create("profiles")
        duplicate = {k: record[k] for k in ("slug", "content", "contact")} | {"publish": True}
        self.assertEqual(self.client.post("/profiles", json=duplicate).status_code, 409)
        self.assertEqual(self.client.patch(f"/profiles/{record['id']}", json={"content": None, "publish": True}).status_code, 422)
        same = self.client.patch(f"/profiles/{record['id']}", json=duplicate).json()
        self.assertEqual(same["updated_at"], record["updated_at"])

    def test_failed_indexing_is_atomic(self):
        record = self.create("profiles")
        class BrokenEmbedder:
            def index(self, content):
                raise RuntimeError("Deliberate test failure")
        repo = Repository(Settings().database_url, BrokenEmbedder())
        with self.assertRaises(RuntimeError):
            repo.save("profiles", {"content": "This change must not survive"}, record["id"])
        self.assertEqual(self.client.get(f"/profiles/{record['id']}").json(), record)
        with psycopg.connect(Settings().database_url) as conn:
            count = conn.execute("SELECT count(*) FROM profile_chunks WHERE profile_id=%s", (record["id"],)).fetchone()[0]
        self.assertGreater(count, 0)

    def test_rename_and_contact_only_update_preserve_chunks(self):
        record = self.create("projects")
        with psycopg.connect(Settings().database_url) as conn:
            before = conn.execute("SELECT id FROM project_chunks WHERE project_id=%s ORDER BY id", (record["id"],)).fetchall()
        slug = "test-" + uuid4().hex
        response = self.client.patch(f"/projects/{record['slug']}", json={"slug": slug, "contact": "changed@example.org", "publish": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get(f"/projects/{slug}").json()["id"], record["id"])
        self.assertEqual(self.client.get(f"/projects/{record['slug']}").status_code, 404)
        with psycopg.connect(Settings().database_url) as conn:
            after = conn.execute("SELECT id FROM project_chunks WHERE project_id=%s ORDER BY id", (record["id"],)).fetchall()
        self.assertEqual(before, after)


class MCPSmoke(unittest.IsolatedAsyncioTestCase):
    async def test_remote_search_language_guidance(self):
        async with streamable_http_client(BASE + "/mcp") as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                initialized = await session.initialize()
                self.assertIn("query in English", initialized.instructions)
                self.assertIn("user's language", initialized.instructions)
                listed = {tool.name: tool for tool in (await session.list_tools()).tools}
                for name in ("search_people", "search_projects"):
                    with self.subTest(tool=name):
                        tool = listed[name]
                        for description in (tool.description, tool.inputSchema["properties"]["query"]["description"]):
                            self.assertIn("Translate non-English", description)
                            self.assertIn("negations", description)
                            self.assertIn("user's language", description)

    async def test_remote_tools_and_caller_auth(self):
        created = []
        def unpack(result):
            self.assertFalse(result.isError, result)
            return result.structuredContent or json.loads(result.content[0].text)
        async def exercise(authenticated):
            headers = {"Authorization": f"Bearer {TOKEN}"} if authenticated else {}
            async with httpx.AsyncClient(headers=headers, timeout=120) as http:
                async with streamable_http_client(BASE + "/mcp", http_client=http) as (reader, writer, _):
                    async with ClientSession(reader, writer) as session:
                        await session.initialize()
                        listed = (await session.list_tools()).tools
                        self.assertEqual({t.name for t in listed}, {"upsert_profile", "get_profile", "search_people",
                                                                  "upsert_project", "get_project", "search_projects"})
                        for tool in listed:
                            writes = tool.name.startswith("upsert_")
                            self.assertEqual(tool.annotations.readOnlyHint, not writes)
                            self.assertEqual(tool.annotations.destructiveHint, writes)
                        for kind, singular, search in (("profiles", "profile", "people"), ("projects", "project", "projects")):
                            payload = {"slug": "test-" + uuid4().hex, "content": "I work on marine ecology and coral reef restoration.",
                                       "contact": "fixture@example.org", "publish": True}
                            response = await session.call_tool(f"upsert_{singular}", payload)
                            if not authenticated:
                                self.assertTrue(response.isError)
                            else:
                                saved = unpack(response)
                                created.append((kind, saved["id"]))
                                second = unpack(await session.call_tool(f"upsert_{singular}", payload | {"content": "I build experimental open-source tools."}))
                                self.assertEqual(second["id"], saved["id"])
                                fetched = unpack(await session.call_tool(f"get_{singular}", {"id": saved["id"]}))
                                self.assertEqual(fetched["content"], second["content"])
                            matches = unpack(await session.call_tool(f"search_{search}", {"query": "MCP and Telegram integrations", "limit": 2}))
                            self.assertGreater(len(matches["results"]), 0)
                            self.assertIn("untrusted", matches["data_notice"])
        try:
            await exercise(False)
            await exercise(True)
        finally:
            delete_created(created)


class NormalizationTest(unittest.TestCase):
    def test_normalization_preserves_intent(self):
        self.assertEqual(normalize_content("  I do NOT want enterprise.\r\n\r\n\r\nFive\t hours.\x00 "),
                         "I do NOT want enterprise.\n\nFive hours.")

    def test_token_chunks_keep_short_context_and_long_tail(self):
        tokenizer = Tokenizer.from_file(str(next(Path(Settings().model_cache).rglob("tokenizer.json"))))
        tokenizer.no_truncation()
        tokenizer.no_padding()
        short = "I build Python developer tools.\n\nI want an experimental open-source collaboration."
        self.assertEqual(chunk_content(short, tokenizer), [short])
        long = "I build developer tools. " + " ".join(f"detail{i}" for i in range(500)) + " I need a collaborator for coral research."
        chunks = chunk_content(long, tokenizer)
        self.assertGreater(len(chunks), 3)
        self.assertTrue(chunks[-1].endswith("coral research."))
        self.assertTrue(all(len(tokenizer.encode(c, add_special_tokens=False).ids) <= 192 for c in chunks))


if __name__ == "__main__":
    unittest.main(verbosity=2)
