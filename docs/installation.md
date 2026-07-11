# Installation

## Prerequisites

- **Python 3.11 or later** (uses `match` statements, `type` aliases, and modern typing).
- pip or any PEP 517-compatible installer.
- Git for cloning.

## Clone the Repository

```bash
git clone <repo-url> session-export
cd session-export
```

## Install Core Dependencies

```bash
pip install -r requirements.txt
```

This installs:

| Package | Required | Purpose |
|---|---|---|
| `tiktoken>=0.7.0` | Yes | Token estimation for Pi assistant message usage fields. |
| `orjson>=3.10.0` | No (auto-detected) | 2x faster JSON parsing. Falls back to stdlib `json` if absent. |
| `fastmcp>=2.0.0` | No | MCP server. Install separately if you need the server. |

## Install MCP Dependencies (Optional)

If you want to run the MCP server for agent chat recall:

```bash
pip install -r requirements-mcp.txt
```

Or use the optional extra from `pyproject.toml`:

```bash
pip install -e ".[mcp]"
```

## Editable Install (Recommended)

For development and CLI access:

```bash
pip install -e .
```

This registers the `unisessions-mcp` console script entry point.

## Verify Installation

```bash
python -m unisessions list codex
python -m unisessions list pi
python -m unisessions list opencode
python -m unisessions list claude
```

Each command should print a tab-separated list of sessions or "no sessions found."

## Optional: orjson for Speed

`orjson` is auto-detected at runtime. No configuration needed. When installed, all JSON parsing uses `orjson` (measured at ~244 MB/s vs ~120 MB/s for stdlib `json` on a 21 MB Codex rollout file). When absent, the SDK falls back to stdlib `json` transparently.

## Next Steps

- [Quick Start](quickstart.md) -- convert your first session.
- [CLI Reference](cli.md) -- all commands and flags.
