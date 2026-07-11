# UniSessions

UniSessions is an SDK-first AI CLI session converter for moving sessions between Codex, Claude Code, Pi, OpenCode, Devin, Factory, and Windsurf Cascade, with trace export for HuggingFace and fine-tuning, plus an MCP chat recall server built on top.

<div align="center">

```
                                                                   
 ▄▄▄  ▄▄            ▄▄▄▄▄                                         
█▀██  ██           ██▀▀▀▀█▄                                       
  ██  ██  ▄     ▀▀ ▀██▄  ▄▀                   ▀▀       ▄          
  ██  ██  ████▄ ██   ▀██▄▄  ▄█▀█▄ ▄██▀█ ▄██▀█ ██ ▄███▄ ████▄ ▄██▀█
  ██  ██  ██ ██ ██ ▄   ▀██▄ ██▄█▀ ▀███▄ ▀███▄ ██ ██ ██ ██ ██ ▀███▄
  ▀█████▄▄██ ▀█▄██ ▀██████▀▄▀█▄▄▄█▄▄██▀█▄▄██▀▄██▄▀███▀▄██ ▀██▄▄██▀

```

**Convert one AI CLI session format into another across Codex, Pi, OpenCode, Claude Code, Devin, Factory, and Windsurf Cascade.**

I use a lot of AI coding CLIs Codex Claude Code Pi OpenCode Devin Factory Windsurf and wanted to move a session from one tool into another without losing the useful conversation history

[![PyPI](https://img.shields.io/pypi/v/unisessions?logo=pypi&logoColor=white)](https://pypi.org/project/unisessions/)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11+-blue?logo=python&logoColor=white)](https://www.python.org/)
[![Tests](https://img.shields.io/badge/Tests-32%20passing-green)]()
[![Providers](https://img.shields.io/badge/Providers-7%20(Codex%20%7C%20Pi%20%7C%20OpenCode%20%7C%20Claude%20%7C%20Devin%20%7C%20Factory%20%7C%20Windsurf)-purple)]()
[![Conversions](https://img.shields.io/badge/Conversions-42%20directions-orange)]()
[![Traces](https://img.shields.io/badge/Trace%20Export-3%20formats-blue)]()
[![MCP](https://img.shields.io/badge/MCP-FastMCP-teal?logo=fastapi&logoColor=white)]()
[![License](https://img.shields.io/badge/License-MIT-yellow)]()
[![GitHub stars](https://img.shields.io/github/stars/vibheksoni/session-export?style=social)](https://github.com/vibheksoni/session-export/stargazers)

</div>

---

## Table of Contents

- [Why I built this](#why-i-built-this)
- [Quick Start](#quick-start)
- [Supported AI coding agents](#supported-ai-coding-agents)
- [All 42 conversion directions](#all-42-conversion-directions)
- [How to list sessions](#how-to-list-sessions)
- [How to convert sessions](#how-to-convert-sessions)
- [How to export traces](#how-to-export-traces)
- [How to handle conflicts](#how-to-handle-conflicts)
- [How to bulk export](#how-to-bulk-export)
- [How to use custom paths](#how-to-use-custom-paths)
- [MCP server for agent chat recall](#mcp-server-for-agent-chat-recall)
- [SDK usage](#sdk-usage)
- [Architecture](#architecture)
- [Default session paths](#default-session-paths)
- [Data fidelity](#data-fidelity)
- [Performance](#performance)
- [FAQ](#faq)
- [Contributing](#contributing)
- [Development](#development)
- [License](#license)

## Why I built this

I looked for a tool that could convert one AI CLI session into another AI CLI session format and found nothing so I built one

I also wanted my agent to remember things from my other sessions like if I solved a bug in one project I wanted to tell it hey in that other session I fixed this by doing X and it would go check and learn from it instead of me explaining the same thing again

So this does two things

- Moves your sessions between Codex Pi OpenCode Claude Code Devin Factory and Windsurf Cascade in any direction all 42 combinations
- Exports sessions as traces in HuggingFace STS OpenAI fine-tuning or ShareGPT format for Hub upload or model training
- Lets your agent search through all your old chat history across all projects and providers so it can recall what you did before and learn from it

Then I wanted the project to not be locked into a CLI or MCP its an SDK first so you can use it for other projects to do cool stuff like build a GUI for it or whatever you want the CLI and MCP server are just built on top of the SDK

## Quick Start

Install from PyPI:

```powershell
pip install unisessions
python -m unisessions list codex
python -m unisessions list pi
python -m unisessions list opencode
python -m unisessions list claude
python -m unisessions list devin
python -m unisessions list factory
python -m unisessions list windsurf
```

Or clone the repo:

```powershell
git clone https://github.com/vibheksoni/session-export.git
cd session-export
pip install -e .
python -m unisessions list codex
```

Convert a single session (dry-run first, then write):

```powershell
python -m unisessions codex-to-pi <session-id>
python -m unisessions codex-to-pi <session-id> --write
python -m unisessions claude-to-pi <session-id> --write
python -m unisessions devin-to-pi <session-id> --write
python -m unisessions factory-to-pi <session-id> --write
python -m unisessions windsurf-to-pi <session-id> --write
```

Export a session as a HuggingFace trace:

```powershell
python -m unisessions to-trace codex <session-id> --format sts --write -o trace.jsonl
```

Bulk export all Codex sessions to Pi:

```powershell
python -m unisessions codex-to-pi-all --write --workers 8
```

Search across all sessions via MCP:

```powershell
python -m unisessions.mcp_server
```

## Supported AI coding agents

| Agent | Store | Session Format | Session IDs |
|---|---|---|---|
| [OpenAI Codex](https://github.com/openai/codex) | `codex` | JSONL rollout files under date tree | UUID v7 |
| [Pi](https://github.com/earendil-works/pi-coding-agent) | `pi` | JSONL append-only tree entries in cwd-encoded dirs | UUID v7 |
| [OpenCode](https://github.com/sst/opencode) | `opencode` | Official export/import JSON | `ses_` prefixed |
| [Claude Code](https://docs.anthropic.com/en/docs/claude-code) | `claude` | JSONL transcript files in cwd-sanitized dirs | UUID v4 |
| [Devin](https://windsurf.com/devin) (Windsurf CLI) | `devin` | ATIF transcript JSON + SQLite metadata | slug names |
| [Factory](https://factory.ai) (Droid) | `factory` | JSONL transcript files with session headers | UUID v4 |
| [Windsurf](https://windsurf.com) (Cascade) | `windsurf` | AES-256-GCM encrypted protobuf trajectory files | UUID v4 |

## All 42 conversion directions

| From \ To | Pi | Codex | OpenCode | Claude | Devin | Factory | Windsurf |
|---|---|---|---|---|---|---|---|
| **Codex** | `codex-to-pi` | -- | `codex-to-opencode` | `codex-to-claude` | `codex-to-devin` | `codex-to-factory` | `codex-to-windsurf` |
| **Pi** | -- | `pi-to-codex` | `pi-to-opencode` | `pi-to-claude` | `pi-to-devin` | `pi-to-factory` | `pi-to-windsurf` |
| **OpenCode** | `opencode-to-pi` | `opencode-to-codex` | -- | `opencode-to-claude` | `opencode-to-devin` | `opencode-to-factory` | `opencode-to-windsurf` |
| **Claude** | `claude-to-pi` | `claude-to-codex` | `claude-to-opencode` | -- | `claude-to-devin` | `claude-to-factory` | `claude-to-windsurf` |
| **Devin** | `devin-to-pi` | `devin-to-codex` | `devin-to-opencode` | `devin-to-claude` | -- | `devin-to-factory` | `devin-to-windsurf` |
| **Factory** | `factory-to-pi` | `factory-to-codex` | `factory-to-opencode` | `factory-to-claude` | `factory-to-devin` | -- | `factory-to-windsurf` |
| **Windsurf** | `windsurf-to-pi` | `windsurf-to-codex` | `windsurf-to-opencode` | `windsurf-to-claude` | `windsurf-to-devin` | `windsurf-to-factory` | -- |

## How to list sessions

```powershell
python -m unisessions list codex
python -m unisessions list pi
python -m unisessions list opencode
python -m unisessions list claude
python -m unisessions list devin
python -m unisessions list factory
python -m unisessions list windsurf
```

## How to convert sessions

All commands default to dry-run. Add `--write` to produce output.

```powershell
python -m unisessions codex-to-pi <session-id> --write
python -m unisessions pi-to-codex <session-id> --write
python -m unisessions codex-to-opencode <session-id> --write
python -m unisessions pi-to-opencode <session-id> --write
python -m unisessions opencode-to-codex <session-id> --write
python -m unisessions opencode-to-pi <session-id> --write
python -m unisessions claude-to-pi <session-id> --write
python -m unisessions pi-to-claude <session-id> --write
python -m unisessions claude-to-codex <session-id> --write
python -m unisessions codex-to-claude <session-id> --write
python -m unisessions claude-to-opencode <session-id> --write
python -m unisessions opencode-to-claude <session-id> --write
python -m unisessions devin-to-pi <session-id> --write
python -m unisessions pi-to-devin <session-id> --write
python -m unisessions devin-to-codex <session-id> --write
python -m unisessions codex-to-devin <session-id> --write
python -m unisessions devin-to-opencode <session-id> --write
python -m unisessions opencode-to-devin <session-id> --write
python -m unisessions devin-to-claude <session-id> --write
python -m unisessions claude-to-devin <session-id> --write
python -m unisessions factory-to-pi <session-id> --write
python -m unisessions pi-to-factory <session-id> --write
python -m unisessions factory-to-codex <session-id> --write
python -m unisessions codex-to-factory <session-id> --write
python -m unisessions factory-to-opencode <session-id> --write
python -m unisessions opencode-to-factory <session-id> --write
python -m unisessions factory-to-claude <session-id> --write
python -m unisessions claude-to-factory <session-id> --write
python -m unisessions factory-to-devin <session-id> --write
python -m unisessions devin-to-factory <session-id> --write
python -m unisessions windsurf-to-pi <session-id> --write
python -m unisessions pi-to-windsurf <session-id> --write
python -m unisessions windsurf-to-codex <session-id> --write
python -m unisessions codex-to-windsurf <session-id> --write
python -m unisessions windsurf-to-opencode <session-id> --write
python -m unisessions opencode-to-windsurf <session-id> --write
python -m unisessions windsurf-to-claude <session-id> --write
python -m unisessions claude-to-windsurf <session-id> --write
python -m unisessions windsurf-to-devin <session-id> --write
python -m unisessions devin-to-windsurf <session-id> --write
python -m unisessions windsurf-to-factory <session-id> --write
python -m unisessions factory-to-windsurf <session-id> --write
```

## How to export traces

Export any session as a trace file for HuggingFace Hub upload, model fine-tuning, or training data preparation. Three formats are supported:

| Format | Use case |
|---|---|
| `sts` | HuggingFace Hub trace viewer (Session Trace Simple Format) |
| `openai` | OpenAI / Azure fine-tuning JSONL format |
| `sharegpt` | ShareGPT format for LLaMA-Factory, Axolotl, torchtune |

```powershell
# HuggingFace STS format (default) -- print to stdout
python -m unisessions to-trace codex <session-id> --format sts

# OpenAI fine-tuning format -- write to file
python -m unisessions to-trace pi <session-id> --format openai --write -o train.jsonl

# ShareGPT format from Devin session
python -m unisessions to-trace devin <session-id> --format sharegpt --write -o traces.jsonl
```

Upload to HuggingFace Hub:

```powershell
pip install huggingface-cli
hf upload your-username/your-dataset trace.jsonl
```

The Hub auto-detects the trace format and renders it in the trace viewer. See the [trace export docs](docs/traces.md) for SDK usage and format details.

## How to handle conflicts

When a destination file already exists use `--on-conflict` to control behavior:

| Mode | Behavior |
|---|---|
| `skip` (default) | Skip if destination exists |
| `overwrite` | Replace existing destination with new content |
| `fork` | Generate a new UUID session ID, preserve old file untouched |
| `update` | Skip if unchanged, overwrite if source changed (fast head-meta + line count check) |

```powershell
python -m unisessions codex-to-pi <id> --write --on-conflict fork
python -m unisessions codex-to-pi <id> --write --on-conflict update
```

## How to bulk export

Export all Codex sessions to one or more targets in parallel:

```powershell
python -m unisessions codex-to-pi-all --write --workers 8
python -m unisessions export-all --write --targets pi opencode claude devin factory windsurf --workers 8
```

## How to use custom paths

Use system defaults by omitting path flags. For backups or staging:

```powershell
python -m unisessions --codex-session-dir C:\path\to\sessions list codex
python -m unisessions --pi-session-dir C:\path\to\pi\sessions list pi
python -m unisessions --opencode-session-dir C:\path\to\opencode\exports list opencode
python -m unisessions --claude-session-dir C:\path\to\claude\projects list claude
python -m unisessions --devin-session-dir C:\path\to\devin\transcripts list devin
python -m unisessions --factory-session-dir C:\path\to\factory\sessions list factory
python -m unisessions --windsurf-session-dir C:\path\to\windsurf\cascade list windsurf
```

OpenCode output files use the official import/export JSON shape. Load them with
`opencode import <path-to-json>`.

## MCP server for agent chat recall

UniSessions ships a [FastMCP](https://github.com/jlowin/fastmcp) server that
exposes a SQLite FTS5 full-text search index over parsed session chat history.
AI agents can use it to recall past conversations, find what was discussed, and
search across all providers.

### Setup

```powershell
python -m unisessions.mcp_server
```

### MCP client configuration

```json
{
  "mcpServers": {
    "unisessions": {
      "command": "unisessions-mcp",
      "args": [],
      "env": {
        "UNISESSIONS_SEARCH_INDEX": "C:\\Users\\you\\AppData\\Local\\unisessions\\search.sqlite"
      }
    }
  }
}
```

### HTTP transports for app-managed servers

```powershell
unisessions-mcp --transport streamable-http --host 127.0.0.1 --port 8765 --path /mcp
unisessions-mcp --transport http --host 127.0.0.1 --port 8765
unisessions-mcp --transport sse --host 127.0.0.1 --port 8765
```

Environment knobs: `UNISESSIONS_MCP_TRANSPORT`, `UNISESSIONS_MCP_HOST`,
`UNISESSIONS_MCP_PORT`, `UNISESSIONS_MCP_PATH`, `UNISESSIONS_MCP_LOG_LEVEL`,
`UNISESSIONS_MCP_SHOW_BANNER`, `UNISESSIONS_SEARCH_INDEX`.

### MCP tools

| Tool | Description |
|---|---|
| `list_chats` | List sessions globally or filtered by provider and project path |
| `index_status` | Report indexed, missing, stale, deleted, and refresh-size counts |
| `refresh_chats_index` | Parse sessions into SQLite FTS5 index for fast recall |
| `search_chats` | Full-text search with literal, regex, all-keywords, and any-keywords modes |
| `search_sessions` | Find which sessions match a topic, with match counts and top snippets |

`search_chats` returns a structured response with `search_metadata` (total_matches, deduplicated, sessions_searched, messages_searched, truncated) and a `results` array ranked by relevance score. Duplicate messages across compaction cycles are collapsed to a single hit with a `duplicate_count` field.

`search_chats` supports provider (`codex`, `pi`, `opencode`, `claude`, `devin`, `factory`, `windsurf`), cwd,
session_id, role (`user`, `assistant`), message type (`message`, `compaction`,
`contextual`), `exclude_keywords` to filter out false positives, `max_per_session`
(default 5) to prevent one session from flooding results, date range (`after`,
`before`), and stale-index policy (`refresh`, `skip`, `error`).

Search runs over parsed `TextMessage` rows, not raw JSON files, so semantic
filters like `roles=["user"]` stay correct. Tool calls and tool outputs are
excluded to keep recall focused on chat text. An internal raw-match cap of 200
prevents timeouts on massive sessions.

**Performance**: warm indexed search ~35-40ms on a 20-session corpus. Cold
index refresh ~39-49s (I/O-bound). Call `index_status` first, then
`refresh_chats_index`, then use `search_chats` with `stale_policy="skip"` for
fast interactive recall.

## SDK usage

Build your own application on top of `session_sdk` without the CLI:

```python
from session_sdk import (
    CodexStore, PiStore, PiDcpStore, FactoryStore, WindsurfStore,
    CodexToPiConverter, SessionIdFactory, WindowsDefaults,
)

defaults = WindowsDefaults()
codex = CodexStore(defaults.codex_home)
pi = PiStore(defaults.pi_agent_home)
dcp = PiDcpStore(defaults.pi_dcp_home)

converter = CodexToPiConverter(codex, pi, dcp, SessionIdFactory())
plan = converter.plan("your-session-id-here")

print(f"Source:      {plan.source.path}")
print(f"Destination: {plan.destination}")
print(f"Records:     {len(plan.records)}")

converter.write(plan, overwrite=False)
```

### Trace export API

```python
from session_sdk import (
    CodexStore, WindowsDefaults,
    MessageExtractor, build_trace,
)
from session_sdk.jsonl import _dumps

defaults = WindowsDefaults()
store = CodexStore(defaults.codex_home)
session = store.load("session-id")
messages = MessageExtractor().from_codex(session)

# Build HuggingFace STS-format trace
records = build_trace("sts", session, messages)

# Write to JSONL
with open("trace.jsonl", "wb") as f:
    for record in records:
        f.write(_dumps(record))
        f.write(b"\n")
```

### Search API

```python
from session_sdk import (
    CodexStore, PiStore, OpenCodeStore, ClaudeStore, DevinStore, FactoryStore, WindsurfStore,
    SessionSearchEngine, WindowsDefaults,
)

defaults = WindowsDefaults()
engine = SessionSearchEngine(
    CodexStore(defaults.codex_home),
    PiStore(defaults.pi_agent_home),
    OpenCodeStore(defaults.opencode_data_home),
    claude=ClaudeStore(defaults.claude_home),
    devin=DevinStore(defaults.devin_home),
    factory=FactoryStore(defaults.factory_home),
    windsurf=WindsurfStore(defaults.windsurf_home),
)

engine.refresh_index(provider="claude")

response = engine.search(
    query="authentication",
    provider="claude",
    roles=["assistant"],
    stale_policy="skip",
)
for hit in response["results"]:
    print(f"{hit['session_id']} [{hit['role']}] {hit['snippet'][:80]}")

# SDK escape hatch for apps that want custom ranking or filtering.
raw_rows = engine.raw_search_rows(query="authentication", provider="claude")

engine.close()
```

## Architecture

```
session-export/
  pyproject.toml              # both packages + optional [mcp] extra
  session_sdk/                # the SDK core (no CLI dependencies)
    __init__.py               # public API surface
    json_types.py             # JSON type guards and coercion
    jsonl.py                  # JSONL read/write helpers (orjson when available)
    models.py                 # SessionSummary, TextMessage, NativeSession, ConversionPlan
    paths.py                  # WindowsDefaults, path encoding, SessionIdFactory, timestamps
    stores.py                 # CodexStore, PiStore, PiDcpStore, OpenCodeStore, ClaudeStore, DevinStore, FactoryStore, WindsurfStore
    converters.py             # Extractors, builders, 42 converters
    traces.py                 # STS, OpenAI, ShareGPT trace format builders
    search.py                 # parsed SQLite FTS5 chat recall index/search
  unisessions/                # CLI and MCP app (depends on session_sdk)
    __init__.py
    __main__.py               # python -m unisessions
    cli.py                    # argparse, command routing, dry-run/write, conflict resolution, trace export
    mcp_server.py             # FastMCP tools for chat recall/search
  tests/
    test_conversion.py        # 32 tests
  docs/                       # full API documentation
  examples/                   # 7 tested Python example scripts
  requirements.txt            # tiktoken + cryptography (required), orjson + google-re2 (optional)
  requirements-mcp.txt        # fastmcp for MCP server
```

### Dependency chain (no cycles)

```
json_types  (leaf)
jsonl       -> json_types
models      -> json_types
paths       (leaf)
stores      -> models, jsonl, json_types, paths
converters  -> stores, models, paths, json_types
search      -> stores, converters, models
```

The SDK never imports from the CLI. `unisessions` depends on `session_sdk`,
never the reverse.

## Default session paths

| Agent | Default Location |
|---|---|
| Codex sessions | `~/.codex/sessions` |
| Codex archived | `~/.codex/archived_sessions` |
| Pi sessions | `~/.pi/agent/sessions` |
| Pi DCP sidecars | `~/.pi-dcp/sessions` |
| OpenCode data | `%APPDATA%\opencode` or `OPENCODE_GLOBAL_DATA_DIR` |
| Claude Code | `~/.claude` or `CLAUDE_CONFIG_DIR` |
| Devin | `%APPDATA%/devin` or `DEVIN_CONFIG_DIR` |
| Factory | `~/.factory` or `FACTORY_CONFIG_DIR` |
| Windsurf Cascade | `~/.codeium/windsurf/cascade/` or `WINDSURF_CONFIG_DIR` |

## Data fidelity

UniSessions performs text-history conversions, not full behavioral state
replay. It preserves user/assistant/system text and enough metadata for the
target tool to open the session. It does not fully preserve every tool call,
provider-specific event, approval state, sandbox state, MCP runtime, or
UI-only event.

### How compaction is handled

Compaction markers are preserved across all seven formats so the target tool
can reconstruct context correctly:

- **Pi**: `type="compaction"` entry with `summary`, `firstKeptEntryId`,
  `tokensBefore`, `details`, and `fromHook=True`
- **Codex**: `type="compacted"` record with `payload.message` summary text
- **OpenCode**: user message with `CompactionPart` + assistant `summary=True`
- **Claude Code**: `system` entry with `subtype="compact_boundary"` +
  `logicalParentUuid`, followed by user message with `isCompactSummary=true`
- **Devin**: compaction summaries become system steps in ATIF transcript JSON
- **Windsurf Cascade**: compaction checkpoints (field 30) become compaction summary messages

### How contextual messages are handled

Codex injects contextual messages (permissions, AGENTS.md instructions,
environment context, skills, plugins) as `developer` or `user` role messages.
These are marked `is_contextual=True` by the SDK extractor and skipped by all
builders during export to prevent payload overflow in target tools. SDK
consumers can still inspect these messages.

## Performance

- `orjson` for JSON parsing when available (2x faster than stdlib)
- `google-re2` for regex search when available (4x faster than stdlib `re`)
- Trace export to HuggingFace STS, OpenAI fine-tuning, and ShareGPT formats
- O(1) session lookup via cached path index and ID index
- `has_changes()` reads head meta + line count (no full JSON parse)
- Bulk export reuses converter instances across sessions
- Default bulk workers: 8 (higher is opt-in; 32 workers measured ~2x slower)
- Warm indexed search: ~35-40ms on a 20-session corpus
- Cold index refresh: ~39-49s (I/O-bound, threading helps modestly)

## FAQ

**Can I convert Claude Code sessions to Pi?**

Yes. Use `python -m unisessions claude-to-pi <session-id> --write`. All 42
conversion directions are supported between Codex, Pi, OpenCode, Claude Code,
Devin, Factory, and Windsurf Cascade.

**Can I export all my Codex sessions at once?**

Yes. Use `python -m unisessions codex-to-pi-all --write --workers 8` to bulk
export all Codex sessions to Pi in parallel. You can also export to multiple
targets at once with `export-all --write --targets pi opencode claude devin factory windsurf`.

**Can my AI agent search my old chat history?**

Yes. The MCP server exposes `search_chats` and `search_sessions` tools that do full-text search
over parsed chat messages from all providers. Your agent can recall what you
discussed in any session across any project. Search runs on a SQLite FTS5
index so warm queries are ~35ms. Results are ranked by relevance, deduplicated
across compaction cycles, and capped per session to prevent timeouts.

**What happens if the destination session already exists?**

Use `--on-conflict` to control behavior: `skip` (default), `overwrite`,
`fork` (new UUID, preserves old file), or `update` (skip if unchanged,
overwrite if source changed).

**Does this preserve tool calls and tool outputs?**

No. This tool performs text-history conversions preserving user and assistant
chat messages, compaction summaries, and session metadata. Tool calls and
tool outputs are not preserved in the current version.

**Can I use the SDK without the CLI?**

Yes. The SDK (`session_sdk`) is a standalone library with no CLI dependencies.
Import stores, converters, and the search engine directly in your own Python
projects. The CLI and MCP server are thin wrappers built on top.

**What session formats are supported?**

Codex JSONL rollout files, Pi JSONL append-only tree entries, OpenCode
export/import JSON, Claude Code JSONL transcript files, Devin ATIF
transcript JSON with SQLite metadata, Factory JSONL transcript files, and
Windsurf Cascade AES-256-GCM encrypted protobuf trajectory files.
All seven formats are supported in all 42 conversion directions.

**How fast is bulk export?**

A full export of 276 Codex sessions to Pi completes in about 65 seconds with
8 workers. The largest sessions (700MB+) take a few seconds each. Default
workers is 8 because higher counts measured slower on I/O-bound workloads.

## Contributing

1. Fork the repo
2. Create a branch: `git checkout -b feature/your-feature`
3. Run tests: `python -m unittest discover -s tests -v`
4. Submit a PR with a description of your change

## Development

```powershell
python -m compileall session_sdk unisessions tests
python -m unittest discover -s tests -v
```

32 tests covering conversion shape, compaction extraction/emission, dry-run
safety, assistant usage estimation, OpenCode JSON shape, custom session
directories, path encoding, Claude extraction and conversion, Devin extraction
and conversion, Windsurf Cascade extraction and conversion, trace format
building (STS, OpenAI, ShareGPT), search behavior, search index persistence,
regex full-index search, keyword contraction handling, and dedup edge cases.

## Links

- X/Twitter: [@ImVibhek](https://x.com/ImVibhek)
- Website: [vibheksoni.com](https://vibheksoni.com/)
- Security Blog: [opendoors.wtf](https://opendoors.wtf/)

## License

MIT
