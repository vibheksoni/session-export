# UniSessions

UniSessions converts and syncs AI coding agent sessions between five tools: **Codex**, **Pi**, **OpenCode**, **Claude Code**, and **Devin** (Windsurf CLI). It preserves user/assistant text history, compaction summaries, and session metadata so you can resume a conversation in a different tool without losing context.

## What It Does

- **Convert** individual sessions between any pair of supported tools (20 conversion directions).
- **Bulk export** all Codex sessions to Pi, OpenCode, Claude Code, or Devin in one command.
- **Export traces** in HuggingFace STS, OpenAI fine-tuning, or ShareGPT format for Hub upload or model training.
- **Search** across all sessions with a SQLite FTS5 index, exposed via CLI or MCP server.
- **Preserve** compaction markers, session IDs, timestamps, and project paths across formats.
- **Estimate tokens** for Pi imports using tiktoken with a char-based fallback.

## Packages

| Package | Purpose |
|---|---|
| `session_sdk` | Typed library with stores, converters, models, paths, and search. No CLI dependencies. |
| `unisessions` | CLI and MCP server built on top of the SDK. |

## Documentation

- [Installation](installation.md) -- clone, Python version, optional dependencies.
- [Quick Start](quickstart.md) -- list, convert, bulk export, and search in 5 minutes.
- [Stores](stores.md) -- filesystem store classes for each tool.
- [Converters](converters.md) -- all 20 converter classes and the conversion plan.
- [Models](models.md) -- data classes for sessions, messages, and plans.
- [Paths](paths.md) -- default paths, ID generation, and encoding helpers.
- [Search](search.md) -- FTS5 search engine and result types.
- [Trace Export](traces.md) -- HuggingFace STS, OpenAI, and ShareGPT trace formats.
- [MCP Server](mcp-server.md) -- tools, transports, and environment variables.
- [CLI Reference](cli.md) -- all commands, flags, and conflict resolution modes.
- [Architecture](architecture.md) -- package structure, dependency rules, and data flow.
- [Data Fidelity](data-fidelity.md) -- what is preserved and what is not.

## Quick Example

```bash
# List Codex sessions
python -m unisessions list codex

# Convert one Codex session to Pi (dry run)
python -m unisessions codex-to-pi 01234567-89ab-cdef-0123-456789abcdef

# Write the converted session
python -m unisessions codex-to-pi 01234567-89ab-cdef-0123-456789abcdef --write

# Bulk export all Codex sessions to Pi and OpenCode
python -m unisessions export-all --write --targets pi opencode
```
