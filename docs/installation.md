# Installation

## From PyPI (Recommended)

```bash
pip install unisessions
```

With optional extras:

```bash
pip install "unisessions[mcp]"        # MCP server (FastMCP)
pip install "unisessions[fast]"       # orjson + google-re2 for faster JSON and regex
pip install "unisessions[mcp,fast]"   # everything
```

This installs the `session_sdk` package, the `unisessions` CLI, and the
`unisessions-mcp` console script entry point.

## From Source

```bash
git clone https://github.com/vibheksoni/session-export.git
cd session-export
pip install -e .
```

## Prerequisites

- **Python 3.11 or later** (uses `match` statements, `type` aliases, and modern typing).
- pip or any PEP 517-compatible installer.
- Git for cloning (if installing from source).

## Dependencies

| Package | Required | Purpose |
|---|---|---|
| `tiktoken>=0.7.0` | Yes | Token estimation for Pi assistant message usage fields. |
| `cryptography>=42.0.0` | Yes | AES-256-GCM decryption for Windsurf Cascade sessions. |
| `orjson>=3.10.0` | No (auto-detected) | 2x faster JSON parsing. Falls back to stdlib `json` if absent. |
| `google-re2>=1.1` | No (auto-detected) | 4x faster regex search. Falls back to stdlib `re` if absent. |
| `fastmcp>=2.0.0` | No | MCP server. Install with `pip install "unisessions[mcp]"`. |

## Verify Installation

```bash
python -m unisessions list codex
python -m unisessions list pi
python -m unisessions list opencode
python -m unisessions list claude
python -m unisessions list devin
python -m unisessions list factory
python -m unisessions list windsurf
```

Each command should print a tab-separated list of sessions or "no sessions found."

## Optional: orjson for Speed

`orjson` is auto-detected at runtime. No configuration needed. When installed, all JSON parsing uses `orjson` (measured at ~244 MB/s vs ~120 MB/s for stdlib `json` on a 21 MB Codex rollout file). When absent, the SDK falls back to stdlib `json` transparently.

## Next Steps

- [Quick Start](quickstart.md) -- convert your first session.
- [CLI Reference](cli.md) -- all commands and flags.
