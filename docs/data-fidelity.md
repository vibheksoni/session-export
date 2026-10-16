# Data Fidelity

UniSessions performs **text-history conversions**, not full behavioral state replay. This page documents exactly what is preserved and what is not when converting sessions between tools.

## What Is Preserved

### User and Assistant Text Messages

All user and assistant text messages are extracted from the source format and written to the target format. Message order, roles, and timestamps are preserved.

### Compaction Summaries

Compaction markers are preserved across all ten formats. Each format's builder emits the correct compaction entry type so the target tool can reconstruct context using its native compaction logic.

| Target | Compaction Representation |
|---|---|
| Pi | `type="compaction"` entry with `summary`, `firstKeptEntryId`, `tokensBefore`, `fromHook=True` |
| Codex | `type="compacted"` record with `payload.message` |
| OpenCode | User message with `CompactionPart` + assistant message with `summary=True` |
| Claude | `type="system"` record with `subtype="compact_boundary"` |
| Windsurf | Checkpoint step (field 30) in `CortexTrajectory` protobuf |
| Grok | Compaction summaries import as `user_message_chunk` envelopes |
| Freebuff | Text part with `[compaction summary]` prefix |
| OMP | `type="compaction"` entry whose `firstKeptEntryId` is its own id |

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

Tool calls and their outputs are carried across formats where the target has a representation. Each tool call keeps its name, arguments, output, and error flag. The table at the end of this page lists what each format holds. Some source tools are not decoded yet (for example Windsurf's non-shell steps), so their calls are not converted. The search index still indexes chat text only.

### Approval State

Whether a tool call was approved, denied, or auto-approved is not preserved. The target session starts with no approval history.

### Sandbox State

Sandbox configuration, file system snapshots, and execution environment state are not carried over.

### MCP Runtime Events

MCP server interactions, tool registrations, and runtime events are not preserved.

### UI-Only Events

Events that exist only in the source tool's UI layer (e.g. Pi's `thinking_level_change`, Codex's `turn_context`, display-only metadata) are not converted to the target format.

### Reasoning / Thinking Content

Assistant reasoning is carried into the thinking field of each assistant message and written back as that format's thinking block. Codex keeps reasoning encrypted, so only the readable summary is available. T3 Code's legacy database has no field for reasoning or tool calls, so T3 writes keep text only.

### Format Support

| Format | Thinking (read / write) | Tool calls and outputs (read / write) |
|---|---|---|
| Pi | yes / yes (`thinking` block) | yes / yes (`toolCall` block, `toolResult` message) |
| Claude Code | yes / yes (`thinking` block, unsigned) | yes / yes (`tool_use`, `tool_result`) |
| Codex | summary only / summary only (`reasoning` item) | yes / yes (`function_call`, `function_call_output`) |
| OpenCode | yes / yes (`reasoning` part) | yes / yes (`tool` part with state) |
| Devin | yes / yes (`reasoning_content`) | yes / yes (`tool_calls`, `observation`) |
| Factory | yes / yes (`thinking` block, from the Droid schema) | yes / yes (`tool_use`, `tool_result`) |
| Windsurf | planner thinking / yes (field 3) | `run_command`, grep, view file, list directory, find files, and MCP calls, with outputs from the command-result steps / `run_command` only |
| Grok (grok-cli `grok.db`, and Grok Build folders) | yes / yes (`reasoning` part; read from Grok Build folders too) | yes / yes (`tool-call` and `tool-result` parts, read from Grok Build ACP `tool_call` updates) |
| Freebuff | yes / yes (`reasoning` part) | yes / yes (`tool` part) |
| T3 Code | no / no (text only) | no / no (text only) |

Claude Code removes thinking blocks before each request (checked in the Claude Code bundle), so the unsigned thinking blocks written for it are kept for display only. Behavior against the live API was not tested.

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
