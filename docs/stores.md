# Stores

Store classes provide filesystem access to session files for each tool. All stores implement the `SessionStore` interface with `list`, `list_metadata`, `load`, and `load_path` methods. Stores cache `_session_paths()` and `_id_index` after the first call, making subsequent `load()` lookups O(1) instead of O(n) file scanning.

## SessionStore (Base Class)

```python
class SessionStore:
    provider_name: str

    def list(self, *, workers: int = 1) -> list[SessionSummary]: ...
    def list_metadata(self, *, workers: int = 1) -> list[SessionSummary]: ...
    def load(self, session_id: str) -> NativeSession: ...
    def load_path(self, path: Path) -> NativeSession: ...
```

- `list()` returns full summaries including message counts. Uses `_safe_summary` which reads headers/counts without constructing a full `NativeSession`.
- `list_metadata()` returns metadata-only summaries (message_count = -1). Faster when message counts are not needed.
- `load()` finds a session by ID using the cached `_id_index` dict, then loads and parses the file.
- `load_path()` loads a session directly from a file path, bypassing the ID index.
- `workers > 1` parallelizes file scanning with `ThreadPoolExecutor`.

## CodexStore

```python
from session_sdk.stores import CodexStore
from session_sdk.paths import WindowsDefaults

defaults = WindowsDefaults()
store = CodexStore(defaults.codex_home)
# Or with a custom session directory:
store = CodexStore(defaults.codex_home, session_dir=Path("/custom/sessions"))
```

| Method | Description |
|---|---|
| `list(workers=1)` | Scan `sessions/` and `archived_sessions/` directories recursively for `.jsonl` files. |
| `list_metadata(workers=1)` | Same as `list()` (reads head meta for metadata). |
| `load(session_id)` | Find by UUID in filename or `session_meta.payload.id`. |
| `load_path(path)` | Load and parse a specific rollout file. |
| `destination_path(session_id, timestamp)` | Compute the target path: `sessions/YYYY/MM/DD/rollout-<ts>-<id>.jsonl`. |
| `write(path, records, overwrite=False)` | Write JSONL records atomically (temp file + rename). |

### Codex Format Handling

- Supports both old (pre-2026 flat) and new (2026+ wrapped) rollout formats.
- `_normalize_records()` detects old format and converts to wrapped format before processing.
- `_timestamp_from_file` scans filename from the right for UUID suffix to extract timestamp, falling back to file mtime.
- `_read_head_meta` reads only the first 200 lines to find `session_meta` without full file parse.

### Default Paths

```
<home>/.codex/sessions/YYYY/MM/DD/rollout-YYYY-MM-DDThh-mm-ss-<uuid>.jsonl
<home>/.codex/archived_sessions/YYYY/MM/DD/rollout-YYYY-MM-DDThh-mm-ss-<uuid>.jsonl
```

## PiStore

```python
from session_sdk.stores import PiStore

store = PiStore(defaults.pi_agent_home)
# Or with a custom session directory:
store = PiStore(defaults.pi_agent_home, session_dir=Path("/custom/sessions"))
```

| Method | Description |
|---|---|
| `list(workers=1)` | Scan cwd-encoded subdirectories for `.jsonl` files. Reads header + counts messages. |
| `list_metadata(workers=1)` | Reads only the session header line (faster, message_count = -1). |
| `load(session_id)` | Find by UUID suffix in filename. |
| `load_path(path)` | Load and parse a Pi session JSONL file. |
| `destination_path(session_id, timestamp, cwd)` | Compute target: `sessions/<encoded-cwd>/<ts>_<id>.jsonl`. |
| `write(path, records, overwrite=False)` | Write JSONL records atomically. |

### Pi Path Encoding

Pi encodes cwd into directory names. The `encode_pi_cwd` function strips Windows `\\?\` extended-length path prefixes and replaces path separators with dashes:

```
C:\Projects\myproject  -->  --C--Projects-myproject--
```

### Default Paths

```
<home>/.pi/agent/sessions/<encoded-cwd>/<timestamp>_<session-id>.jsonl
```

## OpenCodeStore

```python
from session_sdk.stores import OpenCodeStore

store = OpenCodeStore(defaults.opencode_data_home)
# Or with a custom session directory:
store = OpenCodeStore(defaults.opencode_data_home, session_dir=Path("/custom/exports"))
```

| Method | Description |
|---|---|
| `list(workers=1)` | Scan `session-export/` directory for `.json` files. Reads info + counts messages. |
| `list_metadata(workers=1)` | Same as `list()` (metadata from JSON info block). |
| `load(session_id)` | Find by filename stem matching session ID. |
| `load_path(path)` | Load and parse an OpenCode export JSON file. |
| `destination_path(session_id)` | Compute target: `session-export/<id>.json`. |
| `write(path, records, overwrite=False)` | Write JSON with indent=2. `records[0]` is the full export object. |

OpenCode uses the official `opencode export` / `opencode import` JSON shape, not direct SQLite writes.

### Default Paths

```
<opencode-data-home>/session-export/<session-id>.json
```

Where `<opencode-data-home>` is `%APPDATA%/opencode` on Windows, `$XDG_DATA_HOME/opencode` on Linux, or `OPENCODE_GLOBAL_DATA_DIR` if set.

## ClaudeStore

```python
from session_sdk.stores import ClaudeStore

store = ClaudeStore(defaults.claude_home)
# Or with a custom session directory:
store = ClaudeStore(defaults.claude_home, session_dir=Path("/custom/projects"))
```

| Method | Description |
|---|---|
| `list(workers=1)` | Scan `projects/` directory recursively for `.jsonl` files. Excludes `subagents/` and `tool-results/` subdirectories. |
| `list_metadata(workers=1)` | Same as `list()`. |
| `load(session_id)` | Find by UUID filename stem. |
| `load_path(path)` | Load and parse a Claude Code session JSONL file. |
| `destination_path(session_id, cwd)` | Compute target: `projects/<sanitized-cwd>/<id>.jsonl`. |
| `write(path, records, overwrite=False)` | Write JSONL records atomically. |

### Claude CWD Sanitization

`sanitize_claude_cwd` strips `\\?\` prefixes and replaces non-alphanumeric characters with dashes:

```
C:\Projects\myproject  -->  C--Projects-myproject
```

### Default Paths

```
<home>/.claude/projects/<sanitized-cwd>/<session-id>.jsonl
```

Where `<home>/.claude` is used unless `CLAUDE_CONFIG_DIR` is set.

## PiDcpStore

Pi DCP (Dynamic Context Pruning) sidecar store. This is not a `SessionStore` subclass -- it has a simpler interface for writing default DCP JSON files.

```python
from session_sdk.stores import PiDcpStore

dcp_store = PiDcpStore(defaults.pi_dcp_home)
```

| Method | Description |
|---|---|
| `destination_path(session_id)` | Compute target: `sessions/<id>.json`. |
| `write_default(session_id, path, overwrite=False)` | Write a minimal DCP sidecar JSON with empty compressions and zeroed stats. |

### DCP Sidecar Format

```json
{
  "version": 1,
  "sessionId": "<session-id>",
  "savedAt": 0,
  "nextCompressionId": 1,
  "turnIndex": 0,
  "compressions": [],
  "dedupedCallIds": [],
  "purgedErrorCallIds": [],
  "appliedCompressionTargets": [],
  "erroredAt": [],
  "stats": {
    "dedupPruned": 0,
    "errorInputsPurged": 0,
    "compressionsApplied": 0,
    "tokensSaved": 0
  }
}
```

### Default Path

```
<home>/.pi-dcp/sessions/<session-id>.json
```

## Caching Behavior

All `SessionStore` subclasses cache two data structures after the first access:

- **`_path_cache`**: `list[Path]` -- all session file paths, sorted. Avoids repeated `rglob` calls.
- **`_id_index`**: `dict[str, Path]` -- maps session ID to file path. Enables O(1) `load(session_id)` lookups.

Do not invalidate these caches without reason. If you add or remove session files on disk, create a new store instance.

## See Also

- [Models](models.md) -- `SessionSummary`, `NativeSession`, and other data classes.
- [Paths](paths.md) -- `WindowsDefaults` for default path resolution.
- [Converters](converters.md) -- how stores are used in conversion workflows.
- [Trace Export](traces.md) -- how stores feed trace builders.
