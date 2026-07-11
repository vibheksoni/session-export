# Paths

The `session_sdk/paths.py` module provides default path resolution, session ID generation, and encoding helpers for all supported tools.

## WindowsDefaults

Resolves default filesystem paths for each tool based on the user's home directory and environment variables.

```python
from session_sdk.paths import WindowsDefaults

defaults = WindowsDefaults()
# Or with a custom home:
defaults = WindowsDefaults(home=Path("/custom/home"))
```

### Properties

| Property | Path | Env Override |
|---|---|---|
| `codex_home` | `<home>/.codex` | -- |
| `pi_home` | `<home>/.pi` | -- |
| `pi_agent_home` | `<home>/.pi/agent` | -- |
| `pi_dcp_home` | `<home>/.pi-dcp` | -- |
| `opencode_data_home` | Windows: `%APPDATA%/opencode`, Linux: `$XDG_DATA_HOME/opencode` or `~/.local/share/opencode` | `OPENCODE_GLOBAL_DATA_DIR` |
| `claude_home` | `<home>/.claude` | `CLAUDE_CONFIG_DIR` |
| `devin_home` | Windows: `%APPDATA%/devin`, Linux: `~/.config/devin` | `DEVIN_CONFIG_DIR` |
| `opencode_session_dir` | `<opencode_data_home>/session-export` | -- |

### Platform Behavior

`opencode_data_home` branches on `sys.platform`:
- **Windows**: `%APPDATA%/opencode` (or `<home>/AppData/Roaming/opencode` as fallback).
- **Linux/other**: `$XDG_DATA_HOME/opencode` (or `~/.local/share/opencode` as fallback).

`claude_home` checks `CLAUDE_CONFIG_DIR` first, then falls back to `<home>/.claude`.

## SessionIdFactory

Controls whether session IDs are preserved or regenerated during conversion.

```python
from session_sdk.paths import SessionIdFactory

# Preserve source IDs (default)
factory = SessionIdFactory(preserve_ids=True)

# Generate new UUIDs for every conversion
factory = SessionIdFactory(preserve_ids=False)
```

### Methods

| Method | Behavior |
|---|---|
| `create(source_id)` | Returns `source_id` if `preserve_ids=True`, otherwise a new UUID v4. Used for Pi and Claude targets. |
| `create_codex(source_id)` | Returns `source_id` if it is a valid UUID and `preserve_ids=True`, otherwise a new UUID v4. Used for Codex targets. |
| `create_opencode(source_id, timestamp)` | Returns `source_id` if it starts with `ses_` and `preserve_ids=True`, otherwise generates a new OpenCode-style descending ID via `opencode_id("ses", timestamp)`. |

## encode_pi_cwd

Encodes a cwd path into Pi's directory naming convention.

```python
from session_sdk.paths import encode_pi_cwd

encode_pi_cwd("C:\\Projects\\myproject")
# --> "--C--Projects-myproject--"
```

Steps:
1. Strip Windows `\\?\` extended-length path prefix if present.
2. Resolve the path via `Path.resolve()`.
3. Strip `\\?\` prefix again (resolve may re-add it on Windows).
4. Strip leading `/` or `\`.
5. Replace `/`, `\`, and `:` with `-`.
6. Wrap with `--` prefix and suffix.

The `\\?\` prefix stripping is critical because it produces invalid `?` characters in directory names.

## sanitize_claude_cwd

Sanitizes a cwd path for Claude Code's directory naming.

```python
from session_sdk.paths import sanitize_claude_cwd

sanitize_claude_cwd("C:\\Projects\\myproject")
# --> "C--Projects-myproject"
```

Strips `\\?\` prefix, then replaces every non-alphanumeric character with `-`.

## opencode_id

Generates OpenCode-style session/message/part IDs.

```python
from session_sdk.paths import opencode_id

session_id = opencode_id("ses", "2026-06-11T20:03:35Z")
# --> "ses_<12-char-hex><14-char-base62>"
```

Format: `<prefix>_<12-char-hex-timestamp><14-char-random-base62>`

- The hex portion encodes `epoch_ms * 0x1000 + 1` as a 48-bit value.
- For `ses` prefix, bitwise NOT is applied to produce descending IDs matching OpenCode's reverse chronological sort order.
- Prefixes: `ses` (sessions), `msg` (messages), `prt` (parts).

## opencode_slug

Generates a URL-safe slug from text (used for OpenCode session titles).

```python
from session_sdk.paths import opencode_slug

opencode_slug("Fix the login bug in auth.py")
# --> "fix-the-login-bug-in-auth-py"
```

Converts to lowercase, replaces non-alphanumeric characters with `-`, collapses consecutive dashes, and takes up to 8 dash-separated segments.

## Timestamp Helpers

### iso_to_epoch_ms

```python
from session_sdk.paths import iso_to_epoch_ms

iso_to_epoch_ms("2026-06-11T20:03:35Z")
# --> 1779907415000 (integer milliseconds)
```

Converts an ISO 8601 string to epoch milliseconds. Returns current time if the string is empty or unparseable.

### epoch_ms_to_iso

```python
from session_sdk.paths import epoch_ms_to_iso

epoch_ms_to_iso(1779907415000)
# --> "2026-06-11T20:03:35.000Z"
```

Converts epoch milliseconds to an ISO 8601 string with millisecond precision and `Z` suffix.

## Filename Timestamp Helpers

### pi_filename_timestamp

```python
from session_sdk.paths import pi_filename_timestamp

pi_filename_timestamp("2026-06-11T20:03:35.686Z")
# --> "2026-06-11T20-03-35-686Z"
```

Replaces `:` and `.` with `-` for filesystem-safe timestamps in Pi filenames.

### codex_filename_timestamp

```python
from session_sdk.paths import codex_filename_timestamp

codex_filename_timestamp("2026-06-11T20:03:35.686Z")
# --> "2026-06-11T20-03-35-686"
```

Same as Pi but also strips the trailing `Z`.

### codex_date_parts

```python
from session_sdk.paths import codex_date_parts

codex_date_parts("2026-06-11T20:03:35Z")
# --> ("2026", "06", "11")
```

Extracts year, month, and day from an ISO timestamp for building Codex's `YYYY/MM/DD` directory structure.

## is_uuid

```python
from session_sdk.paths import is_uuid

is_uuid("01234567-89ab-cdef-0123-456789abcdef")  # True
is_uuid("not-a-uuid")                              # False
```

Validates whether a string is a parseable UUID.

## See Also

- [Stores](stores.md) -- how `WindowsDefaults` feeds into store constructors.
- [Converters](converters.md) -- how `SessionIdFactory` and path helpers are used in conversion.
