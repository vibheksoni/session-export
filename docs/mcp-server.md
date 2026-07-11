# MCP Server

The MCP server exposes UniSessions search and listing capabilities to MCP-compatible AI agents via the Model Context Protocol.

## Starting the Server

```bash
# Using the console script (installed via pip install -e ".[mcp]")
unisessions-mcp

# Using Python module
python -m unisessions.mcp_server
```

## Transports

| Transport | Command | Use Case |
|---|---|---|
| stdio (default) | `unisessions-mcp` | Normal MCP host integration. Keeps stdout protocol-clean. |
| streamable-http | `unisessions-mcp --transport streamable-http --host 127.0.0.1 --port 8765 --path /mcp` | Long-running app-managed server. |
| http | `unisessions-mcp --transport http --host 127.0.0.1 --port 8765` | HTTP transport. |
| sse | `unisessions-mcp --transport sse --host 127.0.0.1 --port 8765` | Server-Sent Events transport. |

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `UNISESSIONS_MCP_TRANSPORT` | `stdio` | MCP transport type. |
| `UNISESSIONS_MCP_HOST` | `127.0.0.1` | Bind host for non-stdio transports. |
| `UNISESSIONS_MCP_PORT` | `8765` | Bind port for non-stdio transports. |
| `UNISESSIONS_MCP_PATH` | (none) | URL path for streamable-http. |
| `UNISESSIONS_MCP_LOG_LEVEL` | `ERROR` | Logging level. |
| `UNISESSIONS_MCP_SHOW_BANNER` | `false` | Show FastMCP startup banner. Keep off for stdio. |
| `UNISESSIONS_SEARCH_INDEX` | `%LOCALAPPDATA%/unisessions/search.sqlite` | Path to the FTS5 index database. |

## Tools

### list_chats

List known chat sessions, optionally filtered by provider or project path.

```python
list_chats(
    cwd: str | None = None,
    provider: str = "all",       # "all", "codex", "pi", "opencode", "claude"
    cwd_match: str = "exact",    # "exact", "contains", "prefix"
    limit: int = 100,
    workers: int = 1,
) -> list[dict]
```

Returns dicts with `provider`, `session_id`, `cwd`, `timestamp`, `path`, `message_count`.

### index_status

Report indexed, missing, stale, and deleted chat index state without parsing sessions.

```python
index_status(
    cwd: str | None = None,
    provider: str = "all",
    cwd_match: str = "exact",
    workers: int = 1,
) -> dict
```

Returns per-provider stats plus `total_refresh_sessions` and `total_refresh_bytes`.

### refresh_chats_index

Parse sessions into the local FTS index so later recall is fast.

```python
refresh_chats_index(
    cwd: str | None = None,
    provider: str = "all",
    cwd_match: str = "exact",
    workers: int = 1,
    max_refresh_sessions: int | None = None,
    max_refresh_bytes: int | None = None,
) -> dict
```

Returns a dict mapping provider names to the number of sessions indexed, plus a `total` key.

### search_chats

Search chat text across one, many, or all sessions.

```python
search_chats(
    query: str | None = None,
    keywords: list[str] | None = None,
    regex: str | None = None,
    exclude_keywords: list[str] | None = None,
    provider: str = "all",
    cwd: str | None = None,
    cwd_match: str = "exact",
    session_ids: list[str] | None = None,
    roles: list[str] | None = None,         # ["user", "assistant"]
    message_types: list[str] | None = None,  # ["message", "compaction", "contextual"]
    mode: str = "literal",                   # "literal", "regex", "all_keywords", "any_keywords"
    case_sensitive: bool = False,
    include_contextual: bool = False,
    include_compactions: bool = False,
    max_results: int = 50,
    context_chars: int = 300,
    max_per_session: int = 5,                # 0 = unlimited
    after: str | None = None,                # ISO timestamp lower bound
    before: str | None = None,               # ISO timestamp upper bound
    workers: int = 1,
    stale_policy: str = "skip",              # "refresh", "skip", "error"
    max_refresh_sessions: int | None = None,
    max_refresh_bytes: int | None = None,
) -> dict
```

Returns a structured response with `search_metadata` (total_matches, returned, deduplicated, sessions_searched, messages_searched, truncated) and a `results` array. Each result has `rank`, `relevance_score`, `provider`, `session_id`, `timestamp`, `role`, `message_type`, `snippet`, `duplicate_count`, `first_seen`, and `match_positions`.

### search_sessions

```python
search_sessions(
    query: str | None = None,
    keywords: list[str] | None = None,
    regex: str | None = None,
    exclude_keywords: list[str] | None = None,
    provider: str = "all",
    cwd: str | None = None,
    cwd_match: str = "exact",
    session_ids: list[str] | None = None,
    roles: list[str] | None = None,
    message_types: list[str] | None = None,
    mode: str = "literal",
    case_sensitive: bool = False,
    include_contextual: bool = False,
    include_compactions: bool = False,
    after: str | None = None,
    before: str | None = None,
    workers: int = 1,
    stale_policy: str = "skip",
) -> list[dict]
```

Finds which sessions are relevant to a topic. Returns sessions sorted by match_count with top_snippets.

## MCP Client Configuration

Example configuration for an MCP client (e.g., Claude Desktop, Pi, or other MCP host):

```json
{
  "mcpServers": {
    "unisessions": {
      "command": "unisessions-mcp",
      "transport": "stdio"
    }
  }
}
```

For HTTP transport:

```json
{
  "mcpServers": {
    "unisessions": {
      "transport": "streamable-http",
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

## Recommended Workflow

For low-latency interactive search:

1. **`index_status`** -- check how many sessions need indexing and the total refresh size.
2. **`refresh_chats_index`** -- parse stale/missing sessions into the FTS5 index. Use `workers=4` for faster refresh on I/O-bound datasets.
3. **`search_chats`** with `stale_policy="skip"` -- search only cached index rows for ~35-40ms latency.

For fast cached recall, use `stale_policy="skip"` (default) in `search_chats`. This searches only indexed sessions without parsing new ones. Call `refresh_chats_index` first to ensure sessions are indexed.

For always-fresh results, use `stale_policy="refresh"`. This parses changed sessions before searching, adding latency on the first search after changes.

For caller-controlled refresh, use `stale_policy="error"`. The call raises if sessions need refresh, letting the caller decide whether to call `refresh_chats_index` first.

## See Also

- [Search](search.md) -- the underlying `SessionSearchEngine` API.
- [Installation](installation.md) -- installing MCP dependencies.
