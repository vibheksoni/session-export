"""Command Code session files, in every version the CLI reads.

Three layouts exist on disk. Command Code's CLI reads all three and rewrites an old file as version 3 the
first time it opens it (``migrateToV3`` in its ``cli.mjs``). This module does the same conversion in memory,
so the source file is never changed.

- Version 3 (current): a ``session`` header line, then entries linked by ``parentId``. Message entries are
  ``{"type": "message", "message": {"role", "content"}}`` with Anthropic-style content blocks.
- Version 2: one flat line per message, ``{"id", "timestamp", "role", "content", "metadata": {"version": 2}}``,
  with AI SDK style content parts (``reasoning``, ``tool-call``, ``tool-result``).
- Version 1: plain Anthropic-style ``{"role", "content"}`` lines with no ids and no metadata.

A file newer than version 3 is refused, as the CLI refuses it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from session_sdk.json_types import JsonObject, JsonValue, as_list, as_object, as_str, string_value

CURRENT_VERSION = 3

ENTRY_TYPES = frozenset(
    {"message", "model_change", "effort_change", "compaction", "branch_summary", "custom", "custom_message", "label", "session_info"}
)


class CommandCodeFormatError(ValueError):
    """A session file this build cannot read, such as one written by a newer Command Code."""


@dataclass(frozen=True)
class MigratedSession:
    """The session as version 3 records: the header first, then entries. ``corrupted`` counts lines skipped."""

    records: list[JsonObject]
    corrupted: int


@dataclass(frozen=True)
class _Paired:
    message: JsonObject
    timestamp: str
    metadata: JsonObject | None


def parse_record(line: str) -> JsonObject | None:
    """One JSON object from a line, or ``None`` for anything else."""
    try:
        value = json.loads(line)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def is_header(record: JsonObject) -> bool:
    return (
        record.get("type") == "session"
        and record.get("version") == CURRENT_VERSION
        and isinstance(record.get("id"), str)
        and isinstance(record.get("timestamp"), str)
        and isinstance(record.get("cwd"), str)
    )


def is_entry(record: JsonObject) -> bool:
    parent = record.get("parentId")
    return (
        record.get("type") in ENTRY_TYPES
        and isinstance(record.get("id"), str)
        and (parent is None or isinstance(parent, str))
        and isinstance(record.get("timestamp"), str)
    )


def detect_version(lines: list[str]) -> int:
    """The layout of a file, read from its first parseable line. Raises for a version this build cannot read."""
    for line in lines:
        record = parse_record(line)
        if record is None:
            continue
        if is_header(record):
            return 3
        version = record.get("version")
        if record.get("type") == "session" and isinstance(version, (int, float)) and version > CURRENT_VERSION:
            raise CommandCodeFormatError(
                f"Session file uses format v{int(version)}, but this build reads up to v{CURRENT_VERSION}. "
                "Update Command Code or this package to open it."
            )
        metadata = as_object(record.get("metadata"))
        return 2 if metadata is not None and metadata.get("version") == 2 else 1
    return 3


def migrate(lines: list[str], *, session_id: str, cwd: str, fallback_timestamp: str, model: str | None = None) -> MigratedSession:
    """Version 3 records for any version of a session file. ``model`` names the model of the last reply when the
    file does not record it (Command Code keeps it in a sidecar meta file)."""
    nonblank = [line for line in lines if line.strip()]
    if detect_version(nonblank) == 3:
        return _read_v3(nonblank, session_id=session_id, cwd=cwd, fallback_timestamp=fallback_timestamp)
    return _migrate_legacy(nonblank, session_id=session_id, cwd=cwd, fallback_timestamp=fallback_timestamp, model=model)


def _read_v3(lines: list[str], *, session_id: str, cwd: str, fallback_timestamp: str) -> MigratedSession:
    header: JsonObject | None = None
    entries: list[JsonObject] = []
    corrupted = 0
    for line in lines:
        record = parse_record(line)
        if record is None:
            corrupted += 1
        elif header is None:
            if is_header(record):
                header = record
            else:
                corrupted += 1
        elif is_entry(record):
            entries.append(record)
        else:
            corrupted += 1
    if header is None:
        header = {"type": "session", "version": CURRENT_VERSION, "id": session_id, "timestamp": fallback_timestamp, "cwd": cwd}
    return MigratedSession([header, *entries], corrupted)


def _migrate_legacy(lines: list[str], *, session_id: str, cwd: str, fallback_timestamp: str, model: str | None) -> MigratedSession:
    parsed: list[JsonObject] = []
    corrupted = 0
    for line in lines:
        record = parse_record(line)
        if record is None:
            corrupted += 1
        else:
            parsed.append(record)
    paired, skipped = _pair_mixed(parsed)
    corrupted += skipped
    converted = _convert_paired(paired, fallback_timestamp)

    records: list[JsonObject] = [{"type": "session", "version": CURRENT_VERSION, "id": session_id, "timestamp": fallback_timestamp, "cwd": cwd}]
    parent: str | None = None
    last_assistant = max((index for index, (message, _) in enumerate(converted) if message.get("role") == "assistant"), default=-1)
    for index, (message, timestamp) in enumerate(converted):
        entry_id = f"{index + 1:08x}"
        entry: JsonObject = {"type": "message", "id": entry_id, "parentId": parent, "timestamp": timestamp or fallback_timestamp, "message": message}
        if model and index == last_assistant:
            entry["model"] = model
        records.append(entry)
        parent = entry_id
    return MigratedSession(records, corrupted)


def _is_legacy(record: JsonObject) -> bool:
    metadata = as_object(record.get("metadata"))
    return metadata is None or metadata.get("version") != 2


def _has_valid_v2_content(record: JsonObject) -> bool:
    content = record.get("content")
    if record.get("role") == "tool":
        return isinstance(content, list)
    return isinstance(content, (str, list))


def _pair_mixed(records: list[JsonObject]) -> tuple[list[_Paired], int]:
    paired: list[_Paired] = []
    corrupted = 0
    index = 0
    while index < len(records):
        record = records[index]
        if not _is_legacy(record):
            index += 1
            if not _has_valid_v2_content(record):
                corrupted += 1
                continue
            timestamp = record.get("timestamp")
            paired.append(
                _Paired({"role": record.get("role"), "content": record.get("content")}, timestamp if isinstance(timestamp, str) else "", as_object(record.get("metadata")))
            )
            continue
        run: list[JsonObject] = []
        while index < len(records) and _is_legacy(records[index]):
            run.append(records[index])
            index += 1
        paired.extend(_pair_legacy(run))
    return paired, corrupted


def _pair_legacy(run: list[JsonObject]) -> list[_Paired]:
    source = [record for record in run if record.get("role") in ("user", "assistant")]
    if not source:
        return []
    converted = _legacy_to_v2([{"role": record.get("role"), "content": record.get("content")} for record in source])
    paired: list[_Paired] = []
    cursor = 0
    for message in converted:
        origin = source[min(cursor, len(source) - 1)]
        if message.get("role") in ("tool", "user"):
            cursor += 1
        timestamp = origin.get("timestamp")
        paired.append(_Paired(message, timestamp if isinstance(timestamp, str) else "", as_object(origin.get("metadata"))))
    return paired


# Version 1 blocks -> version 2 parts. Mirrors legacyAnthropicToSession in the CLI.


def _legacy_to_v2(messages: list[JsonObject]) -> list[JsonObject]:
    names: dict[str, str] = {}
    for message in messages:
        blocks = as_list(message.get("content"))
        if message.get("role") != "assistant" or blocks is None:
            continue
        for block in blocks:
            item = as_object(block)
            item_id = item.get("id") if item is not None else None
            if item is not None and item.get("type") in ("tool_use", "server_tool_use") and isinstance(item_id, str):
                names[item_id] = string_value(item, "name") or ""
    converted: list[JsonObject] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            converted.append({"role": message.get("role"), "content": content})
            continue
        blocks = as_list(content)
        if blocks is None:
            continue
        if message.get("role") == "user":
            converted.extend(_user_blocks_to_v2(blocks, names))
        elif message.get("role") == "assistant":
            converted.extend(_assistant_blocks_to_v2(blocks, names))
    return converted


_SERVER_RESULT_TYPES = ("web_search_tool_result", "web_fetch_tool_result", "server_tool_result")
_SERVER_ERROR_TYPES = ("web_search_tool_result_error", "web_fetch_tool_result_error", "web_search_tool_error", "web_fetch_tool_error")


def _result_value(block: JsonObject) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    return json.dumps(content if content is not None else "", ensure_ascii=False)


def _assistant_blocks_to_v2(blocks: list[JsonValue], names: dict[str, str]) -> list[JsonObject]:
    parts: list[JsonValue] = []
    results: list[JsonValue] = []
    for block in blocks:
        item = as_object(block)
        if item is None:
            continue
        kind = item.get("type")
        if kind == "text" and item.get("text"):
            parts.append({"type": "text", "text": item.get("text")})
        elif kind == "thinking" and item.get("thinking"):
            parts.append({"type": "reasoning", "text": item.get("thinking")})
        elif kind == "tool_use":
            names[str(item.get("id"))] = string_value(item, "name") or ""
            parts.append({"type": "tool-call", "toolCallId": item.get("id"), "toolName": item.get("name"), "input": _input(item)})
        elif kind == "server_tool_use":
            parts.append({"type": "tool-call", "toolCallId": item.get("id"), "toolName": item.get("name"), "input": _input(item), "providerExecuted": True})
        elif kind in _SERVER_RESULT_TYPES:
            error_block = as_object(item.get("content"))
            is_error = error_block is not None and as_str(error_block.get("type")) in _SERVER_ERROR_TYPES
            results.append(
                {
                    "type": "tool-result",
                    "toolCallId": item.get("tool_use_id"),
                    "toolName": names.get(str(item.get("tool_use_id")), "unknown"),
                    "output": {"type": "error-text" if is_error else "text", "value": _result_value(item)},
                    "providerExecuted": True,
                }
            )
    converted: list[JsonObject] = []
    if parts:
        first = as_object(parts[0]) if len(parts) == 1 else None
        only_text = as_str(first.get("text")) if first is not None and first.get("type") == "text" else None
        converted.append({"role": "assistant", "content": only_text if only_text is not None else parts})
    if results:
        converted.append({"role": "tool", "content": results})
    return converted


def _user_blocks_to_v2(blocks: list[JsonValue], names: dict[str, str]) -> list[JsonObject]:
    parts: list[JsonValue] = []
    results: list[JsonValue] = []
    for block in blocks:
        item = as_object(block)
        if item is None:
            continue
        kind = item.get("type")
        if kind == "tool_result":
            value = _result_value(item)
            results.append(
                {
                    "type": "tool-result",
                    "toolCallId": item.get("tool_use_id"),
                    "toolName": names.get(str(item.get("tool_use_id")), "unknown"),
                    "output": {"type": "error-text" if item.get("is_error") else "text", "value": value},
                }
            )
        elif kind == "text":
            parts.append({"type": "text", "text": item.get("text")})
        elif kind == "image":
            source = as_object(item.get("source")) or {}
            if source.get("type") == "base64":
                image = f"data:{source.get('media_type')};base64,{source.get('data')}"
            else:
                image = as_str(source.get("data")) or ""
            parts.append({"type": "image", "image": image})
    converted: list[JsonObject] = []
    if results:
        converted.append({"role": "tool", "content": results})
    if parts:
        first = as_object(parts[0]) if len(parts) == 1 else None
        only_text = as_str(first.get("text")) if first is not None and first.get("type") == "text" else None
        converted.append({"role": "user", "content": only_text if only_text is not None else parts})
    return converted


def _input(item: JsonObject) -> JsonValue:
    value = item.get("input")
    return value if value is not None else {}


# Version 2 parts -> version 3 blocks. Mirrors fromV2 in the CLI.


def v2_to_agent(message: JsonObject) -> JsonObject:
    content = message.get("content")
    if message.get("role") == "assistant":
        if isinstance(content, str):
            return {"role": "assistant", "content": [{"type": "text", "text": content}]}
        blocks: list[JsonValue] = []
        for part in as_list(content) or []:
            item = as_object(part)
            if item is None:
                continue
            kind = item.get("type")
            if kind == "text":
                blocks.append({"type": "text", "text": item.get("text")})
            elif kind == "tool-call":
                blocks.append({"type": "tool_use", "id": item.get("toolCallId"), "name": item.get("toolName"), "input": _input(item)})
            elif kind == "reasoning":
                blocks.append({"type": "thinking", "thinking": item.get("text"), "signature": ""})
        return {"role": "assistant", "content": blocks}
    return {"role": "user", "content": _user_content_from_v2(content)}


def _user_content_from_v2(content: JsonValue) -> list[JsonValue]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    blocks: list[JsonValue] = []
    for part in as_list(content) or []:
        item = as_object(part)
        if item is None:
            continue
        kind = item.get("type")
        if kind == "text":
            blocks.append({"type": "text", "text": item.get("text")})
        elif kind == "image":
            image = as_str(item.get("image")) or ""
            if image.startswith("data:") and ";base64," in image:
                media_type, _, data = image[5:].partition(";base64,")
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}})
        elif kind == "tool-result":
            blocks.append({"type": "tool_result", "tool_use_id": item.get("toolCallId"), "content": [{"type": "text", "text": _output_text(item.get("output"))}]})
    return blocks


def _output_text(output: JsonValue) -> str:
    if isinstance(output, str):
        return output
    obj = as_object(output)
    if obj is not None and "value" in obj:
        value = obj["value"]
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return json.dumps(output, ensure_ascii=False)


def _metadata_meta(metadata: JsonObject | None) -> JsonObject | None:
    if metadata is None:
        return None
    meta: JsonObject = {}
    for key in ("messageId", "createdAt", "isMeta", "isAutomated", "isSummary", "source", "injected", "hookContexts"):
        value = metadata.get(key)
        if value is not None:
            meta[key] = value
    return meta or None


def _convert_paired(paired: list[_Paired], fallback: str) -> list[tuple[JsonObject, str]]:
    converted: list[tuple[JsonObject, str]] = []
    cursor = 0
    while cursor < len(paired):
        current = paired[cursor]
        timestamp = current.timestamp or fallback
        if current.message.get("role") != "tool":
            converted.append((_with_meta(v2_to_agent(current.message), current.metadata), timestamp))
            cursor += 1
            continue
        following = paired[cursor + 1] if cursor + 1 < len(paired) else None
        if following is not None and following.message.get("role") == "user":
            merged = [*v2_to_agent(current.message)["content"], *v2_to_agent(following.message)["content"]]  # type: ignore[misc]
            converted.append((_with_meta({"role": "user", "content": merged}, current.metadata), timestamp))
            cursor += 2
            continue
        converted.append((_with_meta(v2_to_agent(current.message), current.metadata), timestamp))
        cursor += 1
    return converted


def _with_meta(message: JsonObject, metadata: JsonObject | None) -> JsonObject:
    meta = _metadata_meta(metadata)
    return {**message, "meta": meta} if meta else message
