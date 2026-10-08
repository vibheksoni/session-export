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

## WindsurfStore

```python
from session_sdk.stores import WindsurfStore

store = WindsurfStore(defaults.windsurf_home)
# Or with a custom session directory:
store = WindsurfStore(defaults.windsurf_home, session_dir=Path("/custom/cascade"))
```

| Method | Description |
|---|---|
| `list(workers=1)` | Scan `cascade/` directory for `.pb` files. Decrypts and parses protobuf to count messages. |
| `list_metadata(workers=1)` | Same as `list()` (metadata from protobuf trajectory header). |
| `load(session_id)` | Find by cascade ID (UUID v4) in filename. |
| `load_path(path)` | Decrypt and parse a Windsurf Cascade `.pb` file. |
| `destination_path(session_id)` | Compute target: `cascade/<cascade_id>.pb`. |
| `write(path, records, overwrite=False)` | Write encrypted protobuf `.pb` file atomically. |

### Windsurf Cascade Format

Windsurf Cascade stores sessions as AES-256-GCM encrypted protobuf `.pb` files.

- **Encryption**: AES-256-GCM with key `safeCodeiumworldKeYsecretBalloon` hardcoded in Windsurf's `language_server` binary.
- **Protobuf type**: `CortexTrajectory` with repeated `CortexTrajectoryStep` messages.
- **Step variants**: field 19 = `user_input`, field 20 = `planner_response`, field 30 = `checkpoint`/compaction.
- **Session IDs**: trajectory UUIDs (field 1 in protobuf). Files are named by `cascade_id`.
- **Decryption requires** the `cryptography` package (`pip install cryptography`).

### Default Paths

```
<home>/.codeium/windsurf/cascade/<cascade_id>.pb
```

Where `<home>/.codeium/windsurf` is used unless `WINDSURF_CONFIG_DIR` is set.

## GrokStore

```python
from session_sdk.stores import GrokStore

store = GrokStore(defaults.grok_home)
# Or with a custom session directory:
store = GrokStore(defaults.grok_home, session_dir=Path("/custom/grok/sessions"))
```

| Method | Description |
|---|---|
| `list(workers=1)` | Scan `sessions/<encoded-cwd>/<session-id>/` directories for `updates.jsonl` or `summary.json`. |
| `list_metadata(workers=1)` | Same as `list()` (metadata from `summary.json`). |
| `load(session_id)` | Find by session directory name or `summary.json` `info.id`. |
| `load_path(path)` | Load a Grok session directory (reads `summary.json` + `updates.jsonl`). |
| `destination_path(session_id, cwd)` | Compute target: `sessions/<url-encoded-cwd>/<session-id>/`. |
| `write(path, records, overwrite=False)` | Write `updates.jsonl` atomically + derived `summary.json`. |

### Grok Build Format

Grok Build (the `grok` CLI) stores each session as a directory:

```
<grok-home>/sessions/<url-encoded-cwd>/<session-id>/
    summary.json      # metadata: info.id, info.cwd, timestamps, model id, message counts
    updates.jsonl     # ACP session update envelopes, one per line
```

- **Envelope shape**: `{"timestamp", "method": "session/update", "params": {"sessionId", "update": {"sessionUpdate": "user_message_chunk" | "agent_message_chunk" | ..., "messageId", "content": {"type": "text", "text"}}}}`.
- **CWD encoding**: group directories are the URL-encoded cwd (`encode_grok_cwd_dirname`). When the encoded form exceeds 255 bytes, a slug-hash fallback name is used and the original path is recorded in a `.cwd` file inside the group.
- **Session IDs**: the session directory name (also `info.id` in `summary.json`).

### Default Paths

```
<grok-home>/sessions/<url-encoded-cwd>/<session-id>/updates.jsonl
<grok-home>/sessions/<url-encoded-cwd>/<session-id>/summary.json
```

Where `<grok-home>` is `$GROK_HOME` if set, otherwise `~/.grok`.

## FreebuffStore

```python
from session_sdk.stores import FreebuffStore

store = FreebuffStore(defaults.freebuff_home)
# Or with a custom project directory:
store = FreebuffStore(defaults.freebuff_home, session_dir=Path("/custom/freebuff/projects"))
```

| Method | Description |
|---|---|
| `list(workers=1)` | Scan `projects/<slug>-<id>/` directories for `desktop-v2.db` files. Reads `threads` rows + counts `messages` per thread. |
| `list_metadata(workers=1)` | Same as `list()` (metadata from the SQLite `threads` table). |
| `load(session_id)` | Find by thread id in the `threads` table. |
| `load_path(path)` | Load a project `desktop-v2.db` file (resolves the thread id from the db). |
| `destination_path(session_id, cwd)` | Compute target: `projects/<slug(cwd)>-<id8>/desktop-v2.db`. |
| `write(path, records, overwrite=False)` | Create the project DB (`projects`/`threads`/`messages` tables) + `project.json`. |

### Freebuff Desktop Format

Freebuff Desktop (freebuff.com, by CodebuffAI) stores each session as a per-project SQLite database that Freebuff Desktop keeps open, so the store accesses it read-only:

```
{freebuff_home}/projects/<slug>-<project-id>/
    project.json      # version, projectId, projectPath, database
    desktop-v2.db     # SQLite: projects, threads, messages
```

- **`threads`**: session headers -- `id`, `project_id`, `project_path` (= cwd), `title`, `status`, `model`, `reasoning_effort`, `agent_mode`, `execution_mode`, `branch`, `worktree_path`, `fork_source_thread_id`, `created_at`/`updated_at` (epoch ms).
- **`messages`**: one row per message -- `seq` (autoincrement), `thread_id`, `request_id`, `input_id`, `role`, `parts_json` (kind-tagged array), `attachments_json`, `metrics_json`, `ts` (epoch ms).
- **`parts_json` kinds**: `text` (chat text), `reasoning` (thinking), `tool` (tool invocation), `changes` (file diffs), `ad` (sponsored). Only `text` parts carry the conversation; the rest are skipped by the text-history extractor.
- **Session IDs**: thread ids from the `threads` table.

### Read Safety

Freebuff Desktop may hold `desktop-v2.db` open with a WAL journal. `FreebuffStore` opens the database read-only (`mode=ro` URI + `PRAGMA query_only=ON`), which reads the WAL without taking a lock. If the read-only open fails (e.g. exclusive locks), it falls back to the SQLite Online Backup API -- snapshotting the database into a temp file and opening the snapshot.

### Default Paths

```
{freebuff_home}/projects/<slug>-<project-id>/desktop-v2.db
{freebuff_home}/projects/<slug>-<project-id>/project.json
```

Where `{freebuff_home}` is `$FREEBUFF_CONFIG_DIR` if set, otherwise `~/.config/freebuff-desktop`.

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

## T3Store

T3 Code (t3.codes) keeps its data under `<T3 home>/userdata`. `T3Store` reads it read-only.

```python
from session_sdk import T3Store, WindowsDefaults

store = T3Store(WindowsDefaults().t3_home)
for summary in store.list():
    print(summary.session_id, summary.cwd, summary.message_count)
session = store.load("<thread-id>")
```

### Databases

- `statev2.sqlite` (current). Threads come from `orchestration_v2_projection_threads` and messages from `orchestration_v2_projection_messages`. The full object of each row is in `payload_json`.
- `state.sqlite` (legacy V1). Threads come from `projection_threads` and messages from `projection_thread_messages`.

A thread comes from `statev2.sqlite` when that database has it, otherwise from `state.sqlite`. Threads that exist only in the legacy file are still listed. Deleted threads (`deleted_at` set) are hidden.

### Read-only

Both databases are opened with a `mode=ro` URI, so the store never writes to them. T3 keeps the live database open while the app runs, so the store only reads it.

### Limits

- Only text is read: user and assistant messages. Tool calls, approvals, checkpoints, and attachments are not converted.
- Writing into T3 is experimental. See [Writing into T3](#writing-into-t3-experimental).
- Each thread's virtual path is `<database>/<thread id>`. The search index treats the database file's stat as the thread's stat, so any change to the database re-indexes every T3 thread.
- Non-UUID thread ids become a stable UUIDv5 in the target format (T3 ids contain colons, which Windows does not allow in file names). Exporting the same thread twice writes the same file.

### Writing into T3 (experimental)

`<provider>-to-t3` writes a session as a T3 thread. It may not match every T3 release, so
check the result in T3 before you rely on it.

```sh
python -m unisessions --t3-home /path/to/new-t3-home pi-to-t3 <session-id> --write --t3-provider codex
```

How it works:

- T3 creates `statev2.sqlite` on its first launch. When it does, it copies the legacy
  `userdata/state.sqlite` and imports every thread in it. So unisessions writes the legacy V1
  schema, and T3 does the rest. Writing V2 events directly is not done.
- The schema comes from `session_sdk/data/t3_state_v1.sql`. It was generated from the T3
  checkout at commit `9a3070b` by running T3's own migrations 1 to 54 on an empty database.
  Migrations 55 and later are not in the file, because
  T3 creates them itself.
- The thread keeps the session's title (first user message), working directory (the project's
  workspace root), and user and assistant text in order. Tool calls, approvals, checkpoints, and
  attachments are not written. Contextual messages are dropped, and compaction summaries become
  assistant messages.
- The thread runs on the `--t3-provider` driver with that driver's default model, because model
  names do not carry across providers. It starts with `approval-required` runtime mode, so the
  imported history cannot act on its own.
- Thread and message ids are stable, so exporting the same session twice writes the same
  thread. `--on-conflict update` skips threads whose message count has not changed.

Guards. A write is refused when:

- `--t3-home` is missing (the write command exits with status 2).
- The home already has `statev2.sqlite`. T3 imports legacy threads only when it creates that file,
  and appending to an existing home was checked to import nothing. Use a new, empty home. Several
  sessions can go into the same new home before the first launch.
- The target is the live T3 data directory (`$T3CODE_HOME/userdata` or `~/.t3/userdata`).
- The target database exists but was not created by unisessions. Unisessions sets the SQLite
  `application_id` to `0x554E4953` ("UNIS"), which T3 ignores.

A write goes to a staging file that is moved into place only when it succeeds. A failed write
leaves no `state.sqlite`.

Verify a write:

1. `python -m unisessions --t3-home <new home> pi-to-t3 <id> --write`
2. Start T3 against the new home: `T3CODE_HOME=<new home> node apps/server/src/bin.ts --no-browser`.
   The server log should show `Imported legacy v1 thread shells`.
3. Check `<new home>/userdata/statev2.sqlite` or open the thread in T3.

Regenerate the schema template after a T3 migration change. In a T3 checkout, write a short
script that runs `runMigrations({ toMigrationInclusive: 54 })` from `apps/server/src/persistence/Migrations.ts`
on an empty SQLite file. Then dump the non-internal `sqlite_master` SQL and every row of
`effect_sql_migrations` as SQL statements. Replace `session_sdk/data/t3_state_v1.sql` with the output,
keeping the header comment. The script is not kept in this repo, because the Python package does
not depend on it.

