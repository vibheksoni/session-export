# Quick Start

This guide covers listing sessions, converting one session, bulk exporting, and searching via the MCP server. All commands assume you are in the project root.

## 1. List Sessions

List sessions from any supported tool:

```bash
python -m unisessions list codex
python -m unisessions list pi
python -m unisessions list opencode
python -m unisessions list claude
python -m unisessions list devin
python -m unisessions list factory
python -m unisessions list windsurf
```

Output is tab-separated: provider, session ID, timestamp, message count, cwd, file path.

```
codex    01234567-89ab-cdef-0123-456789abcdef    2026-06-11T20:03:35Z    42 messages    C:\Projects\myproject    ...
```

## 2. Convert One Session (Dry Run)

Preview a conversion without writing anything:

```bash
python -m unisessions codex-to-pi 01234567-89ab-cdef-0123-456789abcdef
```

Output shows the source path, session ID, cwd, destination path, and record count. No files are written.

## 3. Convert One Session (Write)

Add `--write` to produce the converted file:

```bash
python -m unisessions codex-to-pi 01234567-89ab-cdef-0123-456789abcdef --write
```

If the destination already exists, use `--on-conflict` to control behavior:

```bash
# Skip if destination exists (default)
python -m unisessions codex-to-pi <id> --write --on-conflict skip

# Replace existing destination
python -m unisessions codex-to-pi <id> --write --on-conflict overwrite

# Generate a new session ID, preserve old file
python -m unisessions codex-to-pi <id> --write --on-conflict fork

# Overwrite only if source has changed
python -m unisessions codex-to-pi <id> --write --on-conflict update
```

## 4. Bulk Export

Export all Codex sessions to Pi:

```bash
python -m unisessions codex-to-pi-all --write
```

Export to multiple targets at once:

```bash
python -m unisessions export-all --write --targets pi opencode
```

Control parallelism with `--workers` (default 8):

```bash
python -m unisessions export-all --write --targets pi opencode --workers 16
```

## 5. Search via MCP Server

Start the MCP server:

```bash
unisessions-mcp
```

Or with Python module syntax:

```bash
python -m unisessions.mcp_server
```

The server exposes four tools over stdio MCP:

1. **`index_status`** -- check how many sessions need indexing.
2. **`refresh_chats_index`** -- parse sessions into the FTS5 index.
3. **`search_chats`** -- search parsed chat text.
4. **`list_chats`** -- list sessions by provider or cwd.

Recommended workflow for low-latency search:

```
index_status  -->  refresh_chats_index  -->  search_chats (stale_policy="skip")
```

On a 20-session corpus, cold refresh takes ~39-49 seconds. Warm search returns in ~35-40ms.

## All Conversion Commands

There are 42 single-session conversion commands, one for each ordered pair of tools:

| Source | Targets |
|---|---|
| Codex | pi, opencode, claude, devin, factory, windsurf |
| Pi | codex, opencode, claude, devin, factory, windsurf |
| OpenCode | codex, pi, claude, devin, factory, windsurf |
| Claude | pi, codex, opencode, devin, factory, windsurf |
| Devin | pi, codex, opencode, claude, factory, windsurf |
| Factory | pi, codex, opencode, claude, devin, windsurf |
| Windsurf | pi, codex, opencode, claude, devin, factory |

```bash
python -m unisessions <source>-to-<target> <session-id> --write
```

See [CLI Reference](cli.md) for the full flag list.
