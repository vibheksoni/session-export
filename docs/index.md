---
hide:
  - navigation
  - toc
---

# UniSessions

Convert AI coding CLI sessions between **Codex**, **Claude Code**, **Pi**,
**OpenCode**, **Devin**, **Factory**, and **Windsurf Cascade**. SDK, CLI, MCP chat recall, and
trace export for fine-tuning.

[![PyPI](https://img.shields.io/pypi/v/unisessions?logo=pypi&logoColor=white)](https://pypi.org/project/unisessions/)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11+-blue?logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-yellow)]()
[![GitHub stars](https://img.shields.io/github/stars/vibheksoni/session-export?style=social)](https://github.com/vibheksoni/session-export/stargazers)

[:material-download: Install](installation.md){ .md-button .md-button--primary }
[:material-rocket-launch: Quick Start](quickstart.md){ .md-button }
[:material-github: GitHub](https://github.com/vibheksoni/session-export){ .md-button }

---

## Why I built this

I use a lot of AI coding CLIs — Codex, Claude Code, Pi, OpenCode, Devin,
Factory, Windsurf Cascade — and wanted to move a session from one tool into another without
losing the useful conversation history.

I looked for a tool that could convert one AI CLI session into another and
found nothing, so I built one.

I also wanted my agent to remember things from my other sessions. Like if I
solved a bug in one project, I wanted to tell it "hey in that other session I
fixed this by doing X" and it would go check and learn from it instead of me
explaining the same thing again.

So this does three things:

- **Move sessions** between Codex, Pi, OpenCode, Claude Code, Devin,
  Factory, and Windsurf Cascade in any direction — all 42 combinations
- **Export traces** in HuggingFace STS, OpenAI fine-tuning, or ShareGPT
  format for Hub upload or model training
- **Search chat history** across all sessions, providers, and projects so
  your agent can recall what you did before

The project is SDK-first so you can build GUIs or other tools on top. The CLI
and MCP server are just built on top of the SDK.

## Features

### 42 conversion directions

Convert any session to any other format. All seven providers are supported in
every direction.

| From \ To | Pi | Codex | OpenCode | Claude | Devin | Factory | Windsurf |
|---|---|---|---|---|---|---|---|
| **Codex** | ✓ | -- | ✓ | ✓ | ✓ | ✓ | ✓ |
| **Pi** | -- | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| **OpenCode** | ✓ | ✓ | -- | ✓ | ✓ | ✓ | ✓ |
| **Claude** | ✓ | ✓ | ✓ | -- | ✓ | ✓ | ✓ |
| **Devin** | ✓ | ✓ | ✓ | ✓ | -- | ✓ | ✓ |
| **Factory** | ✓ | ✓ | ✓ | ✓ | ✓ | -- | ✓ |
| **Windsurf** | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | -- |

### Trace export

Export sessions as training data in three formats:

| Format | Use case |
|---|---|
| `sts` | HuggingFace Hub trace viewer |
| `openai` | OpenAI / Azure fine-tuning JSONL |
| `sharegpt` | LLaMA-Factory, Axolotl, torchtune |

### MCP chat recall

A FastMCP server exposes a SQLite FTS5 full-text search index over all your
parsed session chat history. Your AI agent can search across every session
from every provider to recall past conversations.

### SDK-first

The `session_sdk` package is a standalone library with no CLI dependencies.
Import stores, converters, and the search engine directly in your own Python
projects.

## Quick install

```bash
pip install unisessions
```

With extras:

```bash
pip install "unisessions[mcp,fast]"
```

## One-liner conversion

```bash
python -m unisessions codex-to-pi <session-id> --write
```

## Stats

- **7 providers**: Codex, Pi, OpenCode, Claude Code, Devin, Factory, Windsurf Cascade
- **42 conversion directions**: every provider to every other provider
- **3 trace formats**: HuggingFace STS, OpenAI, ShareGPT
- **32 tests**: conversion shape, compaction, dry-run safety, search, traces
- **MIT licensed**: open source, do whatever

---

[:material-download: Get started](installation.md){ .md-button .md-button--primary }
[:material-book-open: Read the docs](quickstart.md){ .md-button }
