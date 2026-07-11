from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from session_sdk.json_types import JsonObject, as_list, as_object, as_str, sequence_to_text, string_value
from session_sdk.jsonl import _loads
from session_sdk.models import ConversionPlan, NativeSession, TextMessage
from session_sdk.paths import SessionIdFactory, iso_to_epoch_ms, opencode_id, opencode_slug
from session_sdk.stores import ClaudeStore, CodexStore, DevinStore, FactoryStore, OpenCodeStore, PiDcpStore, PiStore, WindsurfStore


def _count_jsonl_records(path: Path) -> int:
    count = 0
    with path.open("rb") as f:
        for line in f:
            if line.strip():
                count += 1
    return count


def _count_opencode_records(path: Path) -> int:
    export = _loads(path.read_bytes())
    if isinstance(export, dict):
        messages = export.get("messages")
        if isinstance(messages, list):
            return len(messages)
    return 0


class Encoding(Protocol):
    def encode(self, text: str) -> list[int]:
        raise NotImplementedError


class TokenEstimator:
    def __init__(self) -> None:
        self._encoding = self._load_encoding()

    def count(self, text: str) -> int:
        if not text:
            return 0
        if self._encoding is not None:
            return len(self._encoding.encode(text))
        return max(1, (len(text) + 3) // 4)

    @staticmethod
    def _load_encoding() -> Encoding | None:
        try:
            import tiktoken
        except ImportError:
            return None
        try:
            return tiktoken.encoding_for_model("gpt-4o")
        except KeyError:
            return tiktoken.get_encoding("cl100k_base")


class MessageExtractor:
    _ROLE_MAP = {"developer": "user", "system": "user"}

    _CONTEXTUAL_MARKERS: tuple[str, ...] = (
        "# AGENTS.md instructions for ",
        "<environment_context>",
        "<permissions instructions>",
        "<apps_instructions>",
        "<skills_instructions>",
        "<collaboration_mode>",
        "<personality_spec>",
        "<token_budget>",
        "<model_switch>",
        "<realtime_conversation>",
        "<user_shell_command>",
        "<turn_aborted>",
        "<subagent_notification>",
        "<codex_internal_context",
        "<goal_context>",
        "<external_",
        "<hook_prompt",
        "Warning: The maximum number of unified exec processes",
        "Warning: apply_patch was requested via",
        "Warning: Your account was flagged for potentially high-risk cyber activity",
        "Approved command prefix saved:",
        "Use prior reviews as context, not binding precedent",
        "Generated images are saved to",
        "Allowed network rule saved in execpolicy",
        "Denied network rule saved in execpolicy",
    )

    @classmethod
    def _is_contextual(cls, text: str) -> bool:
        head = text[:120]
        return any(m in head for m in cls._CONTEXTUAL_MARKERS)

    def _extract_codex_message(self, payload: JsonObject, timestamp: str) -> TextMessage | None:
        role = string_value(payload, "role") or ""
        if role not in {"user", "assistant", "system", "developer"}:
            return None
        text = self._content_text(payload.get("content"))
        if not text:
            return None
        mapped = self._ROLE_MAP.get(role, role)
        contextual = self._is_contextual(text)
        return TextMessage(mapped, text, timestamp, is_contextual=contextual)

    def from_codex(self, session: NativeSession) -> list[TextMessage]:
        messages: list[TextMessage] = []
        for record in session.records:
            rtype = record.get("type")
            if rtype == "compacted":
                payload = as_object(record.get("payload"))
                if payload is None:
                    continue
                summary_text = string_value(payload, "message") or ""
                timestamp = string_value(record, "timestamp") or session.timestamp
                if summary_text:
                    messages.append(TextMessage("user", summary_text, timestamp, is_compaction=True))
                rh = as_list(payload.get("replacement_history"))
                if rh:
                    for item in rh:
                        item_obj = as_object(item)
                        if item_obj is None or item_obj.get("type") != "message":
                            continue
                        msg = self._extract_codex_message(item_obj, timestamp)
                        if msg:
                            messages.append(msg)
                continue
            payload = as_object(record.get("payload"))
            if payload is None or payload.get("type") != "message":
                continue
            timestamp = string_value(record, "timestamp") or session.timestamp
            msg = self._extract_codex_message(payload, timestamp)
            if msg:
                messages.append(msg)
        return messages

    def from_pi(self, session: NativeSession) -> list[TextMessage]:
        messages: list[TextMessage] = []
        current_provider: str | None = None
        current_model: str | None = None
        for record in session.records:
            rtype = record.get("type")
            if rtype == "model_change":
                current_provider = string_value(record, "provider") or current_provider
                current_model = string_value(record, "modelId") or current_model
                continue
            if rtype == "compaction":
                summary = string_value(record, "summary") or ""
                timestamp = string_value(record, "timestamp") or session.timestamp
                if summary:
                    messages.append(TextMessage("user", summary, timestamp, is_compaction=True))
                continue
            if rtype != "message":
                continue
            message = as_object(record.get("message"))
            if message is None:
                continue
            role = string_value(message, "role")
            if role not in {"user", "assistant", "system"}:
                continue
            text = self._content_text(message.get("content"))
            if not text:
                continue
            timestamp = string_value(record, "timestamp") or string_value(message, "timestamp") or session.timestamp
            mapped_role = self._ROLE_MAP.get(role, role)
            messages.append(
                TextMessage(
                    mapped_role,
                    text,
                    timestamp,
                    model=string_value(message, "model") or current_model,
                    provider=string_value(message, "provider") or current_provider,
                    api=string_value(message, "api"),
                )
            )
        return messages

    def from_opencode(self, session: NativeSession) -> list[TextMessage]:
        if not session.records:
            return []
        export = session.records[0]
        messages = as_list(export.get("messages")) or []
        extracted: list[TextMessage] = []
        for item in messages:
            message = as_object(item)
            if message is None:
                continue
            info = as_object(message.get("info"))
            if info is None:
                continue
            role = string_value(info, "role")
            if role not in {"user", "assistant"}:
                continue
            parts = as_list(message.get("parts")) or []
            text = self._opencode_parts_text(parts)
            timestamp = self._opencode_timestamp(info) or session.timestamp
            if info.get("summary") is True and role == "assistant" and text:
                extracted.append(TextMessage("user", text, timestamp, is_compaction=True))
                continue
            if not text:
                continue
            extracted.append(
                TextMessage(
                    role,
                    text,
                    timestamp,
                    model=string_value(info, "modelID") or self._nested_model_value(info, "modelID"),
                    provider=string_value(info, "providerID") or self._nested_model_value(info, "providerID"),
                )
            )
        return extracted

    def from_claude(self, session: NativeSession) -> list[TextMessage]:
        messages: list[TextMessage] = []
        for record in session.records:
            rtype = record.get("type")
            if rtype == "system" and record.get("subtype") == "compact_boundary":
                summary = string_value(record, "content") or ""
                timestamp = string_value(record, "timestamp") or session.timestamp
                if summary:
                    messages.append(TextMessage("user", summary, timestamp, is_compaction=True))
                continue
            if rtype not in ("user", "assistant"):
                continue
            message = as_object(record.get("message"))
            if message is None:
                continue
            role = string_value(message, "role") or rtype
            if role not in ("user", "assistant"):
                continue
            text = self._content_text(message.get("content"))
            if not text:
                continue
            timestamp = string_value(record, "timestamp") or session.timestamp
            model = string_value(message, "model")
            if record.get("isCompactSummary") is True:
                messages.append(TextMessage("user", text, timestamp, is_compaction=True))
                continue
            messages.append(TextMessage(role, text, timestamp, model=model))
        return messages

    def from_devin(self, session: NativeSession) -> list[TextMessage]:
        if not session.records:
            return []
        transcript = session.records[0]
        steps = as_list(transcript.get("steps")) or []
        messages: list[TextMessage] = []
        agent = as_object(transcript.get("agent")) or {}
        model_name = string_value(agent, "model_name")
        for step in steps:
            step_obj = as_object(step)
            if step_obj is None:
                continue
            source = string_value(step_obj, "source") or ""
            timestamp = string_value(step_obj, "timestamp") or session.timestamp
            message = step_obj.get("message")
            if source == "system":
                text = self._devin_message_text(message)
                if not text:
                    continue
                contextual = self._is_contextual(text) or self._is_devin_contextual(text)
                messages.append(TextMessage("user", text, timestamp, is_contextual=contextual))
            elif source == "user":
                text = self._devin_message_text(message)
                if not text:
                    continue
                messages.append(TextMessage("user", text, timestamp))
            elif source == "agent":
                text = self._devin_message_text(message)
                if not text:
                    continue
                extra = as_object(step_obj.get("extra")) or {}
                gen_model = string_value(extra, "generation_model") or model_name
                messages.append(TextMessage("assistant", text, timestamp, model=gen_model, provider="devin"))
        return messages

    _DEVIN_CONTEXTUAL_MARKERS: tuple[str, ...] = (
        "You are Devin, an interactive command line agent",
        "Available subagent profiles",
        "You are powered by",
        "<system_info>",
        "<rules type=\"always-on\">",
        "<rules type='always-on'>",
    )

    @classmethod
    def _is_devin_contextual(cls, text: str) -> bool:
        head = text[:200]
        return any(m in head for m in cls._DEVIN_CONTEXTUAL_MARKERS)

    @staticmethod
    def _devin_message_text(message: object) -> str:
        if isinstance(message, str):
            return message
        obj = as_object(message)
        if obj is not None:
            text = string_value(obj, "content")
            if text:
                return text
            text = string_value(obj, "text")
            if text:
                return text
        return ""

    def from_factory(self, session: NativeSession) -> list[TextMessage]:
        messages: list[TextMessage] = []
        for record in session.records:
            rtype = record.get("type")
            if rtype != "message":
                continue
            msg = as_object(record.get("message"))
            if msg is None:
                continue
            role = string_value(msg, "role") or "user"
            timestamp = string_value(record, "timestamp") or session.timestamp
            content = msg.get("content")
            text = sequence_to_text(content) if isinstance(content, list) else (string_value(msg, "content") or "")
            if not text:
                continue
            if role == "user":
                contextual = self._is_contextual(text)
                messages.append(TextMessage("user", text, timestamp, is_contextual=contextual))
            elif role == "assistant":
                messages.append(TextMessage("assistant", text, timestamp, provider="factory"))
            elif role == "system":
                messages.append(TextMessage("user", text, timestamp, is_contextual=True))
        return messages

    def from_windsurf(self, session: NativeSession) -> list[TextMessage]:
        if not session.records:
            return []
        record = session.records[0]
        plaintext = record.get("_plaintext")
        if not isinstance(plaintext, (bytes, bytearray)):
            return []
        from session_sdk.windsurf_pb import (
            parse_trajectory,
            parse_step,
            parse_step_timestamp,
            iter_fields,
            read_string_field,
            parse_checkpoint,
            VARIANT_USER_INPUT,
            VARIANT_PLANNER_RESPONSE,
            VARIANT_CHECKPOINT,
            VARIANT_CONTEXT_INJECTION,
        )
        from session_sdk.paths import epoch_ms_to_iso
        traj = parse_trajectory(plaintext)
        steps = traj.get("steps") or []
        messages: list[TextMessage] = []
        for step_buf in steps:
            if not isinstance(step_buf, (bytes, bytearray)):
                continue
            step = parse_step(step_buf)
            vf = step["variant_field"]
            vdata = step["variant_data"]
            if vdata is None:
                continue
            ts_seconds = parse_step_timestamp(step_buf)
            timestamp = epoch_ms_to_iso(ts_seconds * 1000) if ts_seconds is not None else session.timestamp
            if vf == VARIANT_USER_INPUT:
                text = read_string_field(vdata, 2) or ""
                if text:
                    contextual = self._is_contextual(text) or self._is_windsurf_contextual(text)
                    messages.append(TextMessage("user", text, timestamp, is_contextual=contextual))
            elif vf == VARIANT_PLANNER_RESPONSE:
                visible = read_string_field(vdata, 1) or ""
                internal = read_string_field(vdata, 3) or ""
                if visible:
                    messages.append(TextMessage("assistant", visible, timestamp, provider="windsurf"))
                elif internal and not visible:
                    messages.append(TextMessage("assistant", internal, timestamp, provider="windsurf"))
            elif vf == VARIANT_CHECKPOINT:
                cp = parse_checkpoint(vdata)
                summary = cp.get("session_summary") or cp.get("user_intent") or ""
                if summary:
                    messages.append(TextMessage("user", summary, timestamp, is_compaction=True))
            elif vf == VARIANT_CONTEXT_INJECTION:
                text = read_string_field(vdata, 1) or ""
                if not text:
                    for sfno, swt, _off, sval in iter_fields(vdata):
                        if sfno == 5 and swt == 2 and isinstance(sval, (bytes, bytearray)):
                            inner = read_string_field(sval, 1)
                            if inner:
                                text = inner
                            break
                if text:
                    messages.append(TextMessage("user", text, timestamp, is_contextual=True))
        return messages

    _WINDSURF_CONTEXTUAL_MARKERS: tuple[str, ...] = (
        "You are a tool-calling assistant",
        "Available tools",
        "System prompt",
        "<system_info>",
        "## System Prompt",
        "Coding Guidelines",
        "MCP USAGE",
    )

    @classmethod
    def _is_windsurf_contextual(cls, text: str) -> bool:
        head = text[:200]
        return any(m in head for m in cls._WINDSURF_CONTEXTUAL_MARKERS)

    @staticmethod
    def _content_text(value: object) -> str:
        if isinstance(value, str):
            return value
        parts = as_list(value)
        if parts is not None:
            return sequence_to_text(parts)
        obj = as_object(value)
        if obj is not None:
            return as_str(obj.get("text")) or ""
        return ""

    @staticmethod
    def _opencode_parts_text(parts: list[object]) -> str:
        text_parts: list[str] = []
        for part in parts:
            obj = as_object(part)
            if obj is None or obj.get("type") != "text":
                continue
            text = as_str(obj.get("text"))
            if text:
                text_parts.append(text)
        return "\n".join(text_parts)

    @staticmethod
    def _opencode_timestamp(info: JsonObject) -> str:
        time = as_object(info.get("time")) or {}
        created = time.get("created")
        if isinstance(created, int):
            return datetime.fromtimestamp(created / 1000, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        return ""

    @staticmethod
    def _nested_model_value(info: JsonObject, key: str) -> str | None:
        model = as_object(info.get("model"))
        if model is None:
            return None
        return string_value(model, key)


class PiRecordBuilder:
    def build(self, session_id: str, cwd: str, timestamp: str, messages: list[TextMessage]) -> list[JsonObject]:
        records: list[JsonObject] = [
            {
                "type": "session",
                "version": 3,
                "id": session_id,
                "timestamp": timestamp,
                "cwd": cwd,
            }
        ]
        parent_id = None
        pending_input_tokens = 0
        estimator = TokenEstimator()
        _id_counter = 0

        def _next_id() -> str:
            nonlocal _id_counter
            _id_counter += 1
            return f"{_id_counter:08x}"

        next_record_id = None
        for message in messages:
            if message.is_contextual:
                continue
            record_id = next_record_id or _next_id()
            next_record_id = None
            if message.is_compaction:
                next_record_id = _next_id()
                records.append(
                    {
                        "type": "compaction",
                        "id": record_id,
                        "parentId": parent_id,
                        "timestamp": message.timestamp,
                        "summary": message.text,
                        "firstKeptEntryId": next_record_id,
                        "tokensBefore": 0,
                        "details": {},
                        "fromHook": True,
                    }
                )
                parent_id = record_id
                continue
            message_tokens = estimator.count(message.text)
            message_payload: JsonObject = {
                "role": message.role,
                "content": [{"type": "text", "text": message.text}],
                "timestamp": self._epoch_ms(message.timestamp),
            }
            if message.role == "assistant":
                message_payload["api"] = message.api or "openai-completions"
                message_payload["provider"] = message.provider or "session-export"
                message_payload["model"] = message.model or "imported"
                message_payload["usage"] = self._usage(pending_input_tokens, message_tokens)
                message_payload["stopReason"] = "stop"
                pending_input_tokens = 0
            else:
                pending_input_tokens += message_tokens
            records.append(
                {
                    "type": "message",
                    "id": record_id,
                    "parentId": parent_id,
                    "timestamp": message.timestamp,
                    "message": message_payload,
                }
            )
            parent_id = record_id
        return records

    @staticmethod
    def _usage(input_tokens: int, output_tokens: int) -> JsonObject:
        return {
            "input": input_tokens,
            "output": output_tokens,
            "cacheRead": 0,
            "cacheWrite": 0,
            "reasoning": 0,
            "totalTokens": input_tokens + output_tokens,
            "cost": {
                "input": 0,
                "output": 0,
                "cacheRead": 0,
                "cacheWrite": 0,
                "total": 0,
            },
        }

    @staticmethod
    def _epoch_ms(timestamp: str) -> int:
        normalized = timestamp.replace("Z", "+00:00")
        try:
            return int(datetime.fromisoformat(normalized).timestamp() * 1000)
        except ValueError:
            return int(datetime.now(UTC).timestamp() * 1000)


class CodexRecordBuilder:
    def build(
        self,
        session_id: str,
        cwd: str,
        timestamp: str,
        messages: list[TextMessage],
        model_provider: str = "session-export-import",
    ) -> list[JsonObject]:
        records: list[JsonObject] = [
            {
                "timestamp": timestamp,
                "type": "session_meta",
                "payload": {
                    "id": session_id,
                    "timestamp": timestamp,
                    "cwd": cwd,
                    "originator": "session-export",
                    "cli_version": "session-export",
                    "source": "cli",
                    "model_provider": model_provider,
                },
            }
        ]
        for message in messages:
            if message.is_contextual:
                continue
            if message.is_compaction:
                records.append(
                    {
                        "timestamp": message.timestamp,
                        "type": "compacted",
                        "payload": {
                            "message": message.text,
                        },
                    }
                )
                continue
            content_type = "output_text" if message.role == "assistant" else "input_text"
            records.append(
                {
                    "timestamp": message.timestamp,
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": message.role,
                        "content": [{"type": content_type, "text": message.text}],
                    },
                }
            )
        return records


class OpenCodeExportBuilder:
    def build(self, session_id: str, cwd: str, timestamp: str, messages: list[TextMessage]) -> list[JsonObject]:
        created = iso_to_epoch_ms(timestamp)
        title = self._title(messages)
        export: JsonObject = {
            "info": {
                "id": session_id,
                "slug": opencode_slug(title),
                "projectID": "global",
                "directory": cwd,
                "title": title,
                "version": "session-export",
                "time": {
                    "created": created,
                    "updated": self._updated_at(created, messages),
                },
            },
            "messages": self._messages(session_id, cwd, messages),
        }
        return [export]

    def _messages(self, session_id: str, cwd: str, messages: list[TextMessage]) -> list[JsonObject]:
        exported: list[JsonObject] = []
        parent_id = ""
        for index, message in enumerate(messages):
            if message.is_contextual:
                continue
            created = iso_to_epoch_ms(message.timestamp)
            message_id = opencode_id("msg", message.timestamp)
            if message.is_compaction:
                compaction_part_id = opencode_id("prt", message.timestamp)
                exported.append(
                    {
                        "info": {
                            "id": message_id,
                            "sessionID": session_id,
                            "role": "user",
                            "time": {"created": created},
                            "agent": "session-export",
                            "model": {"providerID": "session-export", "modelID": "imported"},
                        },
                        "parts": [
                            {
                                "id": compaction_part_id,
                                "sessionID": session_id,
                                "messageID": message_id,
                                "type": "compaction",
                                "auto": True,
                            }
                        ],
                    }
                )
                summary_msg_id = opencode_id("msg", message.timestamp)
                summary_part_id = opencode_id("prt", message.timestamp)
                exported.append(
                    {
                        "info": {
                            "id": summary_msg_id,
                            "sessionID": session_id,
                            "role": "assistant",
                            "time": {"created": created, "completed": created},
                            "parentID": message_id,
                            "modelID": "imported",
                            "providerID": "session-export",
                            "mode": "build",
                            "agent": "session-export",
                            "path": {"cwd": cwd, "root": cwd},
                            "cost": 0,
                            "summary": True,
                            "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}},
                        },
                        "parts": [
                            {
                                "id": summary_part_id,
                                "sessionID": session_id,
                                "messageID": summary_msg_id,
                                "type": "text",
                                "text": message.text,
                                "time": {"start": created, "end": created},
                            }
                        ],
                    }
                )
                parent_id = summary_msg_id
                continue
            part_id = opencode_id("prt", message.timestamp)
            info = self._message_info(session_id, message_id, parent_id, cwd, message, created)
            exported.append(
                {
                    "info": info,
                    "parts": [
                        {
                            "id": part_id,
                            "sessionID": session_id,
                            "messageID": message_id,
                            "type": "text",
                            "text": message.text,
                            "time": {"start": created, "end": created},
                        }
                    ],
                }
            )
            if message.role == "user" or index == 0:
                parent_id = message_id
        return exported

    @staticmethod
    def _message_info(
        session_id: str,
        message_id: str,
        parent_id: str,
        cwd: str,
        message: TextMessage,
        created: int,
    ) -> JsonObject:
        provider = message.provider or "session-export"
        model = message.model or "imported"
        if message.role == "assistant":
            return {
                "id": message_id,
                "sessionID": session_id,
                "role": "assistant",
                "time": {"created": created, "completed": created},
                "parentID": parent_id or message_id,
                "modelID": model,
                "providerID": provider,
                "mode": "build",
                "agent": "session-export",
                "path": {"cwd": cwd, "root": cwd},
                "cost": 0,
                "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}},
            }
        return {
            "id": message_id,
            "sessionID": session_id,
            "role": "user",
            "time": {"created": created},
            "agent": "session-export",
            "model": {"providerID": provider, "modelID": model},
        }

    @staticmethod
    def _title(messages: list[TextMessage]) -> str:
        for message in messages:
            if message.role == "user" and message.text.strip():
                return message.text.strip().splitlines()[0][:80]
        return "Imported session"

    @staticmethod
    def _updated_at(created: int, messages: list[TextMessage]) -> int:
        if not messages:
            return created
        return max(iso_to_epoch_ms(message.timestamp) for message in messages)


class ClaudeRecordBuilder:
    def build(
        self,
        session_id: str,
        cwd: str,
        timestamp: str,
        messages: list[TextMessage],
        version: str = "session-export",
    ) -> list[JsonObject]:
        records: list[JsonObject] = []
        parent_uuid: str | None = None
        from uuid import uuid4
        for message in messages:
            if message.is_contextual:
                continue
            entry_uuid = str(uuid4())
            if message.is_compaction:
                boundary_uuid = str(uuid4())
                records.append({
                    "type": "system",
                    "subtype": "compact_boundary",
                    "content": "Conversation compacted",
                    "isMeta": False,
                    "timestamp": message.timestamp,
                    "uuid": boundary_uuid,
                    "parentUuid": None,
                    "logicalParentUuid": parent_uuid,
                    "level": "info",
                    "compactMetadata": {"trigger": "manual", "preTokens": 0},
                    "isSidechain": False,
                    "userType": "external",
                    "entrypoint": "cli",
                    "cwd": cwd,
                    "sessionId": session_id,
                    "version": version,
                })
                parent_uuid = boundary_uuid
                entry_uuid = str(uuid4())
            content = message.text
            if message.role == "assistant":
                msg_obj: JsonObject = {
                    "role": "assistant",
                    "content": [{"type": "text", "text": content}],
                    "model": message.model or "imported",
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                    "stop_reason": "end_turn",
                }
            else:
                msg_obj = {"role": "user", "content": content}
            records.append({
                "type": message.role if message.role in ("user", "assistant") else "user",
                "message": msg_obj,
                "uuid": entry_uuid,
                "parentUuid": None if message.is_compaction else parent_uuid,
                "isSidechain": False,
                "userType": "external",
                "entrypoint": "cli",
                "cwd": cwd,
                "sessionId": session_id,
                "timestamp": message.timestamp,
                "version": version,
            })
            parent_uuid = entry_uuid
        return records


class CodexToPiConverter:
    def __init__(
        self,
        codex_store: CodexStore,
        pi_store: PiStore,
        dcp_store: PiDcpStore,
        id_factory: SessionIdFactory,
    ) -> None:
        self._codex_store = codex_store
        self._pi_store = pi_store
        self._dcp_store = dcp_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = PiRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._codex_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        messages = self._extractor.from_codex(source)
        records = self._builder.build(resolved_id, source.cwd, self._timestamp(source.timestamp), messages)
        destination = self._pi_store.destination_path(resolved_id, self._timestamp(source.timestamp), source.cwd)
        dcp_path = self._dcp_store.destination_path(resolved_id)
        return ConversionPlan(source, destination, records, (dcp_path,))

    def has_changes(self, session_id: str) -> bool:
        source_path = self._codex_store._find_path(session_id)
        if source_path is None:
            return True
        meta = self._codex_store._read_head_meta(source_path)
        source_id = string_value(meta, "id") or self._codex_store._id_from_filename(source_path)
        source_ts = string_value(meta, "timestamp") or self._codex_store._timestamp_from_file(source_path)
        source_cwd = string_value(meta, "cwd") or ""
        target_id = self._id_factory.create(source_id)
        destination = self._pi_store.destination_path(target_id, self._timestamp(source_ts), source_cwd)
        if not destination.exists():
            return True
        return _count_jsonl_records(source_path) != _count_jsonl_records(destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._pi_store.write(plan.destination, plan.records, overwrite=overwrite)
        for service_path in plan.services:
            self._dcp_store.write_default(self._target_id(plan.destination), service_path, overwrite=overwrite)

    @staticmethod
    def _target_id(path: Path) -> str:
        return path.stem.rsplit("_", 1)[-1]

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class PiToCodexConverter:
    def __init__(self, pi_store: PiStore, codex_store: CodexStore, id_factory: SessionIdFactory) -> None:
        self._pi_store = pi_store
        self._codex_store = codex_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = CodexRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._pi_store.load(session_id)
        resolved_id = target_id or self._id_factory.create_codex(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_pi(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages, "pi-import")
        destination = self._codex_store.destination_path(resolved_id, timestamp)
        return ConversionPlan(source, destination, records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._pi_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._codex_store.destination_path(
            self._id_factory.create_codex(session_id),
            self._timestamp(""),
        )
        if not destination.exists():
            return True
        return _count_jsonl_records(source_path) != _count_jsonl_records(destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._codex_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class CodexToOpenCodeConverter:
    def __init__(self, codex_store: CodexStore, opencode_store: OpenCodeStore, id_factory: SessionIdFactory) -> None:
        self._codex_store = codex_store
        self._opencode_store = opencode_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = OpenCodeExportBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._codex_store.load(session_id)
        timestamp = self._timestamp(source.timestamp)
        resolved_id = target_id or self._id_factory.create_opencode(source.session_id, timestamp)
        messages = self._extractor.from_codex(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        return ConversionPlan(source, self._opencode_store.destination_path(resolved_id), records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._codex_store._find_path(session_id)
        if source_path is None:
            return True
        meta = self._codex_store._read_head_meta(source_path)
        source_id = string_value(meta, "id") or self._codex_store._id_from_filename(source_path)
        source_ts = string_value(meta, "timestamp") or self._codex_store._timestamp_from_file(source_path)
        target_id = self._id_factory.create_opencode(source_id, self._timestamp(source_ts))
        destination = self._opencode_store.destination_path(target_id)
        if not destination.exists():
            return True
        return _count_jsonl_records(source_path) != _count_opencode_records(destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._opencode_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class PiToOpenCodeConverter:
    def __init__(self, pi_store: PiStore, opencode_store: OpenCodeStore, id_factory: SessionIdFactory) -> None:
        self._pi_store = pi_store
        self._opencode_store = opencode_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = OpenCodeExportBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._pi_store.load(session_id)
        timestamp = self._timestamp(source.timestamp)
        resolved_id = target_id or self._id_factory.create_opencode(source.session_id, timestamp)
        messages = self._extractor.from_pi(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        return ConversionPlan(source, self._opencode_store.destination_path(resolved_id), records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._pi_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._opencode_store.destination_path(
            self._id_factory.create_opencode(session_id, self._timestamp("")),
        )
        if not destination.exists():
            return True
        return _count_jsonl_records(source_path) != _count_opencode_records(destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._opencode_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class OpenCodeToCodexConverter:
    def __init__(self, opencode_store: OpenCodeStore, codex_store: CodexStore, id_factory: SessionIdFactory) -> None:
        self._opencode_store = opencode_store
        self._codex_store = codex_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = CodexRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._opencode_store.load(session_id)
        resolved_id = target_id or self._id_factory.create_codex(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_opencode(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages, "opencode-import")
        destination = self._codex_store.destination_path(resolved_id, timestamp)
        return ConversionPlan(source, destination, records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._opencode_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._codex_store.destination_path(
            self._id_factory.create_codex(session_id),
            self._timestamp(""),
        )
        if not destination.exists():
            return True
        return _count_opencode_records(source_path) != _count_jsonl_records(destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._codex_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class OpenCodeToPiConverter:
    def __init__(
        self,
        opencode_store: OpenCodeStore,
        pi_store: PiStore,
        dcp_store: PiDcpStore,
        id_factory: SessionIdFactory,
    ) -> None:
        self._opencode_store = opencode_store
        self._pi_store = pi_store
        self._dcp_store = dcp_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = PiRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._opencode_store.load(session_id)
        resolved_id = target_id or self._id_factory.create_codex(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_opencode(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        destination = self._pi_store.destination_path(resolved_id, timestamp, source.cwd)
        dcp_path = self._dcp_store.destination_path(resolved_id)
        return ConversionPlan(source, destination, records, (dcp_path,))

    def has_changes(self, session_id: str) -> bool:
        source_path = self._opencode_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._pi_store.destination_path(
            self._id_factory.create_codex(session_id),
            self._timestamp(""),
            "",
        )
        if not destination.exists():
            return True
        return _count_opencode_records(source_path) != _count_jsonl_records(destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._pi_store.write(plan.destination, plan.records, overwrite=overwrite)
        for service_path in plan.services:
            self._dcp_store.write_default(self._target_id(plan.destination), service_path, overwrite=overwrite)

    @staticmethod
    def _target_id(path: Path) -> str:
        return path.stem.rsplit("_", 1)[-1]

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ClaudeToPiConverter:
    def __init__(self, claude_store: ClaudeStore, pi_store: PiStore, dcp_store: PiDcpStore, id_factory: SessionIdFactory) -> None:
        self._claude_store = claude_store
        self._pi_store = pi_store
        self._dcp_store = dcp_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = PiRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._claude_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_claude(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        destination = self._pi_store.destination_path(resolved_id, timestamp, source.cwd)
        dcp_path = self._dcp_store.destination_path(resolved_id)
        return ConversionPlan(source, destination, records, (dcp_path,))

    def has_changes(self, session_id: str) -> bool:
        source_path = self._claude_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._pi_store.destination_path(self._id_factory.create(session_id), self._timestamp(""), "")
        if not destination.exists():
            return True
        return _count_jsonl_records(source_path) != _count_jsonl_records(destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._pi_store.write(plan.destination, plan.records, overwrite=overwrite)
        for service_path in plan.services:
            self._dcp_store.write_default(self._target_id(plan.destination), service_path, overwrite=overwrite)

    @staticmethod
    def _target_id(path: Path) -> str:
        return path.stem.rsplit("_", 1)[-1]

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class PiToClaudeConverter:
    def __init__(self, pi_store: PiStore, claude_store: ClaudeStore, id_factory: SessionIdFactory) -> None:
        self._pi_store = pi_store
        self._claude_store = claude_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = ClaudeRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._pi_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_pi(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        destination = self._claude_store.destination_path(resolved_id, source.cwd)
        return ConversionPlan(source, destination, records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._pi_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._claude_store.destination_path(self._id_factory.create(session_id), "")
        if not destination.exists():
            return True
        return _count_jsonl_records(source_path) != _count_jsonl_records(destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._claude_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ClaudeToCodexConverter:
    def __init__(self, claude_store: ClaudeStore, codex_store: CodexStore, id_factory: SessionIdFactory) -> None:
        self._claude_store = claude_store
        self._codex_store = codex_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = CodexRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._claude_store.load(session_id)
        resolved_id = target_id or self._id_factory.create_codex(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_claude(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages, "claude-import")
        destination = self._codex_store.destination_path(resolved_id, timestamp)
        return ConversionPlan(source, destination, records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._claude_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._codex_store.destination_path(self._id_factory.create_codex(session_id), self._timestamp(""))
        if not destination.exists():
            return True
        return _count_jsonl_records(source_path) != _count_jsonl_records(destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._codex_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class CodexToClaudeConverter:
    def __init__(self, codex_store: CodexStore, claude_store: ClaudeStore, id_factory: SessionIdFactory) -> None:
        self._codex_store = codex_store
        self._claude_store = claude_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = ClaudeRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._codex_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_codex(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        destination = self._claude_store.destination_path(resolved_id, source.cwd)
        return ConversionPlan(source, destination, records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._codex_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._claude_store.destination_path(self._id_factory.create(session_id), "")
        if not destination.exists():
            return True
        return _count_jsonl_records(source_path) != _count_jsonl_records(destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._claude_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")




class ClaudeToOpenCodeConverter:
    def __init__(self, claude_store: ClaudeStore, opencode_store: OpenCodeStore, id_factory: SessionIdFactory) -> None:
        self._claude_store = claude_store
        self._opencode_store = opencode_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = OpenCodeExportBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._claude_store.load(session_id)
        timestamp = self._timestamp(source.timestamp)
        resolved_id = target_id or self._id_factory.create_opencode(source.session_id, timestamp)
        messages = self._extractor.from_claude(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        return ConversionPlan(source, self._opencode_store.destination_path(resolved_id), records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._claude_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._opencode_store.destination_path(self._id_factory.create_opencode(session_id, self._timestamp("")))
        if not destination.exists():
            return True
        return _count_jsonl_records(source_path) != _count_opencode_records(destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._opencode_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class OpenCodeToClaudeConverter:
    def __init__(self, opencode_store: OpenCodeStore, claude_store: ClaudeStore, id_factory: SessionIdFactory) -> None:
        self._opencode_store = opencode_store
        self._claude_store = claude_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = ClaudeRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._opencode_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_opencode(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        destination = self._claude_store.destination_path(resolved_id, source.cwd)
        return ConversionPlan(source, destination, records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._opencode_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._claude_store.destination_path(self._id_factory.create(session_id), "")
        if not destination.exists():
            return True
        return _count_opencode_records(source_path) != _count_jsonl_records(destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._claude_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class DevinRecordBuilder:
    def build(self, session_id: str, cwd: str, timestamp: str, messages: list[TextMessage]) -> list[JsonObject]:
        steps: list[JsonObject] = []
        step_id = 0
        for message in messages:
            if message.is_contextual:
                continue
            step_id += 1
            source = "agent" if message.role == "assistant" else "user"
            step: JsonObject = {
                "step_id": step_id,
                "timestamp": message.timestamp,
                "source": source,
                "message": message.text,
            }
            if message.is_compaction:
                step["source"] = "system"
                step["extra"] = {"compaction": True}
            elif message.role == "assistant":
                step["extra"] = {
                    "generation_model": message.model or "imported",
                    "telemetry": {"source": "assistant", "operation": "inference"},
                }
            steps.append(step)
        export: JsonObject = {
            "schema_version": "ATIF-v1.7",
            "session_id": session_id,
            "agent": {
                "name": "devin",
                "version": "session-export",
                "model_name": "imported",
                "extra": {"backend": "session-export", "cwd": cwd},
            },
            "steps": steps,
            "final_metrics": {
                "total_prompt_tokens": 0,
                "total_completion_tokens": 0,
                "total_cached_tokens": 0,
                "total_steps": len(steps),
            },
        }
        return [export]


class DevinToPiConverter:
    def __init__(self, devin_store: DevinStore, pi_store: PiStore, dcp_store: PiDcpStore, id_factory: SessionIdFactory) -> None:
        self._devin_store = devin_store
        self._pi_store = pi_store
        self._dcp_store = dcp_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = PiRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._devin_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_devin(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        destination = self._pi_store.destination_path(resolved_id, timestamp, source.cwd)
        dcp_path = self._dcp_store.destination_path(resolved_id)
        return ConversionPlan(source, destination, records, (dcp_path,))

    def has_changes(self, session_id: str) -> bool:
        source_path = self._devin_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._pi_store.destination_path(self._id_factory.create(session_id), self._timestamp(""), "")
        if not destination.exists():
            return True
        return _count_jsonl_records(source_path) != _count_jsonl_records(destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._pi_store.write(plan.destination, plan.records, overwrite=overwrite)
        for service_path in plan.services:
            self._dcp_store.write_default(self._target_id(plan.destination), service_path, overwrite=overwrite)

    @staticmethod
    def _target_id(path: Path) -> str:
        return path.stem.rsplit("_", 1)[-1]

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class PiToDevinConverter:
    def __init__(self, pi_store: PiStore, devin_store: DevinStore, id_factory: SessionIdFactory) -> None:
        self._pi_store = pi_store
        self._devin_store = devin_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = DevinRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._pi_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_pi(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        return ConversionPlan(source, self._devin_store.destination_path(resolved_id), records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._pi_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._devin_store.destination_path(self._id_factory.create(session_id))
        if not destination.exists():
            return True
        return _count_jsonl_records(source_path) != _count_opencode_records(destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._devin_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class DevinToCodexConverter:
    def __init__(self, devin_store: DevinStore, codex_store: CodexStore, id_factory: SessionIdFactory) -> None:
        self._devin_store = devin_store
        self._codex_store = codex_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = CodexRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._devin_store.load(session_id)
        resolved_id = target_id or self._id_factory.create_codex(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_devin(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages, "devin-import")
        destination = self._codex_store.destination_path(resolved_id, timestamp)
        return ConversionPlan(source, destination, records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._devin_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._codex_store.destination_path(self._id_factory.create_codex(session_id), self._timestamp(""))
        if not destination.exists():
            return True
        return _count_jsonl_records(source_path) != _count_jsonl_records(destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._codex_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class CodexToDevinConverter:
    def __init__(self, codex_store: CodexStore, devin_store: DevinStore, id_factory: SessionIdFactory) -> None:
        self._codex_store = codex_store
        self._devin_store = devin_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = DevinRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._codex_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_codex(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        return ConversionPlan(source, self._devin_store.destination_path(resolved_id), records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._codex_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._devin_store.destination_path(self._id_factory.create(session_id))
        if not destination.exists():
            return True
        return _count_jsonl_records(source_path) != _count_opencode_records(destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._devin_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class DevinToOpenCodeConverter:
    def __init__(self, devin_store: DevinStore, opencode_store: OpenCodeStore, id_factory: SessionIdFactory) -> None:
        self._devin_store = devin_store
        self._opencode_store = opencode_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = OpenCodeExportBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._devin_store.load(session_id)
        timestamp = self._timestamp(source.timestamp)
        resolved_id = target_id or self._id_factory.create_opencode(source.session_id, timestamp)
        messages = self._extractor.from_devin(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        return ConversionPlan(source, self._opencode_store.destination_path(resolved_id), records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._devin_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._opencode_store.destination_path(self._id_factory.create_opencode(session_id, self._timestamp("")))
        if not destination.exists():
            return True
        return _count_jsonl_records(source_path) != _count_opencode_records(destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._opencode_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class OpenCodeToDevinConverter:
    def __init__(self, opencode_store: OpenCodeStore, devin_store: DevinStore, id_factory: SessionIdFactory) -> None:
        self._opencode_store = opencode_store
        self._devin_store = devin_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = DevinRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._opencode_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_opencode(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        return ConversionPlan(source, self._devin_store.destination_path(resolved_id), records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._opencode_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._devin_store.destination_path(self._id_factory.create(session_id))
        if not destination.exists():
            return True
        return _count_opencode_records(source_path) != _count_opencode_records(destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._devin_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class DevinToClaudeConverter:
    def __init__(self, devin_store: DevinStore, claude_store: ClaudeStore, id_factory: SessionIdFactory) -> None:
        self._devin_store = devin_store
        self._claude_store = claude_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = ClaudeRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._devin_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_devin(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        destination = self._claude_store.destination_path(resolved_id, source.cwd)
        return ConversionPlan(source, destination, records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._devin_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._claude_store.destination_path(self._id_factory.create(session_id), "")
        if not destination.exists():
            return True
        return _count_jsonl_records(source_path) != _count_jsonl_records(destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._claude_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ClaudeToDevinConverter:
    def __init__(self, claude_store: ClaudeStore, devin_store: DevinStore, id_factory: SessionIdFactory) -> None:
        self._claude_store = claude_store
        self._devin_store = devin_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = DevinRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._claude_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_claude(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        return ConversionPlan(source, self._devin_store.destination_path(resolved_id), records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._claude_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._devin_store.destination_path(self._id_factory.create(session_id))
        if not destination.exists():
            return True
        return _count_jsonl_records(source_path) != _count_opencode_records(destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._devin_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class FactoryRecordBuilder:
    def build(self, session_id: str, cwd: str, timestamp: str, messages: list[TextMessage]) -> list[JsonObject]:
        header: JsonObject = {
            "type": "session_start",
            "id": session_id,
            "title": "Imported Session",
            "owner": "session-export",
        }
        if cwd:
            header["cwd"] = cwd
        if timestamp:
            header["timestamp"] = timestamp
        records: list[JsonObject] = [header]
        for message in messages:
            if message.is_contextual:
                continue
            if message.is_compaction:
                continue
            content: list[JsonObject] = [{"type": "text", "text": message.text}]
            records.append({
                "type": "message",
                "id": str(uuid4()),
                "timestamp": message.timestamp,
                "message": {
                    "role": message.role,
                    "content": content,
                },
            })
        return records


class FactoryToPiConverter:
    def __init__(self, factory_store: FactoryStore, pi_store: PiStore, dcp_store: PiDcpStore, id_factory: SessionIdFactory) -> None:
        self._factory_store = factory_store
        self._pi_store = pi_store
        self._dcp_store = dcp_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = PiRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._factory_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_factory(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        destination = self._pi_store.destination_path(resolved_id, timestamp, source.cwd)
        dcp_path = self._dcp_store.destination_path(resolved_id)
        return ConversionPlan(source, destination, records, (dcp_path,))

    def has_changes(self, session_id: str) -> bool:
        source_path = self._factory_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._pi_store.destination_path(self._id_factory.create(session_id), self._timestamp(""), "")
        if not destination.exists():
            return True
        return _count_jsonl_records(source_path) != _count_jsonl_records(destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._pi_store.write(plan.destination, plan.records, overwrite=overwrite)
        for service_path in plan.services:
            self._dcp_store.write_default(self._target_id(plan.destination), service_path, overwrite=overwrite)

    @staticmethod
    def _target_id(path: Path) -> str:
        return path.stem.rsplit("_", 1)[-1]

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class PiToFactoryConverter:
    def __init__(self, pi_store: PiStore, factory_store: FactoryStore, id_factory: SessionIdFactory) -> None:
        self._pi_store = pi_store
        self._factory_store = factory_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = FactoryRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._pi_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_pi(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        return ConversionPlan(source, self._factory_store.destination_path(resolved_id, source.cwd), records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._pi_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._factory_store.destination_path(self._id_factory.create(session_id), "")
        if not destination.exists():
            return True
        return _count_jsonl_records(source_path) != _count_jsonl_records(destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._factory_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class FactoryToCodexConverter:
    def __init__(self, factory_store: FactoryStore, codex_store: CodexStore, id_factory: SessionIdFactory) -> None:
        self._factory_store = factory_store
        self._codex_store = codex_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = CodexRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._factory_store.load(session_id)
        resolved_id = target_id or self._id_factory.create_codex(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_factory(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages, "factory-import")
        destination = self._codex_store.destination_path(resolved_id, timestamp)
        return ConversionPlan(source, destination, records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._factory_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._codex_store.destination_path(self._id_factory.create_codex(session_id), self._timestamp(""))
        if not destination.exists():
            return True
        return _count_jsonl_records(source_path) != _count_jsonl_records(destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._codex_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class CodexToFactoryConverter:
    def __init__(self, codex_store: CodexStore, factory_store: FactoryStore, id_factory: SessionIdFactory) -> None:
        self._codex_store = codex_store
        self._factory_store = factory_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = FactoryRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._codex_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_codex(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        return ConversionPlan(source, self._factory_store.destination_path(resolved_id, source.cwd), records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._codex_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._factory_store.destination_path(self._id_factory.create(session_id), "")
        if not destination.exists():
            return True
        return _count_jsonl_records(source_path) != _count_jsonl_records(destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._factory_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class FactoryToOpenCodeConverter:
    def __init__(self, factory_store: FactoryStore, opencode_store: OpenCodeStore, id_factory: SessionIdFactory) -> None:
        self._factory_store = factory_store
        self._opencode_store = opencode_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = OpenCodeExportBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._factory_store.load(session_id)
        timestamp = self._timestamp(source.timestamp)
        resolved_id = target_id or self._id_factory.create_opencode(source.session_id, timestamp)
        messages = self._extractor.from_factory(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        return ConversionPlan(source, self._opencode_store.destination_path(resolved_id), records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._factory_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._opencode_store.destination_path(self._id_factory.create_opencode(session_id, self._timestamp("")))
        if not destination.exists():
            return True
        return _count_jsonl_records(source_path) != _count_opencode_records(destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._opencode_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class OpenCodeToFactoryConverter:
    def __init__(self, opencode_store: OpenCodeStore, factory_store: FactoryStore, id_factory: SessionIdFactory) -> None:
        self._opencode_store = opencode_store
        self._factory_store = factory_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = FactoryRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._opencode_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_opencode(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        return ConversionPlan(source, self._factory_store.destination_path(resolved_id, source.cwd), records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._opencode_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._factory_store.destination_path(self._id_factory.create(session_id), "")
        if not destination.exists():
            return True
        return _count_opencode_records(source_path) != _count_jsonl_records(destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._factory_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class FactoryToClaudeConverter:
    def __init__(self, factory_store: FactoryStore, claude_store: ClaudeStore, id_factory: SessionIdFactory) -> None:
        self._factory_store = factory_store
        self._claude_store = claude_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = ClaudeRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._factory_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_factory(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        destination = self._claude_store.destination_path(resolved_id, source.cwd)
        return ConversionPlan(source, destination, records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._factory_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._claude_store.destination_path(self._id_factory.create(session_id), "")
        if not destination.exists():
            return True
        return _count_jsonl_records(source_path) != _count_jsonl_records(destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._claude_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ClaudeToFactoryConverter:
    def __init__(self, claude_store: ClaudeStore, factory_store: FactoryStore, id_factory: SessionIdFactory) -> None:
        self._claude_store = claude_store
        self._factory_store = factory_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = FactoryRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._claude_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_claude(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        return ConversionPlan(source, self._factory_store.destination_path(resolved_id, source.cwd), records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._claude_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._factory_store.destination_path(self._id_factory.create(session_id), "")
        if not destination.exists():
            return True
        return _count_jsonl_records(source_path) != _count_jsonl_records(destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._factory_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class FactoryToDevinConverter:
    def __init__(self, factory_store: FactoryStore, devin_store: DevinStore, id_factory: SessionIdFactory) -> None:
        self._factory_store = factory_store
        self._devin_store = devin_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = DevinRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._factory_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_factory(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        return ConversionPlan(source, self._devin_store.destination_path(resolved_id), records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._factory_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._devin_store.destination_path(self._id_factory.create(session_id))
        if not destination.exists():
            return True
        return _count_jsonl_records(source_path) != _count_opencode_records(destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._devin_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class DevinToFactoryConverter:
    def __init__(self, devin_store: DevinStore, factory_store: FactoryStore, id_factory: SessionIdFactory) -> None:
        self._devin_store = devin_store
        self._factory_store = factory_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = FactoryRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._devin_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_devin(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        return ConversionPlan(source, self._factory_store.destination_path(resolved_id, source.cwd), records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._devin_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._factory_store.destination_path(self._id_factory.create(session_id), "")
        if not destination.exists():
            return True
        return _count_jsonl_records(source_path) != _count_jsonl_records(destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._factory_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _encode_varint(value: int) -> bytes:
    """Encode an integer as a protobuf varint."""
    result = bytearray()
    while value > 0x7F:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value & 0x7F)
    return bytes(result)


def _encode_tag(field_no: int, wire_type: int) -> bytes:
    return _encode_varint((field_no << 3) | wire_type)


def _encode_string_field(field_no: int, text: str) -> bytes:
    encoded = text.encode("utf-8")
    return _encode_tag(field_no, 2) + _encode_varint(len(encoded)) + encoded


def _encode_bytes_field(field_no: int, data: bytes) -> bytes:
    return _encode_tag(field_no, 2) + _encode_varint(len(data)) + data


def _encode_varint_field(field_no: int, value: int) -> bytes:
    return _encode_tag(field_no, 0) + _encode_varint(value)


def _encode_message_field(field_no: int, message_bytes: bytes) -> bytes:
    return _encode_tag(field_no, 2) + _encode_varint(len(message_bytes)) + message_bytes


class WindsurfRecordBuilder:
    """Build encrypted Windsurf Cascade .pb files from TextMessage stream.

    Serializes a CortexTrajectory protobuf with user_input and planner_response
    steps, then encrypts with AES-256-GCM using the hardcoded Windsurf key.
    """

    _ENCRYPTION_KEY = b"safeCodeiumworldKeYsecretBalloon"
    _NONCE_SIZE = 12

    def build(self, session_id: str, cwd: str, timestamp: str, messages: list[TextMessage]) -> list[bytes]:
        from session_sdk.paths import iso_to_epoch_ms
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError as exc:
            raise ImportError("cryptography package required for Windsurf session encryption") from exc

        steps_bytes: list[bytes] = []
        step_idx = 0
        for message in messages:
            if message.is_contextual:
                continue
            step_idx += 1
            ts_seconds = iso_to_epoch_ms(message.timestamp) // 1000
            metadata = _encode_message_field(5,
                _encode_message_field(1,
                    _encode_varint_field(1, ts_seconds)
                )
            )
            if message.is_compaction:
                cp_inner = _encode_string_field(5, message.text)
                step = (
                    _encode_varint_field(1, 15) +
                    _encode_varint_field(4, 3) +
                    metadata +
                    _encode_message_field(30, cp_inner)
                )
            elif message.role == "assistant":
                variant = b""
                if message.text:
                    variant += _encode_string_field(1, message.text)
                step = (
                    _encode_varint_field(1, 14) +
                    _encode_varint_field(4, 3) +
                    metadata +
                    _encode_message_field(20, variant)
                )
            else:
                variant = _encode_string_field(2, message.text)
                step = (
                    _encode_varint_field(1, 14) +
                    _encode_varint_field(4, 3) +
                    metadata +
                    _encode_message_field(19, variant)
                )
            steps_bytes.append(step)

        # Build CortexTrajectory
        trajectory = _encode_string_field(1, session_id)
        for step_b in steps_bytes:
            trajectory += _encode_message_field(2, step_b)
        trajectory += _encode_varint_field(4, 4)

        # Encrypt: [12-byte nonce][ciphertext+16-byte GCM tag]
        import os as _os
        nonce = _os.urandom(self._NONCE_SIZE)
        ciphertext = AESGCM(self._ENCRYPTION_KEY).encrypt(nonce, trajectory, None)
        return [nonce + ciphertext]


class WindsurfToPiConverter:
    def __init__(self, windsurf_store: WindsurfStore, pi_store: PiStore, dcp_store: PiDcpStore, id_factory: SessionIdFactory) -> None:
        self._windsurf_store = windsurf_store
        self._pi_store = pi_store
        self._dcp_store = dcp_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = PiRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._windsurf_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_windsurf(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        destination = self._pi_store.destination_path(resolved_id, timestamp, source.cwd)
        dcp_path = self._dcp_store.destination_path(resolved_id)
        return ConversionPlan(source, destination, records, (dcp_path,))

    def has_changes(self, session_id: str) -> bool:
        source_path = self._windsurf_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._pi_store.destination_path(self._id_factory.create(session_id), self._timestamp(""), "")
        if not destination.exists():
            return True
        return True

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._pi_store.write(plan.destination, plan.records, overwrite=overwrite)
        for service_path in plan.services:
            self._dcp_store.write_default(self._target_id(plan.destination), service_path, overwrite=overwrite)

    @staticmethod
    def _target_id(path: Path) -> str:
        return path.stem.rsplit("_", 1)[-1]

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class PiToWindsurfConverter:
    def __init__(self, pi_store: PiStore, windsurf_store: WindsurfStore, id_factory: SessionIdFactory) -> None:
        self._pi_store = pi_store
        self._windsurf_store = windsurf_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = WindsurfRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._pi_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_pi(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        return ConversionPlan(source, self._windsurf_store.destination_path(resolved_id), records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._pi_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._windsurf_store.destination_path(self._id_factory.create(session_id))
        if not destination.exists():
            return True
        return True

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._windsurf_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")



class WindsurfToCodexConverter:
    def __init__(self, windsurf_store: WindsurfStore, codex_store: CodexStore, id_factory: SessionIdFactory) -> None:
        self._windsurf_store = windsurf_store
        self._codex_store = codex_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = CodexRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._windsurf_store.load(session_id)
        resolved_id = target_id or self._id_factory.create_codex(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_windsurf(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages, "windsurf-import")
        destination = self._codex_store.destination_path(resolved_id, timestamp)
        return ConversionPlan(source, destination, records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._windsurf_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._codex_store.destination_path(self._id_factory.create_codex(session_id), self._timestamp(""))
        if not destination.exists():
            return True
        return True

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._codex_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class CodexToWindsurfConverter:
    def __init__(self, codex_store: CodexStore, windsurf_store: WindsurfStore, id_factory: SessionIdFactory) -> None:
        self._codex_store = codex_store
        self._windsurf_store = windsurf_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = WindsurfRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._codex_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_codex(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        return ConversionPlan(source, self._windsurf_store.destination_path(resolved_id), records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._codex_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._windsurf_store.destination_path(self._id_factory.create(session_id))
        if not destination.exists():
            return True
        return True

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._windsurf_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class WindsurfToOpenCodeConverter:
    def __init__(self, windsurf_store: WindsurfStore, opencode_store: OpenCodeStore, id_factory: SessionIdFactory) -> None:
        self._windsurf_store = windsurf_store
        self._opencode_store = opencode_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = OpenCodeExportBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._windsurf_store.load(session_id)
        timestamp = self._timestamp(source.timestamp)
        resolved_id = target_id or self._id_factory.create_opencode(source.session_id, timestamp)
        messages = self._extractor.from_windsurf(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        return ConversionPlan(source, self._opencode_store.destination_path(resolved_id), records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._windsurf_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._opencode_store.destination_path(self._id_factory.create_opencode(session_id, self._timestamp("")))
        if not destination.exists():
            return True
        return True

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._opencode_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class OpenCodeToWindsurfConverter:
    def __init__(self, opencode_store: OpenCodeStore, windsurf_store: WindsurfStore, id_factory: SessionIdFactory) -> None:
        self._opencode_store = opencode_store
        self._windsurf_store = windsurf_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = WindsurfRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._opencode_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_opencode(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        return ConversionPlan(source, self._windsurf_store.destination_path(resolved_id), records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._opencode_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._windsurf_store.destination_path(self._id_factory.create(session_id))
        if not destination.exists():
            return True
        return True

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._windsurf_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class WindsurfToClaudeConverter:
    def __init__(self, windsurf_store: WindsurfStore, claude_store: ClaudeStore, id_factory: SessionIdFactory) -> None:
        self._windsurf_store = windsurf_store
        self._claude_store = claude_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = ClaudeRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._windsurf_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_windsurf(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        destination = self._claude_store.destination_path(resolved_id, source.cwd)
        return ConversionPlan(source, destination, records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._windsurf_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._claude_store.destination_path(self._id_factory.create(session_id), "")
        if not destination.exists():
            return True
        return True

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._claude_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ClaudeToWindsurfConverter:
    def __init__(self, claude_store: ClaudeStore, windsurf_store: WindsurfStore, id_factory: SessionIdFactory) -> None:
        self._claude_store = claude_store
        self._windsurf_store = windsurf_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = WindsurfRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._claude_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_claude(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        return ConversionPlan(source, self._windsurf_store.destination_path(resolved_id), records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._claude_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._windsurf_store.destination_path(self._id_factory.create(session_id))
        if not destination.exists():
            return True
        return True

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._windsurf_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class WindsurfToDevinConverter:
    def __init__(self, windsurf_store: WindsurfStore, devin_store: DevinStore, id_factory: SessionIdFactory) -> None:
        self._windsurf_store = windsurf_store
        self._devin_store = devin_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = DevinRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._windsurf_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_windsurf(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        return ConversionPlan(source, self._devin_store.destination_path(resolved_id), records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._windsurf_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._devin_store.destination_path(self._id_factory.create(session_id))
        if not destination.exists():
            return True
        return True

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._devin_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class DevinToWindsurfConverter:
    def __init__(self, devin_store: DevinStore, windsurf_store: WindsurfStore, id_factory: SessionIdFactory) -> None:
        self._devin_store = devin_store
        self._windsurf_store = windsurf_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = WindsurfRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._devin_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_devin(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        return ConversionPlan(source, self._windsurf_store.destination_path(resolved_id), records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._devin_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._windsurf_store.destination_path(self._id_factory.create(session_id))
        if not destination.exists():
            return True
        return True

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._windsurf_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class WindsurfToFactoryConverter:
    def __init__(self, windsurf_store: WindsurfStore, factory_store: FactoryStore, id_factory: SessionIdFactory) -> None:
        self._windsurf_store = windsurf_store
        self._factory_store = factory_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = FactoryRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._windsurf_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_windsurf(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        return ConversionPlan(source, self._factory_store.destination_path(resolved_id, source.cwd), records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._windsurf_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._factory_store.destination_path(self._id_factory.create(session_id), "")
        if not destination.exists():
            return True
        return True

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._factory_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class FactoryToWindsurfConverter:
    def __init__(self, factory_store: FactoryStore, windsurf_store: WindsurfStore, id_factory: SessionIdFactory) -> None:
        self._factory_store = factory_store
        self._windsurf_store = windsurf_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = WindsurfRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._factory_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_factory(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        return ConversionPlan(source, self._windsurf_store.destination_path(resolved_id), records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._factory_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._windsurf_store.destination_path(self._id_factory.create(session_id))
        if not destination.exists():
            return True
        return True

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._windsurf_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
