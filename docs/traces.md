# Trace Export

UniSessions can export any session from any supported provider into three
standard trace formats used for HuggingFace Hub upload, model fine-tuning,
and training data preparation.

## Supported Trace Formats

### 1. STS-Format (HuggingFace Session Trace Simple Format)

The official HuggingFace Hub trace viewer format. JSONL with a session
header line followed by message lines. Upload the resulting `.jsonl` to a
HuggingFace Dataset or Storage Bucket to open it in the trace viewer.

```jsonl
{"type":"session","harness":"codex","id":"abc123","cwd":"/project"}
{"type":"message","message":{"role":"user","content":"Fix the bug"}}
{"type":"message","message":{"role":"assistant","content":"Looking at it","timestamp":1719900000000,"model":"gpt-4o"}}
```

Spec: <https://huggingface.co/docs/hub/session-traces-format>

### 2. OpenAI Fine-tuning Format

Standard `{"messages": [{"role": ..., "content": ...}]}` JSONL, one
conversation per line. Compatible with OpenAI, Azure OpenAI, and
HuggingFace TRL fine-tuning pipelines.

```jsonl
{"messages":[{"role":"user","content":"Fix the bug"},{"role":"assistant","content":"Looking at it"}]}
```

Spec: <https://platform.openai.com/docs/guides/fine-tuning>

### 3. ShareGPT Format

Community standard `{"conversations": [{"from": ..., "value": ...}]}` JSONL.
Used by LLaMA-Factory, Axolotl, torchtune, and many open-source training
frameworks. Role mapping: `user` -> `human`, `assistant` -> `gpt`,
`system` -> `system`, `tool` -> `tool`.

```jsonl
{"conversations":[{"from":"human","value":"Fix the bug"},{"from":"gpt","value":"Looking at it"}]}
```

## CLI Usage

### Single session to trace

```powershell
# STS format (default) -- prints to stdout
python -m unisessions to-trace codex <session-id> --format sts

# OpenAI fine-tuning format -- write to file
python -m unisessions to-trace pi <session-id> --format openai --write -o output.jsonl

# ShareGPT format
python -m unisessions to-trace devin <session-id> --format sharegpt --write -o traces.jsonl
```

Arguments:

| Argument | Description |
|---|---|
| `provider` | Source provider: `codex`, `pi`, `opencode`, `claude`, `devin`, `factory`, `windsurf` |
| `session_id` | Session ID to export |
| `--format` | Trace format: `sts` (default), `openai`, `sharegpt` |
| `--output`, `-o` | Output file path (required with `--write`) |
| `--write` | Write to file instead of stdout |

### All providers supported

The `to-trace` command works with all 7 providers:

```powershell
python -m unisessions to-trace codex <session-id> --format sts
python -m unisessions to-trace pi <session-id> --format openai
python -m unisessions to-trace opencode <session-id> --format sharegpt
python -m unisessions to-trace claude <session-id> --format sts
python -m unisessions to-trace devin <session-id> --format openai
python -m unisessions to-trace factory <session-id> --format sharegpt
python -m unisessions to-trace windsurf <session-id> --format sts
```

## SDK Usage

```python
from session_sdk import (
    CodexStore, WindowsDefaults,
    MessageExtractor, build_trace,
)

defaults = WindowsDefaults()
store = CodexStore(defaults.codex_home)
session = store.load("session-id")
messages = MessageExtractor().from_codex(session)

# Build STS-format trace records
records = build_trace("sts", session, messages)

# Build OpenAI fine-tuning format
records = build_trace("openai", session, messages)

# Build ShareGPT format
records = build_trace("sharegpt", session, messages)

# Write to JSONL file
from session_sdk.jsonl import _dumps
with open("trace.jsonl", "wb") as f:
    for record in records:
        f.write(_dumps(record))
        f.write(b"\n")
```

### Direct builder usage

```python
from session_sdk import STSTraceBuilder, OpenAITraceBuilder, ShareGPTTraceBuilder

sts_records = STSTraceBuilder().build(session, messages)
openai_records = OpenAITraceBuilder().build(session, messages)
sharegpt_records = ShareGPTTraceBuilder().build(session, messages)
```

## How It Works

1. The source store loads the session in its native format.
2. `MessageExtractor` extracts `TextMessage` objects, skipping contextual
   messages (system prompts, environment info) and preserving compaction
   markers.
3. The trace builder transforms `TextMessage` objects into the target
   trace format's JSON structure.

Contextual messages are always skipped in trace output — they contain
system prompts, environment configuration, and injected instructions that
should not be included in training data or public traces.

Compaction summaries are preserved as `system` role messages with a
`[compaction summary]` prefix, making them visible in the trace viewer
and trainable as context boundaries.

## HuggingFace Hub Upload

After generating STS-format traces, upload them to HuggingFace Hub:

```powershell
# Install huggingface-cli
pip install huggingface-cli

# Upload to a dataset
hf upload your-username/your-dataset trace.jsonl

# Or use buckets for continuous sync
hf buckets sync ~/.codex/sessions --repo your-username/your-bucket
```

The Hub auto-detects the trace format and renders it in the trace viewer.
Raw Claude Code, Codex, and Pi session files are also natively supported
— you can upload them directly without conversion.
