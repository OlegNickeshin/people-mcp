import asyncio
import json
import os

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main():
    url = os.getenv("MCP_URL", "http://127.0.0.1:8000/mcp")
    async with streamable_http_client(url) as (reader, writer, _):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            print("Tools:", ", ".join(tool.name for tool in (await session.list_tools()).tools))
            result = await session.call_tool("search_people", {
                "query": "Find someone who understands MCP and Telegram integrations.", "limit": 3,
            })
            if result.isError:
                raise RuntimeError(result.content)
            print(json.dumps(result.structuredContent or json.loads(result.content[0].text), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
