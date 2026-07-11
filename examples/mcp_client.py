"""Connect to the UniSessions MCP server as a client.

This example uses the fastmcp Client to call the MCP server's tools
(list_chats, index_status, refresh_chats_index, search_chats) via stdio.

Usage:
    python examples/mcp_client.py [query]
    python examples/mcp_client.py "error handling"
    python examples/mcp_client.py

The server is launched automatically as a subprocess via stdio transport.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import PythonStdioTransport


def _server_script() -> Path:
    """Resolve the path to the MCP server module."""
    root = Path(__file__).resolve().parent.parent
    return root / "unisessions" / "mcp_server.py"


async def run(query: str | None) -> int:
    transport = PythonStdioTransport(script_path=str(_server_script()))

    async with Client(transport) as client:
        # Step 1: List chats across all providers
        print("=== list_chats ===")
        chats = await client.call_tool("list_chats", {"limit": 10})
        print(chats)

        # Step 2: Check index status
        print("\n=== index_status ===")
        status = await client.call_tool("index_status", {})
        print(status)

        # Step 3: Refresh the index for claude only (small, already indexed)
        # Scoping to a single provider avoids the large codex/pi corpus.
        print("\n=== refresh_chats_index (provider=claude) ===")
        try:
            refreshed = await client.call_tool("refresh_chats_index", {
                "provider": "claude",
            })
            print(refreshed)
        except Exception as exc:
            print(f"  refresh failed (expected if corpus is large): {exc}")

        # Step 4: Search (if a query was provided)
        if query:
            print(f"\n=== search_chats: '{query}' ===")
            response = await client.call_tool("search_chats", {
                "query": query,
                "max_results": 10,
                "max_per_session": 3,
                "stale_policy": "skip",
            })
            print(response)

            print(f"\n=== search_sessions: '{query}' ===")
            sessions = await client.call_tool("search_sessions", {
                "query": query,
                "stale_policy": "skip",
            })
            print(sessions)
        else:
            print("\nNo query provided -- skipping search_chats.")
            print("Usage: python examples/mcp_client.py \"your search query\"")

    return 0


def main() -> int:
    query = sys.argv[1] if len(sys.argv) > 1 else None
    return asyncio.run(run(query))


if __name__ == "__main__":
    sys.exit(main())
