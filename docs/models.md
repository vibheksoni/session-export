# Models

Data classes in `session_sdk/models.py` represent sessions, messages, and conversion plans. All are frozen dataclasses with `slots=True` for memory efficiency.

## SessionSummary

```python
@dataclass(frozen=True, slots=True)
class SessionSummary:
    provider: str          # "codex", "pi", "opencode", "claude", "devin", "factory", "windsurf"
    session_id: str        # unique session identifier
    cwd: str               # project working directory
    timestamp: str         # ISO 8601 timestamp
    path: Path             # filesystem path to the session file
    message_count: int     # number of messages (-1 if metadata-only)
```

Returned by `store.list()` and `store.list_metadata()`. When `message_count` is -1, the summary was produced by a metadata-only scan that did not count messages.

## TextMessage

```python
@dataclass(frozen=True, slots=True)
class TextMessage:
    role: str                          # "user" or "assistant"
    text: str                          # message text content
    timestamp: str                     # ISO 8601 timestamp
    model: str | None = None           # model name (e.g. "gpt-4o")
    provider: str | None = None        # provider name (e.g. "openai")
    api: str | None = None             # API type (e.g. "openai-completions")
    is_compaction: bool = False        # True if this is a compaction summary
    is_contextual: bool = False        # True if this is an injected/contextual message
```

The internal representation of a single chat message, extracted from any source format by `MessageExtractor`. Builders use the `is_compaction` and `is_contextual` flags to control emission:

- `is_compaction=True`: emitted as a compaction marker in the target format.
- `is_contextual=True`: skipped by all builders during export. SDK consumers can still inspect these messages.

## NativeSession

```python
@dataclass(frozen=True, slots=True)
class NativeSession:
    provider: str              # "codex", "pi", "opencode", "claude", "devin", "factory", "windsurf"
    session_id: str            # unique session identifier
    cwd: str                   # project working directory
    timestamp: str             # ISO 8601 timestamp
    path: Path                 # filesystem path to the session file
    records: list[JsonObject]  # raw JSON records from the file

    def summary(self, message_count: int) -> SessionSummary: ...
```

The loaded representation of a session file. `records` contains the raw parsed JSON objects from the file (JSONL lines for Codex/Pi/Claude, or a single JSON object wrapped in a list for OpenCode).

The `summary()` method converts the session to a `SessionSummary` with a provided message count.

## ConversionPlan

```python
@dataclass(frozen=True, slots=True)
class ConversionPlan:
    source: NativeSession           # the loaded source session
    destination: Path               # target file path
    records: list[JsonObject]       # target-format records to write
    services: tuple[Path, ...] = () # DCP sidecar paths (Pi targets only)
```

The output of `converter.plan()`. Contains everything needed to write the converted session:

- `source`: the original `NativeSession` for reference.
- `destination`: where `converter.write()` will write the file.
- `records`: the target-format JSON records (JSONL lines or a single JSON object).
- `services`: additional files to write (Pi DCP sidecars). Empty tuple for non-Pi targets.

## Usage Example

```python
from session_sdk.stores import CodexStore, PiStore, PiDcpStore
from session_sdk.converters import CodexToPiConverter
from session_sdk.paths import WindowsDefaults, SessionIdFactory

defaults = WindowsDefaults()
codex = CodexStore(defaults.codex_home)
pi = PiStore(defaults.pi_agent_home)
dcp = PiDcpStore(defaults.pi_dcp_home)
factory = SessionIdFactory(preserve_ids=True)

converter = CodexToPiConverter(codex, pi, dcp, factory)

# Plan the conversion
plan = converter.plan("01234567-89ab-cdef-0123-456789abcdef")
print(f"Source: {plan.source.path}")
print(f"Destination: {plan.destination}")
print(f"Records: {len(plan.records)}")
print(f"Services: {plan.services}")

# Check for changes
if converter.has_changes("01234567-89ab-cdef-0123-456789abcdef"):
    converter.write(plan, overwrite=True)

# Fork with a new session ID
import uuid
forked_plan = converter.plan("01234567-89ab-cdef-0123-456789abcdef", target_id=str(uuid.uuid4()))
converter.write(forked_plan, overwrite=True)
```

## See Also

- [Converters](converters.md) -- how models are used in conversion workflows.
- [Stores](stores.md) -- how `NativeSession` and `SessionSummary` are produced.
