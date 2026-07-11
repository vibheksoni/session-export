# Search

The search module provides full-text search across all session providers using a SQLite FTS5 index. It parses session files into `TextMessage` rows, indexes them, and supports literal, regex, all-keywords, and any-keywords matching with role and message-type filters.

## SessionSearchEngine

The high-level search API that coordinates stores, the FTS5 index, and message extraction.

```python
from session_sdk.search import SessionSearchEngine
from session_sdk.stores import CodexStore, PiStore, OpenCodeStore, ClaudeStore, DevinStore, FactoryStore, WindsurfStore
from session_sdk.paths import WindowsDefaults

defaults = WindowsDefaults()
engine = SessionSearchEngine(
    codex=CodexStore(defaults.codex_home),
    pi=PiStore(defaults.pi_agent_home),
    opencode=OpenCodeStore(defaults.opencode_data_home),
    claude=ClaudeStore(defaults.claude_home),
    devin=DevinStore(defaults.devin_home),
    factory=FactoryStore(defaults.factory_home),
    windsurf=WindsurfStore(defaults.windsurf_home),
    index_path=Path("/custom/search.sqlite"),  # None = in-memory
)
```

### list_chats

```python
engine.list_chats(
    provider="all",        # "all", "codex", "pi", "opencode", "claude", "devin", "factory", "windsurf"
    cwd=None,              # filter by project path
    cwd_match="exact",     # "exact", "contains", "prefix"
    limit=100,
    workers=1,
) -> list[dict[str, object]]
```

Lists chat sessions across one or all providers. Returns dicts with `provider`, `session_id`, `cwd`, `timestamp`, `path`, and `message_count`.

### index_status

```python
engine.index_status(
    provider="all",
    cwd=None,
    cwd_match="exact",
    workers=1,
) -> dict[str, dict[str, int] | int]
```

Reports index state without parsing session bodies. Returns a dict keyed by provider name, each containing:

| Key | Description |
|---|---|
| `sessions` | Total sessions on disk |
| `indexed_sessions` | Sessions present in the index |
| `missing_sessions` | Sessions not yet indexed |
| `stale_sessions` | Indexed sessions whose file changed |
| `refresh_sessions` | missing + stale |
| `refresh_bytes` | Total bytes to parse for refresh |
| `indexed_messages` | Total indexed message rows |
| `deleted_indexed_paths` | Index entries whose files no longer exist |

Top-level keys: `total_refresh_sessions`, `total_refresh_bytes`.

### refresh_index

```python
engine.refresh_index(
    provider="all",
    cwd=None,
    cwd_match="exact",
    workers=1,
    max_refresh_sessions=None,  # guardrail: raise if exceeded
    max_refresh_bytes=None,     # guardrail: raise if exceeded
) -> dict[str, int]
```

Parses stale/missing sessions into the FTS5 index. Returns a dict mapping provider names to the number of sessions indexed, plus a `total` key.

### search

```python
engine.search(
    query=None,               # search text
    keywords=None,            # list of keywords (alternative to query)
    regex=None,               # regex pattern (alternative to query)
    exclude_keywords=None,    # keywords to exclude from results
    provider="all",
    cwd=None,
    cwd_match="exact",
    session_ids=None,         # filter to specific session IDs
    roles=None,               # ["user", "assistant"]
    message_types=None,       # ["message", "compaction", "contextual"]
    mode="literal",           # "literal", "regex", "all_keywords", "any_keywords"
    case_sensitive=False,
    include_contextual=False,
    include_compactions=False,
    max_results=50,
    context_chars=300,
    max_per_session=5,        # 0 = unlimited
    after=None,               # ISO timestamp lower bound
    before=None,              # ISO timestamp upper bound
    workers=1,
    stale_policy="skip",     # "refresh", "skip", "error"
    max_refresh_sessions=None,
    max_refresh_bytes=None,
) -> dict[str, object]
```

Returns a structured response with `search_metadata` (total_matches, returned, deduplicated, sessions_searched, messages_searched, truncated) and a `results` array. Each result has `rank`, `relevance_score`, `provider`, `session_id`, `timestamp`, `role`, `message_type`, `snippet`, `duplicate_count`, `first_seen`, and `match_positions`.

### raw_search_rows

```python
engine.raw_search_rows(
    query=None,
    keywords=None,
    regex=None,
    mode="literal",
    provider="all",
    roles=None,
    message_types=None,
    include_contextual=True,
    include_compactions=True,
    after=None,
    before=None,
    limit=1000,
) -> list[dict[str, object]]
```

Returns raw indexed candidate rows with full `text`, `path`, `cwd`, `message_index`, role, type, and timestamp. This SDK escape hatch is for applications that want their own ranking, deduplication, context expansion, or regex verification logic. It does not apply MCP safety defaults such as `max_per_session`, result shaping, relevance scoring, or live-source pruning.

### search_sessions

```python
engine.search_sessions(
    query=None,
    keywords=None,
    regex=None,
    exclude_keywords=None,
    provider="all",
    cwd=None,
    cwd_match="exact",
    session_ids=None,
    roles=None,
    message_types=None,
    mode="literal",
    case_sensitive=False,
    include_contextual=False,
    include_compactions=False,
    after=None,
    before=None,
    workers=1,
    stale_policy="skip",
) -> list[dict[str, object]]
```

Finds which sessions are relevant to a topic. Returns a list sorted by `match_count` (descending), each with `provider`, `session_id`, `cwd`, `timestamp`, `match_count`, and `top_snippets`.

### close

```python
engine.close()
```

Closes the underlying SQLite connection. Always call when done, or use a `try/finally` block.

## ChatSearchResult

```python
@dataclass(frozen=True, slots=True)
class ChatSearchResult:
    provider: str
    session_id: str
    cwd: str
    timestamp: str
    path: str
    message_index: int
    role: str
    message_type: str        # "message", "compaction", or "contextual"
    snippet: str

    def as_dict(self) -> dict[str, object]: ...
```

## StalePolicy

Controls how the search engine handles sessions whose files have changed since indexing:

| Policy | Behavior |
|---|---|
| `"refresh"` | Parse stale sessions before searching. Adds latency on first search. |
| `"skip"` | Search only cached index rows (default). Lowest latency. May miss recent content. |
| `"error"` | Raise `ValueError` if any matching sessions need refresh. Caller decides whether to refresh. |

## Match Modes

| Mode | Behavior |
|---|---|
| `"literal"` | All terms in the query must appear in the text. Uses FTS5 for pre-filtering. |
| `"regex"` | Regular expression search. Uses FTS5 OR query on extracted literals for candidate narrowing, then applies Python regex (or google-re2 when installed, ~4x faster). |
| `"all_keywords"` | Every keyword must appear in the text. |
| `"any_keywords"` | At least one keyword must appear. |

## SessionSearchIndex

The low-level FTS5 index manager. `SessionSearchEngine` wraps this for controlled application defaults, but SDK users can call `raw_search_rows()` or `build_fts_query()` directly when they need custom search behavior.

```python
from session_sdk.search import SessionSearchIndex

index = SessionSearchIndex(Path("/path/to/search.sqlite"))
# or in-memory:
index = SessionSearchIndex(None)
```

### Schema

```sql
CREATE TABLE sessions (
    path TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    session_id TEXT NOT NULL,
    cwd TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    mtime_ns INTEGER NOT NULL,
    size INTEGER NOT NULL,
    message_count INTEGER NOT NULL
);

CREATE VIRTUAL TABLE messages_fts USING fts5(
    provider UNINDEXED,
    session_id UNINDEXED,
    path UNINDEXED,
    cwd UNINDEXED,
    timestamp UNINDEXED,
    message_index UNINDEXED,
    role UNINDEXED,
    message_type UNINDEXED,
    text,
    tokenize='unicode61'
);

```

Uses WAL journal mode with `PRAGMA synchronous=NORMAL` for balanced durability and speed.

## Performance

| Operation | Time |
|---|---|
| Cold refresh (20 sessions, 1 worker) | ~49 seconds |
| Cold refresh (20 sessions, 4 workers) | ~39 seconds |
| Warm search (indexed) | ~35-40ms |

High worker counts should be opt-in for refresh only. Huge sessions (700MB+) can overrun RAM when parsed concurrently. For interactive UX, call `index_status` first, then use `stale_policy="skip"` for low-latency recall.

## Default Index Path

When `index_path` is not specified, the MCP server uses:

```
%LOCALAPPDATA%/unisessions/search.sqlite
```

Override with the `UNISESSIONS_SEARCH_INDEX` environment variable.

## See Also

- [MCP Server](mcp-server.md) -- exposes the search engine as MCP tools.
- [Models](models.md) -- `TextMessage` rows that are indexed.
