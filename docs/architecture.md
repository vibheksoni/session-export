# Architecture

## Package Structure

```
session-export/
  session_sdk/          # Library package (no CLI dependencies)
    __init__.py         # Public API surface
    converters.py       # 20 converters, MessageExtractor, 5 record builders
    traces.py           # STS, OpenAI, ShareGPT trace format builders
    json_types.py       # Safe JSON type guards and coercion
    jsonl.py            # JSONL read/write helpers (orjson with stdlib fallback)
    models.py           # SessionSummary, TextMessage, NativeSession, ConversionPlan
    paths.py            # WindowsDefaults, SessionIdFactory, encoding helpers
    search.py           # SessionSearchEngine, SessionSearchIndex, ChatSearchResult
    stores.py           # CodexStore, PiStore, OpenCodeStore, ClaudeStore, DevinStore, PiDcpStore
  unisessions/          # CLI + MCP server package
    __main__.py         # CLI entry point
    cli.py              # CliApp with all commands and flags
    mcp_server.py       # FastMCP stdio/http/sse server
  tests/
    test_conversion.py  # 29 tests
```

## Dependency Direction

```
unisessions  -->  session_sdk
```

The SDK never imports from the CLI. This means `session_sdk` can be used as a standalone library in any Python project without pulling in CLI or MCP dependencies.

### Module Dependency Chain

```
paths.py          (no internal deps)
  |
json_types.py     (no internal deps)
  |
jsonl.py          --> json_types
  |
models.py         --> json_types
  |
stores.py         --> paths, jsonl, json_types, models
  |
converters.py     --> paths, models, stores, jsonl, json_types
  |
search.py         --> converters, models, stores
  |
__init__.py       --> converters, jsonl, models, paths, search, stores
  |
unisessions/cli.py     --> converters, models, paths, stores
unisessions/mcp_server --> paths, search, stores
```

## Data Flow

### Single-Session Conversion

```
1. CLI parses args, creates stores + SessionIdFactory
2. Converter.plan(session_id):
   a. source_store.load(session_id) -> NativeSession
   b. MessageExtractor.from_<source>(session) -> list[TextMessage]
   c. RecordBuilder.build(id, cwd, ts, messages) -> list[JsonObject]
   d. target_store.destination_path(id, ts, cwd) -> Path
   e. Return ConversionPlan(source, destination, records, services)
3. CLI prints plan (dry run) or calls converter.write(plan)
4. Converter.write():
   a. target_store.write(destination, records)
   b. For Pi targets: dcp_store.write_default(id, dcp_path)
```

### Bulk Export

```
1. CLI lists all Codex sessions (codex_store.list())
2. Creates converter instances once (reused across sessions)
3. ThreadPoolExecutor submits convert_one(summary) per session
4. Each convert_one:
   a. For each target (pi, opencode, claude):
      - Check conflict mode (skip/overwrite/fork/update)
      - plan() or plan(target_id=new_uuid) for fork
      - write(plan, overwrite=True)
5. Results aggregated and printed
```

### Search

```
1. SessionSearchEngine created with stores + index path
2. list_chats: store.list_metadata() -> filter by cwd -> return dicts
3. index_status: store.list_metadata() -> index.status(summaries) -> counts
4. refresh_index: store.list_metadata() -> index.ensure(store, summaries, extractor)
   a. For each stale session: store.load_path() -> extractor.from_<provider>()
   b. Insert TextMessage rows into messages_fts table
5. search: store.list_metadata() -> check stale_policy -> index.search()
   a. FTS5 pre-filter (for non-regex modes)
   b. Python-side matcher for precise matching
   c. Return ChatSearchResult list
```

## Data Fidelity Contract

This tool performs **text-history conversions**, not full behavioral state replay. See [Data Fidelity](data-fidelity.md) for the complete list of what is and is not preserved.

## Compaction Handling

Compaction markers are preserved across all four formats. The internal representation is `TextMessage(role="user", is_compaction=True)`.

| Target Format | Compaction Emission |
|---|---|
| Pi | `type="compaction"` entry with `summary`, `firstKeptEntryId`, `tokensBefore`, `fromHook=True` |
| Codex | `type="compacted"` record with `payload.message` as summary |
| OpenCode | User message with `CompactionPart` + assistant message with `summary=True` |
| Claude | `type="system"` record with `subtype="compact_boundary"` |

Source extractors handle compaction from each format:

| Source | Detection |
|---|---|
| Codex | `compacted` records; `replacement_history` items also extracted |
| Pi | `compaction` entries |
| OpenCode | Assistant messages with `summary=True`; `CompactionPart` filtered |
| Claude | `system` records with `subtype="compact_boundary"` or `isCompactSummary=True` |

## Contextual Message Handling

Codex injects contextual messages (developer permissions, AGENTS.md instructions, environment context, skills, plugins). The `MessageExtractor._is_contextual()` method detects these via 19+ marker patterns. Detected messages are marked `is_contextual=True` on the `TextMessage` and skipped by all builders during export. This prevents payload overflow or silent drops in target tools that do not expect these injected messages.

## Performance Features

### orjson

When `orjson` is installed, all JSON parsing uses it (measured at ~244 MB/s vs ~120 MB/s for stdlib `json`). Falls back to stdlib `json` transparently. No configuration needed.

### O(1) Session Lookups

All stores cache `_session_paths()` (list of file paths) and `_id_index` (dict mapping session ID to path) after the first access. Subsequent `load(session_id)` calls are O(1) dict lookups instead of O(n) file scanning.

### Fast Change Detection

`has_changes()` reads only the source file's head metadata (first 200 lines) and counts file lines, then compares against the destination. No full JSON parse, extract, or build cycle. On a 913 MB file with 155K records: <0.1s instead of ~36s.

### Converter Reuse

Bulk export creates converter instances once and reuses them across all sessions, avoiding repeated initialization of `MessageExtractor` and `TokenEstimator`.

### Sequential Pi Entry IDs

`PiRecordBuilder` uses sequential hex IDs (`f"{n:08x}"`) instead of `token_hex(4)`. Random 8-hex-char IDs collide via birthday paradox at ~65K entries, creating `parentId` cycles that crash Pi with `RangeError: Invalid array length`. Sequential IDs guarantee uniqueness regardless of session size.

### Corrupted Line Handling

`JsonlFile.read()` skips unparseable JSONL lines (e.g. backslash-prefixed `\{` lines from Codex bugs) with a stderr warning rather than failing the entire file.

## See Also

- [Data Fidelity](data-fidelity.md) -- what is preserved and what is not.
- [Stores](stores.md) -- store class details.
- [Converters](converters.md) -- converter and builder details.
- [Search](search.md) -- FTS5 index architecture.
