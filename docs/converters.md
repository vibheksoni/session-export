# Converters

Converters transform sessions between formats. Each converter pairs a source store with a target store, extracts messages via `MessageExtractor`, builds target-format records via a format-specific builder, and produces a `ConversionPlan`.

## Common Interface

Every converter implements three methods:

```python
def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan: ...
def has_changes(self, session_id: str) -> bool: ...
def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None: ...
```

### plan(session_id, *, target_id=None)

Loads the source session, extracts text messages, builds target-format records, and returns a `ConversionPlan`. The `target_id` parameter is keyword-only and enables forking with a new session ID while preserving the old destination file.

### has_changes(session_id)

Fast change detection without full JSON parse. Reads the source file's head metadata (via `_read_head_meta`) to get session ID, timestamp, and cwd, then counts source file lines (for JSONL) or messages array length (for OpenCode) and compares against the destination. Returns `True` if the destination does not exist or record counts differ.

### write(plan, overwrite=False)

Writes the plan's records to the destination path. For Pi-bound converters, also writes DCP sidecar files listed in `plan.services`.

## ConversionPlan

```python
@dataclass(frozen=True, slots=True)
class ConversionPlan:
    source: NativeSession       # the loaded source session
    destination: Path           # target file path
    records: list[JsonObject]   # target-format records to write
    services: tuple[Path, ...]  # DCP sidecar paths (Pi targets only)
```

## All 42 Converters

### CodexToPiConverter

```python
CodexToPiConverter(codex_store, pi_store, dcp_store, id_factory)
```

Converts Codex rollout JSONL to Pi session JSONL. Writes a DCP sidecar via `dcp_store`. Uses `PiRecordBuilder` which generates sequential hex entry IDs (`f"{n:08x}"`) to guarantee uniqueness in sessions with 100K+ entries.

### PiToCodexConverter

```python
PiToCodexConverter(pi_store, codex_store, id_factory)
```

Converts Pi session JSONL to Codex rollout JSONL. Uses `CodexRecordBuilder` with `model_provider="pi-import"`.

### CodexToOpenCodeConverter

```python
CodexToOpenCodeConverter(codex_store, opencode_store, id_factory)
```

Converts Codex rollout JSONL to OpenCode export JSON. Uses `OpenCodeExportBuilder` which generates OpenCode-style descending session IDs via `opencode_id("ses", timestamp)`.

### PiToOpenCodeConverter

```python
PiToOpenCodeConverter(pi_store, opencode_store, id_factory)
```

Converts Pi session JSONL to OpenCode export JSON.

### OpenCodeToCodexConverter

```python
OpenCodeToCodexConverter(opencode_store, codex_store, id_factory)
```

Converts OpenCode export JSON to Codex rollout JSONL. Uses `CodexRecordBuilder` with `model_provider="opencode-import"`.

### OpenCodeToPiConverter

```python
OpenCodeToPiConverter(opencode_store, pi_store, dcp_store, id_factory)
```

Converts OpenCode export JSON to Pi session JSONL. Writes a DCP sidecar.

### ClaudeToPiConverter

```python
ClaudeToPiConverter(claude_store, pi_store, dcp_store, id_factory)
```

Converts Claude Code session JSONL to Pi session JSONL. Writes a DCP sidecar.

### PiToClaudeConverter

```python
PiToClaudeConverter(pi_store, claude_store, id_factory)
```

Converts Pi session JSONL to Claude Code session JSONL. Uses `ClaudeRecordBuilder`.

### ClaudeToCodexConverter

```python
ClaudeToCodexConverter(claude_store, codex_store, id_factory)
```

Converts Claude Code session JSONL to Codex rollout JSONL. Uses `CodexRecordBuilder` with `model_provider="claude-import"`.

### CodexToClaudeConverter

```python
CodexToClaudeConverter(codex_store, claude_store, id_factory)
```

Converts Codex rollout JSONL to Claude Code session JSONL. Uses `ClaudeRecordBuilder`.

### ClaudeToOpenCodeConverter

```python
ClaudeToOpenCodeConverter(claude_store, opencode_store, id_factory)
```

Converts Claude Code session JSONL to OpenCode export JSON.

### OpenCodeToClaudeConverter

```python
OpenCodeToClaudeConverter(opencode_store, claude_store, id_factory)
```

Converts OpenCode export JSON to Claude Code session JSONL.

### WindsurfToPiConverter

```python
WindsurfToPiConverter(windsurf_store, pi_store, dcp_store, id_factory)
```

Converts Windsurf Cascade encrypted protobuf to Pi session JSONL. Decrypts the source `.pb` file, extracts text messages via `MessageExtractor.from_windsurf`, and writes a DCP sidecar.

### PiToWindsurfConverter

```python
PiToWindsurfConverter(pi_store, windsurf_store, id_factory)
```

Converts Pi session JSONL to Windsurf Cascade encrypted protobuf. Uses `WindsurfRecordBuilder` to build protobuf steps and encrypt with AES-256-GCM.

### WindsurfToCodexConverter

```python
WindsurfToCodexConverter(windsurf_store, codex_store, id_factory)
```

Converts Windsurf Cascade encrypted protobuf to Codex rollout JSONL. Uses `CodexRecordBuilder` with `model_provider="windsurf-import"`.

### CodexToWindsurfConverter

```python
CodexToWindsurfConverter(codex_store, windsurf_store, id_factory)
```

Converts Codex rollout JSONL to Windsurf Cascade encrypted protobuf. Uses `WindsurfRecordBuilder`.

### WindsurfToOpenCodeConverter

```python
WindsurfToOpenCodeConverter(windsurf_store, opencode_store, id_factory)
```

Converts Windsurf Cascade encrypted protobuf to OpenCode export JSON.

### OpenCodeToWindsurfConverter

```python
OpenCodeToWindsurfConverter(opencode_store, windsurf_store, id_factory)
```

Converts OpenCode export JSON to Windsurf Cascade encrypted protobuf. Uses `WindsurfRecordBuilder`.

### WindsurfToClaudeConverter

```python
WindsurfToClaudeConverter(windsurf_store, claude_store, id_factory)
```

Converts Windsurf Cascade encrypted protobuf to Claude Code session JSONL. Uses `ClaudeRecordBuilder`.

### ClaudeToWindsurfConverter

```python
ClaudeToWindsurfConverter(claude_store, windsurf_store, id_factory)
```

Converts Claude Code session JSONL to Windsurf Cascade encrypted protobuf. Uses `WindsurfRecordBuilder`.

### WindsurfToDevinConverter

```python
WindsurfToDevinConverter(windsurf_store, devin_store, id_factory)
```

Converts Windsurf Cascade encrypted protobuf to Devin ATIF transcript JSON.

### DevinToWindsurfConverter

```python
DevinToWindsurfConverter(devin_store, windsurf_store, id_factory)
```

Converts Devin ATIF transcript JSON to Windsurf Cascade encrypted protobuf. Uses `WindsurfRecordBuilder`.

### WindsurfToFactoryConverter

```python
WindsurfToFactoryConverter(windsurf_store, factory_store, id_factory)
```

Converts Windsurf Cascade encrypted protobuf to Factory JSONL transcript.

### FactoryToWindsurfConverter

```python
FactoryToWindsurfConverter(factory_store, windsurf_store, id_factory)
```

Converts Factory JSONL transcript to Windsurf Cascade encrypted protobuf. Uses `WindsurfRecordBuilder`.

## Conversion Matrix

| Source \ Target | Codex | Pi | OpenCode | Claude | Devin | Factory | Windsurf |
|---|---|---|---|---|---|---|---|
| **Codex** | -- | CodexToPi | CodexToOpenCode | CodexToClaude | CodexToDevin | CodexToFactory | CodexToWindsurf |
| **Pi** | PiToCodex | -- | PiToOpenCode | PiToClaude | PiToDevin | PiToFactory | PiToWindsurf |
| **OpenCode** | OpenCodeToCodex | OpenCodeToPi | -- | OpenCodeToClaude | OpenCodeToDevin | OpenCodeToFactory | OpenCodeToWindsurf |
| **Claude** | ClaudeToCodex | ClaudeToPi | ClaudeToOpenCode | -- | ClaudeToDevin | ClaudeToFactory | ClaudeToWindsurf |
| **Devin** | DevinToCodex | DevinToPi | DevinToOpenCode | DevinToClaude | -- | DevinToFactory | DevinToWindsurf |
| **Factory** | FactoryToCodex | FactoryToPi | FactoryToOpenCode | FactoryToClaude | FactoryToDevin | -- | FactoryToWindsurf |
| **Windsurf** | WindsurfToCodex | WindsurfToPi | WindsurfToOpenCode | WindsurfToClaude | WindsurfToDevin | WindsurfToFactory | -- |

## MessageExtractor

All converters share a single `MessageExtractor` instance that extracts `TextMessage` objects from any source format:

| Method | Source Format |
|---|---|
| `from_codex(session)` | Codex rollout JSONL (old flat + new wrapped) |
| `from_pi(session)` | Pi session JSONL |
| `from_opencode(session)` | OpenCode export JSON |
| `from_claude(session)` | Claude Code session JSONL |
| `from_windsurf(session)` | Windsurf Cascade encrypted protobuf |

### Role Mapping

Pi has no `system` role and silently drops `system` messages from LLM context. The extractor maps:

- `developer` -> `user`
- `system` -> `user`

### Contextual Message Detection

Codex injects contextual messages (developer permissions, AGENTS.md instructions, environment context, skills, plugins). The extractor detects these via `_CONTEXTUAL_MARKERS` (19+ known patterns) and marks them with `is_contextual=True`. Builders skip contextual messages during export to prevent payload overflow in target tools.

### Compaction Extraction

| Source | Detection |
|---|---|
| Codex | `compacted` records with `payload.message` as summary text. `replacement_history` items also extracted. |
| Pi | `compaction` entries with `summary` field. |
| OpenCode | Assistant messages with `summary=True`. `CompactionPart` entries filtered. |
| Claude | `system` records with `subtype="compact_boundary"` or messages with `isCompactSummary=True`. |
| Windsurf | Checkpoint steps (field 30) in `CortexTrajectory` protobuf. |

## Record Builders

### PiRecordBuilder

Builds Pi session JSONL with:
- Session header (`type: "session"`, `version: 3`)
- Append-only tree entries with sequential hex IDs (`f"{n:08x}"`)
- Compaction entries with `firstKeptEntryId`, `tokensBefore`, `fromHook=True`
- Assistant messages with `usage` (input, output, cacheRead, cacheWrite, reasoning, totalTokens, cost)
- Token estimation via `TokenEstimator` (tiktoken with char-based fallback)

### CodexRecordBuilder

Builds Codex rollout JSONL with:
- `session_meta` record (id, timestamp, cwd, originator, model_provider)
- `response_item` records with `payload.type="message"` and `content` array
- `compacted` records with `payload.message` as summary

### OpenCodeExportBuilder

Builds OpenCode export JSON with:
- `info` block (id, slug, projectID, directory, title, version, time)
- Messages with `info` and `parts` arrays
- Compaction as user message with `CompactionPart` + assistant message with `summary=True`
- OpenCode-style IDs via `opencode_id("msg"/"prt", timestamp)`

### ClaudeRecordBuilder

Builds Claude Code session JSONL with:
- `user` and `assistant` type records with `message`, `uuid`, `parentUuid`
- Compaction as `system` record with `subtype="compact_boundary"`
- UUID v4 for entry IDs

### WindsurfRecordBuilder

Builds Windsurf Cascade encrypted protobuf `.pb` files with:
- `CortexTrajectory` protobuf with repeated `CortexTrajectoryStep` messages
- User input steps (field 19) for user messages
- Planner response steps (field 20) for assistant messages
- Checkpoint steps (field 30) for compaction summaries
- AES-256-GCM encryption using the hardcoded Windsurf key
- Requires the `cryptography` package for encryption

## See Also

- [Models](models.md) -- `ConversionPlan`, `TextMessage`, `NativeSession`.
- [Stores](stores.md) -- source and target store classes.
- [CLI Reference](cli.md) -- how converters are invoked from the command line.
- [Trace Export](traces.md) -- trace format builders that consume the same TextMessage stream.
