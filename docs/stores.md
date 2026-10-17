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

## OmpStore

```python
from session_sdk.stores import OmpStore

store = OmpStore(defaults.omp_agent_home)
# Or with a custom sessions root:
store = OmpStore(defaults.omp_agent_home, session_dir=Path("/custom/omp/sessions"))
```

| Method | Description |
|---|---|
| `list(workers=1)` | Scan `sessions/<bucket>/*.jsonl` files. Parses each file for a message count. |
| `list_metadata(workers=1)` | Read only the header (first line, or second line after the title slot). |
| `load(session_id)` | Find by session id from the header or the file name. |
| `load_path(path)` | Load a specific JSONL file. |
| `destination_path(session_id, timestamp, cwd)` | Compute target: `sessions/<bucket>/<timestamp>_<session-id>.jsonl`. |
| `write(path, records, overwrite=False)` | Write JSONL records. A leading title slot must be exactly 256 bytes. |

### OMP Format

OMP (oh-my-pi, a fork of the Pi coding agent) stores one JSONL file per session:

```
{omp_agent_home}/sessions/<bucket>/<timestamp>_<session-id>.jsonl
```

- **Line 1**: a `title` slot. It is exactly 256 bytes including the newline, padded with spaces, so omp can rewrite the title in place. Build it with `omp_title_slot()` (`OMP_TITLE_SLOT_BYTES` is 256). Older files have no slot; they are read too.
- **Line 2**: the `session` header -- `version: 3`, `id`, `timestamp`, `cwd`, and optional `title`/`titleSource`. The slot title wins over the header title.
- **Later lines**: entries linked by `id`/`parentId`. They form a tree; rewinding a session adds a branch in the same file.
- **Session IDs**: UUIDs (omp mints UUIDv7).

Only `<root>/*/*.jsonl` files are sessions. Deeper files, such as subagent transcripts in `<bucket>/<timestamp>_<id>/`, are skipped, as omp does.

### Buckets

`encode_omp_cwd(cwd)` mirrors omp's `getDefaultSessionDirName`. The path is canonicalized first (symlinks resolved):

- Inside the temp root (Node `os.tmpdir()`: `TMPDIR`, `TMP`, `TEMP`, or `/tmp`): `-tmp-<relative>`.
- Inside the home directory: `-<relative>`. The home directory itself is `-`.
- Anywhere else: `--<absolute>--`.

`/`, `\`, and `:` become `-`. A cwd that is not absolute on the current platform is encoded as written. For example, `~/projects/demo-app` becomes `-projects-demo-app` and `/srv/app` becomes `--srv-app--`.

### Reading

`MessageExtractor.from_omp` reads only the active branch: from the root to omp's leaf, which is the last entry or the entry a `model_usage` record's `activeLeafId` points to.

- User and assistant text is kept. Compaction summaries become compaction messages.
- A `/skill:` request or a collaborator's prompt is stored as a user-attributed `custom_message` (`skill-prompt`, `collab-prompt`) and is kept as a user message, as omp counts it.
- `developer` messages (system reminders), `synthetic` user messages, other `custom_message` entries, and `branch_summary` summaries are contextual: they are indexed for search and dropped on export.
- A `reset_boundary` (`/clear`) takes everything before it out of omp's context, so those messages are contextual too.
- Assistant `thinking` blocks are kept as reasoning. `toolCall` blocks are kept as tool calls, and the `toolResult` message with the same `toolCallId` becomes the call's output, with its `isError` flag. `bashExecution` and `fileMention` are skipped.
- The model and provider come from the assistant message, else from the latest default-role `model_change` (`model: "provider/id"`; Pi's `provider`/`modelId` are read too).

### Writing

`OmpRecordBuilder` writes the title slot, the header, and one `message` entry per user or assistant message, each the child of the previous one.

- The title is the first line of the first user message (at most 80 characters, whitespace collapsed).
- A compaction summary becomes a `compaction` entry whose `firstKeptEntryId` is its own id. It keeps nothing before it, so a resumed session starts from the summary.
- Assistant messages keep the source provider, model, and api when known. Otherwise they use `session-export` / `imported` / `openai-completions`, the same placeholders as the Pi builder.
- Claude Code records the model but not the provider, so replies from Claude sources are written as provider `anthropic`, api `anthropic-messages`, as omp's own Claude importer does.
- Thinking is written as an unsigned `thinking` block. Tool calls are `toolCall` blocks, with `stopReason: "toolUse"` on the turn, and each one is followed by its `toolResult` message, as omp writes them. A call with no output gets the placeholder `(no output recorded)`.
- Token usage is zero.
- A final `custom` entry `foreign_session_import` records `{source, sourceId, sourcePath, sourceCwd}`. omp's own importers write the same entry, and it is never sent to a model.
- Timestamps are normalized to `YYYY-MM-DDTHH:MM:SS.mmmZ`.
- Non-UUID source ids map to a stable UUID, which keeps `_` out of the file name. A source with no cwd goes to the home bucket. Output is deterministic.

omp itself ships `omp --from-claude` and `omp --from-codex` to import those two sources. Unisessions adds the other sources, OMP as a source, search, and traces.

### Resuming

omp resumes a session on the model its last reply names. When that model is not available, interactive omp falls back to the configured model with a warning. `omp -p` and RPC mode refuse to resume (`Could not restore model ...`) unless you pass `--model`:

```sh
omp --resume <session-id> --model <provider/model> -p "continue"
```

### Default Paths

```
{omp_agent_home}/sessions/<bucket>/<timestamp>_<session-id>.jsonl
```

Where `{omp_agent_home}` is resolved like omp does: `~/.omp/agent` by default. `PI_CONFIG_DIR` renames `.omp`. `OMP_PROFILE` (or legacy `PI_PROFILE`) selects `~/.omp/profiles/<name>/agent`. `PI_CODING_AGENT_DIR` overrides the agent dir when no profile is active. On Linux and macOS, `$XDG_DATA_HOME/omp` is used when that folder exists (after `omp config init-xdg`).

## CommandCodeStore

Reads and writes Command Code sessions, the coding agent that ships as `command-code` (`cmdc`). Every file version the CLI reads is accepted. Writes always use version 3, the layout the current CLI writes.

```python
from pathlib import Path
from session_sdk import CommandCodeStore

store = CommandCodeStore(Path.home() / ".commandcode")
for summary in store.list():
    print(summary.session_id, summary.cwd, summary.message_count)
```

### Command Code Format

A session is `projects/<encoded cwd>/<session-id>.jsonl`. The folder is the cwd lowercased, with each run of other characters as `-` (`C:\Users\win\Desktop\app` becomes `c-users-win-desktop-app`). A session with no cwd goes to `root`.

Three layouts exist on disk. The CLI reads all three and rewrites an old file as version 3 the first time it opens it. The store converts in memory and never changes the source file.

| Version | Layout | How it is read |
|---|---|---|
| 3 | A `session` header line (`version: 3`, `id`, `timestamp`, `cwd`), then entries linked by `parentId`. Messages are `{"type": "message", "message": {"role", "content"}}` with Anthropic-style blocks (`text`, `thinking`, `tool_use`, `tool_result`). | Read as written. |
| 2 | One flat line per message with `metadata.version: 2`, AI SDK style parts (`reasoning`, `tool-call`, `tool-result`), and tool results in `tool` messages. | Converted to version 3 messages. |
| 1 | Plain `{"role", "content"}` Anthropic lines, with no ids and no metadata. | Converted to version 3 messages, with tool results paired to their calls. |

A file written by a newer Command Code (header `version` above 3) is refused with `CommandCodeFormatError`. `list()` skips it with a warning.

Sidecar files (`.meta.json`, `.checkpoints.jsonl`, `.prompts.jsonl`) sit beside each session and are not sessions. The `model` in `.meta.json` names the model of a version 1 or 2 file's last reply, since those files do not record it.

Version 1 and 2 files have no header, so they carry no cwd. The store takes the cwd from a version 3 file in the same folder. A folder with only old files has no cwd, and its sessions convert with an empty cwd.

### Reading

`MessageExtractor.from_commandcode` reads the branch from the first record to the last entry, which is the conversation the CLI resumes. Thinking and tool calls are kept, and each `tool_result` becomes the output of its call. A `compaction` entry, or a user message flagged `isSummary`, becomes a compaction message. User messages flagged `isMeta`, `custom_message` entries and `branch_summary` entries are contextual.

### Writing

`CommandCodeRecordBuilder` writes the header, a `session_info` entry with the title (the first line of the first user message), a `model_change` entry when a reply names a model with a provider prefix (`provider/model`), then one `message` entry per turn.

- An assistant turn holds `thinking` (with an empty `signature`), `text` and `tool_use` blocks.
- The tool results follow as one user message of `tool_result` blocks, as the CLI writes them. A call with no output gets `(no output recorded)`.
- A compaction summary becomes a `compaction` entry whose `firstKeptEntryId` is its own id, so a resumed session starts from the summary.
- Contextual messages are dropped. A final `custom` entry `foreign_session_import` records the source, which the CLI never sends to a model.

Model names without a provider prefix (for example `claude-opus-4-6` from Claude Code) are left off, so the CLI uses its default model.

### Default Paths

| Platform | Path |
|---|---|
| Windows | `%USERPROFILE%\.commandcode\projects` |
| Linux/macOS | `~/.commandcode/projects` |

The `--commandcode-home` flag replaces `~/.commandcode`. `--commandcode-session-dir` replaces the `projects` folder.

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

