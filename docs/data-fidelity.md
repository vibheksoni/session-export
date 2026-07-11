# Data Fidelity

UniSessions performs **text-history conversions**, not full behavioral state replay. This page documents exactly what is preserved and what is not when converting sessions between tools.

## What Is Preserved

### User and Assistant Text Messages

All user and assistant text messages are extracted from the source format and written to the target format. Message order, roles, and timestamps are preserved.

### Compaction Summaries

Compaction markers are preserved across all four formats. Each format's builder emits the correct compaction entry type so the target tool can reconstruct context using its native compaction logic.

| Target | Compaction Representation |
|---|---|
| Pi | `type="compaction"` entry with `summary`, `firstKeptEntryId`, `tokensBefore`, `fromHook=True` |
| Codex | `type="compacted"` record with `payload.message` |
| OpenCode | User message with `CompactionPart` + assistant message with `summary=True` |
| Claude | `type="system"` record with `subtype="compact_boundary"` |

### Session Metadata

| Field | Preserved | Notes |
|---|---|---|
| Session ID | Yes | Preserved by default. Use `--new-id` or `--on-conflict fork` to generate a new one. |
| Timestamp | Yes | ISO 8601 format. Used in destination filenames. |
| CWD (project path) | Yes | Encoded per target format's directory naming convention. |
| Provider | Partially | Source provider is not stored in target metadata. Target builder sets its own provider string. |

### Model and Provider Info

When available in the source format, model and provider metadata is carried through to the target:

- **Pi**: `model_change` entries are tracked; provider/model carried to assistant messages.
- **OpenCode**: `modelID` and `providerID` from message info.
- **Codex**: Model provider in `session_meta` payload.
- **Claude**: `model` field in assistant message content.

### Token Estimation for Pi

Pi requires assistant messages to include a `usage` object with input/output token counts. Without it, Pi crashes in the interactive footer (`TypeError: Cannot read properties of undefined (reading 'input')`).

The `TokenEstimator` uses:

1. `tiktoken.encoding_for_model("gpt-4o")` (preferred)
2. `tiktoken.get_encoding("cl100k_base")` (fallback)
3. `ceil(chars / 4)` (last resort)

Token estimation is per-turn, not cumulative. The builder accumulates input tokens since the previous assistant message, sets assistant output tokens from assistant text, then resets pending input. Cumulative estimates produced impossible footer totals (tens of billions of tokens) and confused compaction logic.

## What Is Not Preserved

### Tool Calls and Tool Outputs

Tool calls (shell commands, function calls, web searches, image generation, custom tool calls) and their outputs are not extracted or written to the target format. The SDK's text-history extractors focus on user/assistant chat text only.

### Approval State

Whether a tool call was approved, denied, or auto-approved is not preserved. The target session starts with no approval history.

### Sandbox State

Sandbox configuration, file system snapshots, and execution environment state are not carried over.

### MCP Runtime Events

MCP server interactions, tool registrations, and runtime events are not preserved.

### UI-Only Events

Events that exist only in the source tool's UI layer (e.g. Pi's `thinking_level_change`, Codex's `turn_context`, display-only metadata) are not converted to the target format.

### Reasoning / Thinking Content

Assistant reasoning or thinking blocks (where supported by the source format) are not extracted as separate messages. Only the final text output is preserved.

## Contextual Messages

Codex injects contextual messages into sessions: developer permissions, AGENTS.md instructions, environment context, skills, plugins, and other system-level prompts. These are detected by `MessageExtractor._is_contextual()` via 19+ marker patterns and marked with `is_contextual=True`.

**During export**: Contextual messages are skipped by all builders. They are not written to the target format because they would cause payload overflow or silent drops in tools that do not expect them.

**In the SDK**: SDK consumers can still inspect contextual messages by calling `MessageExtractor.from_codex()` directly and checking `message.is_contextual`.

**In search**: Contextual messages are indexed with `message_type="contextual"` but excluded from search results by default. Use `include_contextual=True` in `search()` to include them.

## Role Mapping

Pi has no `system` role and silently drops `system` messages from LLM context. The extractor maps:

| Source Role | Mapped Role |
|---|---|
| `user` | `user` |
| `assistant` | `assistant` |
| `developer` | `user` |
| `system` | `user` |

This ensures developer and system messages are visible in Pi instead of being silently dropped.

## Codex Format Compatibility

The SDK handles both Codex rollout formats:

- **Old format** (pre-2026): flat JSON lines with `{"id":"<uuid>","timestamp":"<iso>"}` and `{"type":"message","role":"...","content":[...]}`. `CodexStore._normalize_records()` detects and converts these to the new wrapped format.
- **New format** (2026+): wrapped JSON lines with `{"type":"session_meta","payload":{...}}` and `{"type":"response_item","payload":{...}}`.

## Corrupted Data Handling

- **Corrupted JSONL lines** (e.g. backslash-prefixed `\{` from Codex bugs): skipped with a stderr warning. The rest of the file is processed normally.
- **Very large files** (700MB+): may trigger orjson memory limits on individual very large JSON lines. These may need streaming JSON parsing or file splitting before export.
- **Extended-length path prefixes** (`\\?\`): stripped by `encode_pi_cwd` before encoding to avoid invalid `?` characters in directory names.

## Future: Higher-Fidelity Mode

If exact tool/event replay is needed, implement a separate higher-fidelity mode with tests. The current text-history conversion is intentionally limited to useful chat context, not behavioral reproduction.

## See Also

- [Architecture](architecture.md) -- how the fidelity contract is implemented in code.
- [Converters](converters.md) -- the extractor and builder classes that enforce these rules.
