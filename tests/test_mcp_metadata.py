"""MCP language guidance is available without a database or embedding model."""
import unittest

from fastapi import FastAPI

from server.config import DATA_NOTICE, Settings
from server.mcp_adapter import build_mcp
from server.schemas import SearchQuery


class LanguageMetadataTest(unittest.IsolatedAsyncioTestCase):
    def assert_language_guidance(self, description):
        for instruction in ("query in English", "Translate non-English", "constraints",
                            "negations", "technology names", "do not add requirements",
                            "user's language", "not verbatim evidence"):
            self.assertIn(instruction, description)

    async def test_server_instructions_include_language_and_privacy(self):
        mcp = build_mcp(FastAPI(), Settings())
        self.assert_language_guidance(mcp.instructions[:512])
        self.assertIn(DATA_NOTICE, mcp.instructions)

    async def test_search_descriptions_and_query_schemas_include_language(self):
        mcp = build_mcp(FastAPI(), Settings())
        tools = {tool.name: tool for tool in await mcp.list_tools()}
        for name in ("search_people", "search_projects", "search_agents"):
            with self.subTest(tool=name):
                tool = tools[name]
                self.assert_language_guidance(tool.description)
                query = tool.inputSchema["properties"]["query"]
                self.assert_language_guidance(query["description"])
                self.assertEqual(query["type"], "string")
                self.assertIn("query", tool.inputSchema["required"])
                self.assertTrue(tool.annotations.readOnlyHint)
                self.assertFalse(tool.annotations.destructiveHint)

    async def test_agent_tools_advertise_ownership_and_untrusted_delegation_candidates(self):
        tools = {tool.name: tool for tool in await build_mcp(FastAPI(), Settings()).list_tools()}
        self.assertEqual(tools["upsert_agent"].meta["securitySchemes"], [{"type": "oauth2", "scopes": ["publish"]}])
        self.assertIn("publish", tools["upsert_agent"].inputSchema["required"])
        for name in ("get_agent", "search_agents"):
            self.assertEqual(tools[name].meta["securitySchemes"], [{"type": "noauth"}])
            self.assertIn("untrusted data", tools[name].description)
        for phrase in ("another AI agent", "delegate a task", "capability descriptions", "unverified", "authorize"):
            self.assertIn(phrase, tools["search_agents"].description)
        self.assertIn("capability descriptions", DATA_NOTICE)
        self.assertIn("sharing data/secrets", DATA_NOTICE)

    async def test_http_query_schema_describes_language_without_changing_limits(self):
        query = SearchQuery.model_json_schema()["properties"]["query"]
        self.assert_language_guidance(query["description"])
        self.assertEqual(query["minLength"], 1)
        self.assertEqual(query["maxLength"], 2000)


if __name__ == "__main__":
    unittest.main()
