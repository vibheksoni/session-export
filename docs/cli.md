# CLI Reference

The CLI is invoked via `python -m unisessions` or the `unisessions` console script. All commands use system default paths unless overridden.

## Commands

### list

List sessions from a specific provider.

```bash
python -m unisessions list <provider> [--workers N]
```

| Argument | Description |
|---|---|
| `provider` | Required. One of `codex`, `pi`, `opencode`, `claude`, `devin`, `factory`, `windsurf`. |
| `--workers N` | Parallel listing workers (default: 1). |

Output is tab-separated: provider, session ID, timestamp, message count, cwd, file path.

### Single-Session Conversion

42 conversion commands, one for each ordered pair of tools:

```bash
python -m unisessions <source>-to-<target> <session-id> [flags]
```

| Command | Source | Target |
|---|---|---|
| `codex-to-pi` | Codex | Pi |
| `codex-to-opencode` | Codex | OpenCode |
| `codex-to-claude` | Codex | Claude Code |
| `codex-to-devin` | Codex | Devin |
| `codex-to-windsurf` | Codex | Windsurf |
| `pi-to-codex` | Pi | Codex |
| `pi-to-opencode` | Pi | OpenCode |
| `pi-to-claude` | Pi | Claude Code |
| `pi-to-devin` | Pi | Devin |
| `pi-to-windsurf` | Pi | Windsurf |
| `opencode-to-codex` | OpenCode | Codex |
| `opencode-to-pi` | OpenCode | Pi |
| `opencode-to-claude` | OpenCode | Claude Code |
| `opencode-to-devin` | OpenCode | Devin |
| `opencode-to-windsurf` | OpenCode | Windsurf |
| `claude-to-pi` | Claude Code | Pi |
| `claude-to-codex` | Claude Code | Codex |
| `claude-to-opencode` | Claude Code | OpenCode |
| `claude-to-devin` | Claude Code | Devin |
| `claude-to-windsurf` | Claude Code | Windsurf |
| `devin-to-pi` | Devin | Pi |
| `devin-to-codex` | Devin | Codex |
| `devin-to-opencode` | Devin | OpenCode |
| `devin-to-claude` | Devin | Claude Code |
| `devin-to-windsurf` | Devin | Windsurf |
| `windsurf-to-pi` | Windsurf | Pi |
| `windsurf-to-codex` | Windsurf | Codex |
| `windsurf-to-opencode` | Windsurf | OpenCode |
| `windsurf-to-claude` | Windsurf | Claude Code |
| `windsurf-to-devin` | Windsurf | Devin |
| `windsurf-to-factory` | Windsurf | Factory |
| `factory-to-pi` | Factory | Pi |
| `factory-to-codex` | Factory | Codex |
| `factory-to-opencode` | Factory | OpenCode |
| `factory-to-claude` | Factory | Claude Code |
| `factory-to-devin` | Factory | Devin |
| `factory-to-windsurf` | Factory | Windsurf |

#### Flags

| Flag | Description |
|---|---|
| `session_id` | Positional. The source session ID to convert. |
| `--write` | Write the converted session. Without this, only a plan is printed. |
| `--overwrite` | Allow replacing an existing destination. Alias for `--on-conflict overwrite`. |
| `--new-id` | Generate a new target session ID instead of preserving the source ID. |
| `--on-conflict {skip,overwrite,fork,update}` | Conflict resolution mode (default: `skip`). |

### codex-to-pi-all

Bulk export all Codex sessions to Pi.

```bash
python -m unisessions codex-to-pi-all [--write] [--workers N] [--on-conflict MODE]
```

### export-all

Bulk export all Codex sessions to one or more targets.

```bash
python -m unisessions export-all --targets pi opencode claude devin factory windsurf [--write] [--workers N] [--on-conflict MODE]
```

| Flag | Description |
|---|---|
| `--targets {pi,opencode,claude,devin,factory,windsurf} [...]` | Target formats. Default: `pi`. |
| `--write` | Write converted sessions. Without this, prints a dry-run summary. |
| `--workers N` | Parallel workers (default: 8). |
| `--overwrite` | Alias for `--on-conflict overwrite`. |
| `--new-id` | Generate new target session IDs. |
| `--on-conflict {skip,overwrite,fork,update}` | Conflict resolution mode (default: `skip`). |

### to-trace

Export any session as a trace file in HuggingFace STS, OpenAI fine-tuning, or ShareGPT format.

```bash
python -m unisessions to-trace <provider> <session-id> --format <format> [--write] [-o FILE]
```

| Argument | Description |
|---|---|
| `provider` | Source provider: `codex`, `pi`, `opencode`, `claude`, `devin`, `factory`, `windsurf`. |
| `session_id` | Session ID to export. |
| `--format` | Trace format: `sts` (default), `openai`, `sharegpt`. |
| `--output`, `-o` | Output file path (required with `--write`). |
| `--write` | Write to file instead of stdout. |

See [Trace Export](traces.md) for format details and SDK usage.

## Conflict Resolution

When a destination file already exists, `--on-conflict` controls behavior:

| Mode | Behavior |
|---|---|
| `skip` (default) | Skip if destination exists. |
| `overwrite` | Replace existing destination with new content. |
| `fork` | Generate a new UUID session ID, preserve old file untouched. |
| `update` | Check `has_changes()` -- skip if unchanged, overwrite if changed. |

`--overwrite` is kept as a backward-compatible alias for `--on-conflict overwrite`.

### Change Detection

The `update` mode uses `has_changes()` which reads the source file's head metadata (session ID, timestamp, cwd) and counts source file lines (for JSONL) or messages array length (for OpenCode), then compares against the destination. This avoids the expensive full parse + extract + build cycle. On a 913 MB file with 155K records, this takes <0.1s instead of ~36s.

## Path Override Flags

All commands accept these flags to override system default paths:

| Flag | Default | Description |
|---|---|---|
| `--codex-home` | `<home>/.codex` | Codex home root. |
| `--codex-session-dir` | `<codex-home>/sessions` | Root containing Codex rollout JSONL files. |
| `--pi-agent-home` | `<home>/.pi/agent` | Pi agent home. |
| `--pi-session-dir` | `<pi-agent-home>/sessions` | Root containing Pi session JSONL files. |
| `--pi-dcp-home` | `<home>/.pi-dcp` | Pi DCP home root. |
| `--opencode-data-home` | `%APPDATA%/opencode` | OpenCode data home. |
| `--opencode-session-dir` | `<opencode-data-home>/session-export` | Root containing OpenCode export JSON files. |
| `--claude-home` | `<home>/.claude` | Claude Code home root. |
| `--claude-session-dir` | `<claude-home>/projects` | Root containing Claude Code session JSONL files. |
| `--devin-home` | `%APPDATA%/devin` | Devin home root. |
| `--devin-session-dir` | `<devin-home>/cli/transcripts` | Root containing Devin transcript JSON files. |
| `--windsurf-home` | `<home>/.codeium/windsurf` | Windsurf home root. |
| `--windsurf-session-dir` | `<windsurf-home>/cascade` | Root containing Windsurf Cascade `.pb` files. |

When a session directory override is provided, the store scans that directory directly instead of the tool's default location. This is useful for testing, custom installations, or working with exported session archives.

## Examples

### List with parallel workers

```bash
python -m unisessions list codex --workers 4
```

### Convert with fork

```bash
python -m unisessions codex-to-pi 01234567-89ab-cdef-0123-456789abcdef --write --on-conflict fork
```

### Bulk export with update mode

```bash
python -m unisessions export-all --write --targets pi opencode --on-conflict update --workers 16
```

### Convert with custom paths

```bash
python -m unisessions codex-to-pi <id> --write --codex-session-dir /backup/codex/sessions --pi-session-dir /backup/pi/sessions
```

## See Also

- [Quick Start](quickstart.md) -- common workflows.
- [Converters](converters.md) -- the converter classes behind each command.
- [Stores](stores.md) -- how path overrides map to store constructors.
