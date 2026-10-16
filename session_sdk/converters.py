from __future__ import annotations

import functools
import json
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable, Protocol
from uuid import NAMESPACE_URL, uuid5

from session_sdk.options import current_options
from session_sdk.json_types import JsonObject, as_list, as_object, as_str, sequence_to_text, string_value
from session_sdk.jsonl import _loads
from session_sdk.models import ConversionPlan, NativeSession, TextMessage, ToolCall
from session_sdk.paths import SessionIdFactory, epoch_ms_to_iso, iso_to_epoch_ms, opencode_id, opencode_slug, stable_opencode_id, stable_session_uuid
from session_sdk.windsurf_pb import (
    VARIANT_CODE_ACTION,
    VARIANT_ERROR_MESSAGE,
    VARIANT_TODO_LIST,
    iter_fields,
    parse_step_error,
    error_details_text,
    STEP_TYPE_CHECKPOINT,
    STEP_TYPE_FIND,
    STEP_TYPE_GREP_SEARCH,
    STEP_TYPE_LIST_DIRECTORY,
    STEP_TYPE_MCP_TOOL,
    STEP_TYPE_PLANNER_RESPONSE,
    STEP_TYPE_RUN_COMMAND,
    STEP_TYPE_USER_INPUT,
    STEP_TYPE_VIEW_FILE,
    VARIANT_CHECKPOINT,
    VARIANT_PLANNER_RESPONSE,
    VARIANT_USER_INPUT,
    VARIANT_COMMAND_RESULT,
    VARIANT_FIND_FILES,
    VARIANT_GREP,
    VARIANT_LIST_DIRECTORY,
    VARIANT_MCP_CALL,
    VARIANT_RUN_COMMAND,
    VARIANT_VIEW_FILE,
)
from session_sdk.stores import (
    T3_PROVIDER_DRIVERS,
    ClaudeStore,
    CodexStore,
    DevinStore,
    FactoryStore,
    FreebuffStore,
    GrokStore,
    OmpStore,
    OpenCodeStore,
    PiDcpStore,
    PiStore,
    SessionStore,
    T3Store,
    WindsurfStore,
    destination_exists,
    grok_db_message_count,
    omp_title_slot,
)


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


# Windsurf step variants that are tool invocations (see windsurf_pb for the layouts).
_WINDSURF_TOOL_VARIANTS = frozenset({VARIANT_RUN_COMMAND, VARIANT_GREP, VARIANT_VIEW_FILE, VARIANT_LIST_DIRECTORY, VARIANT_FIND_FILES, VARIANT_MCP_CALL, VARIANT_CODE_ACTION, VARIANT_TODO_LIST})


def _apply_options(messages: list[TextMessage]) -> list[TextMessage]:
    """Drop thinking and tool calls that the current ConversionOptions switch off. An assistant turn
    left with nothing (no text, thinking, or tool call) is dropped too."""
    options = current_options()
    if options.include_thinking and options.include_tool_calls:
        return messages
    shaped: list[TextMessage] = []
    for message in messages:
        changes: dict[str, object] = {}
        if not options.include_thinking and message.thinking:
            changes["thinking"] = None
        if not options.include_tool_calls and message.tool_calls:
            changes["tool_calls"] = ()
        if changes:
            message = replace(message, **changes)
        if message.role == "assistant" and not (message.text or message.thinking or message.tool_calls):
            continue
        shaped.append(message)
    return shaped


def _shaped(extract: Callable[[MessageExtractor, NativeSession], list[TextMessage]]) -> Callable[[MessageExtractor, NativeSession], list[TextMessage]]:
    """Apply the conversion options to an extractor's result."""

    @functools.wraps(extract)
    def wrapper(self: MessageExtractor, session: NativeSession) -> list[TextMessage]:
        return _apply_options(extract(self, session))

    return wrapper


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

    @_shaped
    def from_codex(self, session: NativeSession) -> list[TextMessage]:
        messages: list[TextMessage] = []
        outputs: dict[str, tuple[str, bool]] = {}
        pending_thinking: list[str] = []
        pending_calls: list[ToolCall] = []
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
            if payload is None:
                continue
            ptype = payload.get("type")
            timestamp = string_value(record, "timestamp") or session.timestamp
            if ptype == "reasoning":
                thought = self._block_thinking(payload.get("summary"), ("summary_text",))
                if thought:
                    if pending_thinking and not pending_calls:
                        # A new reasoning item with no tool call in between starts a new response.
                        messages.append(TextMessage("assistant", "", timestamp, thinking="\n\n".join(pending_thinking)))
                        pending_thinking = []
                    pending_thinking.append(thought)
                continue
            if ptype in ("function_call", "custom_tool_call"):
                pending_calls.append(self._codex_call(payload, ptype))
                continue
            if ptype in ("function_call_output", "custom_tool_call_output"):
                outputs[string_value(payload, "call_id") or ""] = (self._result_text(payload.get("output")), False)
                continue
            if ptype != "message":
                continue
            msg = self._extract_codex_message(payload, timestamp)
            if msg is None:
                continue
            if msg.role == "assistant":
                msg = replace(msg, thinking="\n\n".join(pending_thinking) or None, tool_calls=tuple(pending_calls))
                pending_thinking, pending_calls = [], []
            elif pending_calls or pending_thinking:
                # Tool calls or reasoning answered by no assistant text (the turn ended on the tools).
                messages.append(TextMessage("assistant", "", timestamp, thinking="\n\n".join(pending_thinking) or None, tool_calls=tuple(pending_calls)))
                pending_thinking, pending_calls = [], []
            messages.append(msg)
        if pending_calls or pending_thinking:
            messages.append(TextMessage("assistant", "", session.timestamp, thinking="\n\n".join(pending_thinking) or None, tool_calls=tuple(pending_calls)))
        return self._with_outputs(messages, outputs)

    @staticmethod
    def _codex_call(payload: JsonObject, kind: str) -> ToolCall:
        """``function_call`` keeps its arguments as a JSON string. ``custom_tool_call`` keeps raw input text."""
        raw = payload.get("arguments") if kind == "function_call" else payload.get("input")
        arguments: JsonObject
        if isinstance(raw, dict):
            arguments = raw
        elif kind == "function_call" and isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = None
            arguments = parsed if isinstance(parsed, dict) else {"raw": raw}
        elif raw is not None:
            arguments = {"input": raw}
        else:
            arguments = {}
        return ToolCall(id=str(payload.get("call_id") or ""), name=str(payload.get("name") or ""), arguments=arguments)

    @_shaped
    def from_pi(self, session: NativeSession) -> list[TextMessage]:
        messages: list[TextMessage] = []
        outputs: dict[str, tuple[str, bool]] = {}
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
            content = message.get("content")
            if role == "toolResult":
                outputs[string_value(message, "toolCallId") or ""] = (self._result_text(content), bool(message.get("isError")))
                continue
            if role not in {"user", "assistant", "system"}:
                continue
            text = self._content_text(content)
            tool_calls = self._block_tool_calls(content, ("toolCall",))
            if not text and not tool_calls:
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
                    thinking=self._block_thinking(content),
                    tool_calls=tool_calls,
                )
            )
        return self._with_outputs(messages, outputs)

    @_shaped
    def from_omp(self, session: NativeSession) -> list[TextMessage]:
        """Extract text history from an OMP session.

        An OMP session is a tree: each entry names its parent, and rewinding a conversation
        starts a new branch beside the old one in the same file. Only the active branch is read,
        from the root to omp's leaf. User and assistant text is kept and compaction summaries
        become compaction messages. A ``/skill:`` request or a collaborator's prompt is stored
        as a user-attributed ``custom_message`` and is kept as a user message, as omp counts it.
        Injected text is contextual: ``developer`` messages (system reminders), ``synthetic``
        user messages, other ``custom_message`` entries, and summaries of abandoned branches.
        A ``reset_boundary`` (``/clear``) takes everything before it out of omp's context, so
        those messages turn contextual too. Thinking, tool calls, tool results, shell runs and
        file mentions are skipped, matching the text-history contract of every other provider.
        """
        messages: list[TextMessage] = []
        current_provider: str | None = None
        current_model: str | None = None
        for record in self._omp_active_branch(session.records):
            rtype = record.get("type")
            timestamp = string_value(record, "timestamp") or session.timestamp
            if rtype == "model_change":
                provider, model = self._omp_model_change(record)
                current_provider = provider or current_provider
                current_model = model or current_model
            elif rtype == "compaction":
                summary = string_value(record, "summary") or ""
                if summary:
                    messages.append(TextMessage("user", summary, timestamp, is_compaction=True))
            elif rtype == "reset_boundary":
                messages = [replace(message, is_contextual=True) for message in messages]
            elif rtype == "custom_message":
                text = self._content_text(record.get("content"))
                user_turn = record.get("attribution") == "user" and record.get("customType") in self._OMP_USER_PROMPT_TYPES
                if text:
                    messages.append(TextMessage("user", text, timestamp, is_contextual=not user_turn))
            elif rtype == "branch_summary":
                text = string_value(record, "summary") or ""
                if text:
                    messages.append(TextMessage("user", text, timestamp, is_contextual=True))
            elif rtype == "message":
                message = as_object(record.get("message"))
                if message is None:
                    continue
                role = string_value(message, "role")
                if role not in ("user", "developer", "assistant"):
                    continue
                text = self._content_text(message.get("content"))
                if not text:
                    continue
                if role == "assistant":
                    messages.append(
                        TextMessage(
                            "assistant",
                            text,
                            timestamp,
                            model=string_value(message, "model") or current_model,
                            provider=string_value(message, "provider") or current_provider,
                            api=string_value(message, "api"),
                        )
                    )
                else:
                    contextual = role == "developer" or message.get("synthetic") is True or self._is_contextual(text)
                    messages.append(TextMessage("user", text, timestamp, is_contextual=contextual))
        return messages

    # Custom messages that start a user turn in omp (``isUserTurnInitiator``).
    _OMP_USER_PROMPT_TYPES = ("skill-prompt", "collab-prompt")

    @staticmethod
    def _omp_active_branch(records: list[JsonObject]) -> list[JsonObject]:
        """The entries on an OMP session's active branch, root first.

        Follows omp's ``resolveSessionLeafId``: the leaf is the last entry, unless that entry is
        a ``model_usage`` record whose ``activeLeafId`` names another one; ``null`` there means
        the session was cleared, so nothing is on the branch. A leaf that is not in the file
        falls back to the newest entry that selects itself. Files without entry ids predate the
        tree format and are read in file order.
        """
        entries = [record for record in records if record.get("type") not in ("title", "session")]
        by_id = {entry_id: entry for entry in entries if isinstance(entry_id := entry.get("id"), str)}
        if not entries or not by_id:
            return entries

        def leaf_of(entry: JsonObject) -> object:
            if entry.get("type") == "model_usage" and "activeLeafId" in entry:
                return entry.get("activeLeafId")
            return entry.get("id")

        last = entries[-1]
        leaf = leaf_of(last)
        if leaf is None and last.get("type") == "model_usage":
            return []
        if not isinstance(leaf, str) or leaf not in by_id:
            leaf = next((entry["id"] for entry in reversed(entries) if isinstance(entry.get("id"), str) and leaf_of(entry) == entry["id"]), None)
        branch: list[JsonObject] = []
        visited: set[str] = set()
        current = by_id.get(leaf) if isinstance(leaf, str) else None
        while current is not None:
            entry_id = str(current.get("id"))
            if entry_id in visited:  # a parent cycle would otherwise loop forever
                break
            visited.add(entry_id)
            branch.append(current)
            parent = current.get("parentId")
            current = by_id.get(parent) if isinstance(parent, str) else None
        branch.reverse()
        return branch

    @staticmethod
    def _omp_model_change(record: JsonObject) -> tuple[str | None, str | None]:
        """Provider and model id from a ``model_change`` entry for the default model. omp writes
        ``model: "provider/id"``; files that came over from Pi use ``provider`` and ``modelId``.
        Changes for another role (``smol``, ``plan``, ...) do not change the chat model."""
        if string_value(record, "role") not in (None, "default"):
            return None, None
        model = string_value(record, "model")
        if model and "/" in model:
            provider, _, model_id = model.partition("/")
            return provider or None, model_id or None
        return string_value(record, "provider"), string_value(record, "modelId") or model

    @_shaped
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
            tool_calls = self._opencode_tool_calls(parts)
            if not text and not tool_calls:
                continue
            extracted.append(
                TextMessage(
                    role,
                    text,
                    timestamp,
                    model=string_value(info, "modelID") or self._nested_model_value(info, "modelID"),
                    provider=string_value(info, "providerID") or self._nested_model_value(info, "providerID"),
                    thinking=self._block_thinking(parts, ("reasoning",)),
                    tool_calls=tool_calls,
                )
            )
        return extracted

    @staticmethod
    def _opencode_tool_calls(parts: list[object]) -> tuple[ToolCall, ...]:
        """OpenCode ``tool`` parts: ``callID``, ``tool``, and ``state`` with input, output and status."""
        calls: list[ToolCall] = []
        for part in parts:
            obj = as_object(part)
            if obj is None or obj.get("type") != "tool":
                continue
            state = as_object(obj.get("state")) or {}
            output = state.get("output")
            arguments = as_object(state.get("input")) or {}
            is_error = state.get("status") == "error"
            if is_error and output is None:
                output = state.get("error")
            calls.append(
                ToolCall(
                    id=str(obj.get("callID") or ""),
                    name=str(obj.get("tool") or ""),
                    arguments=arguments,
                    output=output if isinstance(output, str) else None,
                    is_error=is_error,
                )
            )
        return tuple(calls)

    @_shaped
    def from_claude(self, session: NativeSession) -> list[TextMessage]:
        messages: list[TextMessage] = []
        outputs: dict[str, tuple[str, bool]] = {}
        last_message_id: str | None = None
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
            content = message.get("content")
            for block in as_list(content) or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    outputs[str(block.get("tool_use_id") or "")] = (self._result_text(block.get("content")), block.get("is_error") is True)
            text = self._content_text(content)
            thinking = self._block_thinking(content)
            tool_calls = self._block_tool_calls(content)
            if not text and not tool_calls and not thinking:
                continue
            if role == "user" and not text:
                continue
            timestamp = string_value(record, "timestamp") or session.timestamp
            model = string_value(message, "model")
            if record.get("isCompactSummary") is True:
                messages.append(TextMessage("user", text, timestamp, is_compaction=True))
                continue
            # Claude Code writes one JSONL line per content block, and they share message.id.
            # Lines of the same assistant turn are merged back into one message.
            message_id = string_value(message, "id")
            if role == "assistant" and message_id and message_id == last_message_id and messages and messages[-1].role == "assistant":
                previous = messages[-1]
                messages[-1] = replace(
                    previous,
                    text="\n\n".join(part for part in (previous.text, text) if part),
                    thinking="\n\n".join(part for part in (previous.thinking, thinking) if part) or None,
                    tool_calls=previous.tool_calls + tool_calls,
                )
                continue
            last_message_id = message_id if role == "assistant" else None
            messages.append(TextMessage(role, text, timestamp, model=model, thinking=thinking, tool_calls=tool_calls))
        return self._with_outputs(messages, outputs)

    @_shaped
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
                tool_calls = self._devin_tool_calls(step_obj)
                thinking = string_value(step_obj, "reasoning_content") or None
                if not text and not tool_calls:
                    continue
                extra = as_object(step_obj.get("extra")) or {}
                gen_model = string_value(extra, "generation_model") or model_name
                messages.append(TextMessage("assistant", text, timestamp, model=gen_model, provider="devin", thinking=thinking, tool_calls=tool_calls))
        return self._with_outputs(messages, self._devin_outputs(steps))

    @staticmethod
    def _devin_tool_calls(step: JsonObject) -> tuple[ToolCall, ...]:
        """ATIF ``tool_calls`` on an agent step: ``tool_call_id``, ``function_name``, ``arguments``."""
        calls: list[ToolCall] = []
        for call in as_list(step.get("tool_calls")) or []:
            obj = as_object(call)
            if obj is None:
                continue
            arguments = as_object(obj.get("arguments")) or {}
            calls.append(ToolCall(id=str(obj.get("tool_call_id") or ""), name=str(obj.get("function_name") or ""), arguments=arguments))
        return tuple(calls)

    @classmethod
    def _devin_outputs(cls, steps: list[object]) -> dict[str, tuple[str, bool]]:
        """Observations hold results keyed by ``source_call_id``. Devin reports no error flag."""
        outputs: dict[str, tuple[str, bool]] = {}
        for step in steps:
            step_obj = as_object(step)
            observation = as_object((step_obj or {}).get("observation")) or {}
            for result in as_list(observation.get("results")) or []:
                obj = as_object(result)
                if obj is None:
                    continue
                outputs[str(obj.get("source_call_id") or "")] = (cls._result_text(obj.get("content")), False)
        return outputs

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

    @_shaped
    def from_factory(self, session: NativeSession) -> list[TextMessage]:
        messages: list[TextMessage] = []
        outputs: dict[str, tuple[str, bool]] = {}
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
            for block in as_list(content) or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    outputs[str(block.get("tool_use_id") or "")] = (self._result_text(block.get("content")), block.get("is_error") is True)
            text = sequence_to_text(content) if isinstance(content, list) else (string_value(msg, "content") or "")
            tool_calls = self._block_tool_calls(content) if role == "assistant" else ()
            if not text and not tool_calls:
                continue
            if role == "user":
                contextual = self._is_contextual(text)
                messages.append(TextMessage("user", text, timestamp, is_contextual=contextual))
            elif role == "assistant":
                messages.append(
                    TextMessage("assistant", text, timestamp, provider="factory", thinking=self._block_thinking(content), tool_calls=tool_calls)
                )
            elif role == "system":
                messages.append(TextMessage("user", text, timestamp, is_contextual=True))
        return self._with_outputs(messages, outputs)

    @staticmethod
    def _first_path_in(buf: bytes, depth: int = 0) -> str:
        """First file path found in a nested message. Cascade keeps paths in nested PathScopeItem
        messages (``absolute_uri`` at field 5, ``absolute_path_migrate_me_to_uri`` at field 1), so this
        looks for a ``file://`` URI or an absolute Windows or POSIX path in any string field."""
        from session_sdk.windsurf_pb import iter_fields
        if depth > 6:
            return ""
        for _fno, wtype, _off, value in iter_fields(buf):
            if wtype != 2 or not isinstance(value, (bytes, bytearray)):
                continue
            try:
                text = bytes(value).decode("utf-8")
            except UnicodeDecodeError:
                text = None
            if text is not None and (text.startswith("file://") or re.match(r"^[A-Za-z]:[\\/]", text) or text.startswith("/")):
                return text
            try:
                found = MessageExtractor._first_path_in(bytes(value), depth + 1)
            except ValueError:
                found = ""
            if found:
                return found
        return ""

    @staticmethod
    def _windsurf_code_action_call(data: bytes, index: int) -> ToolCall:
        """CortexStepCodeAction: ``action_spec`` (1) holds an ActionSpecCommand (1) or ActionSpecCreateFile
        (2) with the instruction. The target file is nested in it. ``code_instruction`` (8) is the fallback."""
        from session_sdk.windsurf_pb import read_string_field
        spec = None
        for fno, wtype, _off, value in iter_fields(data):
            if fno == 1 and wtype == 2 and isinstance(value, (bytes, bytearray)):
                spec = bytes(value)
        instruction = ""
        path = ""
        if spec is not None:
            for fno, wtype, _off, value in iter_fields(spec):
                if fno in (1, 2) and wtype == 2 and isinstance(value, (bytes, bytearray)):
                    instruction = read_string_field(bytes(value), 1) or instruction
            path = MessageExtractor._first_path_in(spec)
        instruction = instruction or read_string_field(data, 8) or ""
        return ToolCall(
            id=f"windsurf-step-{index}",
            name="edit_file",
            arguments={k: v for k, v in (("path", path), ("instruction", instruction)) if v},
            output=None,
        )

    @staticmethod
    def _windsurf_todo_call(data: bytes, index: int) -> ToolCall:
        """CortexStepTodoList: repeated CortexTodoListItem (1): id (1), content (2), status (3), priority (4)."""
        from session_sdk.windsurf_pb import read_string_field
        todos: list[JsonObject] = []
        for fno, wtype, _off, value in iter_fields(data):
            if fno == 1 and wtype == 2 and isinstance(value, (bytes, bytearray)):
                status = next((v for f, w, _o, v in iter_fields(bytes(value)) if f == 3 and w == 0), None)
                todos.append({
                    "id": read_string_field(bytes(value), 1) or "",
                    "content": read_string_field(bytes(value), 2) or "",
                    "status": status if isinstance(status, int) else None,
                })
        return ToolCall(id=f"windsurf-step-{index}", name="todo_list", arguments={"todos": todos}, output="")

    @staticmethod
    def _attach_windsurf_error(messages: list[TextMessage], error: str) -> None:
        """A step error belongs to the tool call that just ran. A call with no result takes it as its result."""
        if messages and messages[-1].role == "assistant":
            calls = list(messages[-1].tool_calls)
            for position in range(len(calls) - 1, -1, -1):
                if calls[position].output is None:
                    calls[position] = replace(calls[position], output=error, is_error=True)
                    messages[-1] = replace(messages[-1], tool_calls=tuple(calls))
                    return

    @staticmethod
    def _windsurf_tool_call(variant: int, data: object, index: int, command_outputs: dict[str, str]) -> ToolCall | None:
        """Map one Windsurf tool step to a ToolCall. ``data`` holds the step's variant bytes."""
        from session_sdk.windsurf_pb import iter_fields, read_all_string_fields, read_string_field
        if not isinstance(data, (bytes, bytearray)):
            return None
        call_id = f"windsurf-step-{index}"
        if variant == VARIANT_RUN_COMMAND:
            command_id = read_string_field(data, 13) or ""
            output = read_string_field(data, 24)
            if output is None and command_id:
                output = command_outputs.get(command_id)
            return ToolCall(
                id=command_id or call_id,
                name="run_command",
                arguments={"command": read_string_field(data, 23) or ""},
                output=output,
            )
        if variant == VARIANT_GREP:
            fields = (("query", read_string_field(data, 1)), ("glob", read_string_field(data, 2)), ("path", read_string_field(data, 11)))
            return ToolCall(id=call_id, name="grep_search", arguments={k: v for k, v in fields if v}, output=read_string_field(data, 3))
        if variant == VARIANT_VIEW_FILE:
            return ToolCall(id=call_id, name="view_file", arguments={"uri": read_string_field(data, 1) or ""}, output=read_string_field(data, 4))
        if variant == VARIANT_LIST_DIRECTORY:
            lines: list[str] = []
            for fno, wtype, _off, entry in iter_fields(data):
                if fno == 3 and wtype == 2 and isinstance(entry, (bytes, bytearray)):
                    is_dir = any(f2 == 2 and w2 == 0 and v2 == 1 for f2, w2, _o2, v2 in iter_fields(entry))
                    lines.append((read_string_field(entry, 1) or "") + ("/" if is_dir else ""))
            return ToolCall(id=call_id, name="list_directory", arguments={"uri": read_string_field(data, 1) or ""}, output="\n".join(lines))
        if variant == VARIANT_FIND_FILES:
            return ToolCall(
                id=call_id,
                name="find_files",
                arguments={"pattern": read_string_field(data, 1) or ""},
                output="\n".join(read_all_string_fields(data, 11)),
            )
        if variant == VARIANT_MCP_CALL:
            nested = next((v for fno, wtype, _off, v in iter_fields(data) if fno == 2 and wtype == 2 and isinstance(v, (bytes, bytearray))), None)
            if nested is None:
                return None
            raw_args = read_string_field(nested, 3) or ""
            try:
                parsed = json.loads(raw_args) if raw_args else {}
            except ValueError:
                parsed = None
            arguments = parsed if isinstance(parsed, dict) else {"raw": raw_args}
            return ToolCall(
                id=read_string_field(nested, 1) or call_id,
                name=read_string_field(nested, 2) or "",
                arguments=arguments,
                output=read_string_field(data, 3),
            )
        if variant == VARIANT_CODE_ACTION:
            return MessageExtractor._windsurf_code_action_call(bytes(data), index)
        if variant == VARIANT_TODO_LIST:
            return MessageExtractor._windsurf_todo_call(bytes(data), index)
        return None

    @_shaped
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
            VARIANT_RUN_COMMAND,
            VARIANT_COMMAND_RESULT,
            VARIANT_GREP,
            VARIANT_VIEW_FILE,
            VARIANT_LIST_DIRECTORY,
            VARIANT_FIND_FILES,
            VARIANT_MCP_CALL,
        )
        from session_sdk.paths import epoch_ms_to_iso
        traj = parse_trajectory(plaintext)
        steps = traj.get("steps") or []
        # A command's output can arrive in a later command-result step, joined by command id.
        command_outputs: dict[str, str] = {}
        for step_buf in steps:
            if not isinstance(step_buf, (bytes, bytearray)):
                continue
            step = parse_step(step_buf)
            result_data = step["variant_data"]
            if step["variant_field"] == VARIANT_COMMAND_RESULT and isinstance(result_data, (bytes, bytearray)):
                command_id = read_string_field(result_data, 1)
                output = read_string_field(result_data, 9)
                if command_id and output is not None:
                    command_outputs[command_id] = output
        messages: list[TextMessage] = []
        for index, step_buf in enumerate(steps):
            if not isinstance(step_buf, (bytes, bytearray)):
                continue
            step = parse_step(step_buf)
            step_error = parse_step_error(step_buf)
            if step_error:
                self._attach_windsurf_error(messages, step_error)
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
                    messages.append(TextMessage("assistant", visible, timestamp, provider="windsurf", thinking=internal or None))
                elif internal:
                    # Reasoning with no visible reply stays reasoning, not reply text.
                    messages.append(TextMessage("assistant", "", timestamp, provider="windsurf", thinking=internal))
            elif vf == VARIANT_ERROR_MESSAGE:
                details = error_details_text(bytes(vdata)) if isinstance(vdata, (bytes, bytearray)) else None
                if details:
                    self._attach_windsurf_error(messages, details)
            elif vf in _WINDSURF_TOOL_VARIANTS:
                # Tool steps follow the planner turn that asked for them. The call and its result share the step.
                call = self._windsurf_tool_call(vf, vdata, index, command_outputs)
                if call is not None:
                    if messages and messages[-1].role == "assistant":
                        messages[-1] = replace(messages[-1], tool_calls=messages[-1].tool_calls + (call,))
                    else:
                        messages.append(TextMessage("assistant", "", timestamp, provider="windsurf", tool_calls=(call,)))
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

    @staticmethod
    def _typed_text(parts: list[object]) -> str:
        """Text of ``text`` parts only. ``sequence_to_text`` would also take reasoning text."""
        texts = [str(part.get("text") or "") for part in parts if isinstance(part, dict) and part.get("type") == "text" and part.get("text")]
        return "\n".join(texts)

    @staticmethod
    def _grok_output(output: object) -> tuple[str, bool]:
        """Tool output in the AI SDK shape used by grok-cli: ``{type: json, value: {success, output, error}}``,
        ``{type: text | error-text, value}``, or a bare result object."""
        if isinstance(output, dict) and output.get("type") in ("json", "text", "error-text") and "value" in output:
            value = output.get("value")
            if output.get("type") == "error-text":
                return (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)), True
            if isinstance(value, dict) and "success" in value:
                text = value.get("output") if value.get("output") is not None else value.get("error")
                return (str(text) if text is not None else ""), value.get("success") is not True
            if isinstance(value, str):
                return value, False
            return json.dumps(value, ensure_ascii=False), False
        if isinstance(output, dict) and "success" in output:
            text = output.get("output") if output.get("output") is not None else output.get("error")
            return (str(text) if text is not None else ""), output.get("success") is not True
        if isinstance(output, str):
            return output, False
        return (json.dumps(output, ensure_ascii=False) if output is not None else ""), False

    def _from_grok_messages(self, session: NativeSession) -> list[TextMessage]:
        """grok-cli messages: AI SDK ModelMessage records. Assistant content holds ``reasoning``, ``text``,
        and ``tool-call`` parts. Tool results come in later ``tool`` messages, matched by toolCallId."""
        messages: list[TextMessage] = []
        outputs: dict[str, tuple[str, bool]] = {}
        for record in session.records:
            role = string_value(record, "role") or ""
            timestamp = string_value(record, "created_at") or session.timestamp
            content = record.get("content")
            parts = as_list(content) or []
            if role == "tool":
                for part in parts:
                    obj = as_object(part)
                    if obj is not None and obj.get("type") == "tool-result":
                        outputs[string_value(obj, "toolCallId") or ""] = self._grok_output(obj.get("output"))
                continue
            if role == "user":
                text = content if isinstance(content, str) else self._typed_text(parts)
                if text:
                    messages.append(TextMessage("user", text, timestamp, is_contextual=self._is_contextual(text)))
                continue
            if role != "assistant":
                continue
            text = content if isinstance(content, str) else self._typed_text(parts)
            thinking = self._block_thinking(parts, ("reasoning",))
            calls: list[ToolCall] = []
            for part in parts:
                obj = as_object(part)
                if obj is None or obj.get("type") != "tool-call":
                    continue
                arguments = as_object(obj.get("input")) or {}
                calls.append(ToolCall(id=string_value(obj, "toolCallId") or "", name=string_value(obj, "toolName") or "", arguments=arguments))
            if not text and not thinking and not calls:
                continue
            messages.append(TextMessage("assistant", text, timestamp, provider="grok", thinking=thinking, tool_calls=tuple(calls)))
        return self._with_outputs(messages, outputs)

    @_shaped
    def from_grok(self, session: NativeSession) -> list[TextMessage]:
        """Extract text history from a Grok session (Grok Build or grok-cli)."""
        if any(isinstance(record, dict) and "role" in record for record in session.records):
            return self._from_grok_messages(session)
        return self._from_grok_envelopes(session)

    def _from_grok_envelopes(self, session: NativeSession) -> list[TextMessage]:
        """Extract text history from a Grok Build session.

        Grok stores sessions as ``updates.jsonl``: one ACP session update envelope per line,
        ``{"timestamp", "method", "params": {"sessionId", "update"}}``.

        ``user_message_chunk`` and ``agent_message_chunk`` carry text, and ``messageId`` groups the
        streamed chunks of one message. ``agent_thought_chunk`` is the model's reasoning for the same
        ``messageId``. ``tool_call`` starts a tool invocation (``toolCallId``, ``title``, ``rawInput``),
        and ``tool_call_update`` reports its result (``rawOutput`` or content text, and a ``failed``
        status for errors). A tool call joins the agent message that was streaming when it started.
        """
        messages: list[TextMessage] = []
        outputs: dict[str, tuple[str, bool]] = {}
        pending: dict[str, dict[str, object]] = {}
        current_agent: str | None = None
        anonymous = 0

        def entry(key: str, role: str, timestamp: str) -> dict[str, object]:
            return pending.setdefault(key, {"role": role, "text": "", "thinking": "", "timestamp": timestamp, "calls": []})

        def flush(key: str) -> None:
            item = pending.pop(key, None)
            if item is None:
                return
            text = str(item["text"])
            thinking = str(item["thinking"]) or None
            calls = tuple(item["calls"])  # type: ignore[arg-type]
            if not text and not thinking and not calls:
                return
            timestamp = str(item["timestamp"])
            if item["role"] == "assistant":
                messages.append(TextMessage("assistant", text, timestamp, provider="grok", thinking=thinking, tool_calls=calls))
            else:
                messages.append(TextMessage("user", text, timestamp, is_contextual=self._is_contextual(text)))

        for record in session.records:
            if not isinstance(record, dict):
                continue
            method = string_value(record, "method") or ""
            if method not in ("session/update", "_x.ai/session/update"):
                continue
            params = as_object(record.get("params")) or {}
            update = as_object(params.get("update")) or {}
            utype = string_value(update, "sessionUpdate") or ""
            timestamp = str(record.get("timestamp") or "") or session.timestamp

            if utype == "tool_call":
                call_id = string_value(update, "toolCallId") or ""
                call = ToolCall(
                    id=call_id,
                    name=string_value(update, "title") or string_value(update, "kind") or "",
                    arguments=as_object(update.get("rawInput")) or {},
                )
                key = current_agent or f"tool-{call_id}"
                entry(key, "assistant", timestamp)["calls"].append(call)  # type: ignore[union-attr]
                continue
            if utype == "tool_call_update":
                status = string_value(update, "status") or ""
                if status not in ("completed", "failed"):
                    continue
                call_id = string_value(update, "toolCallId") or ""
                raw = update.get("rawOutput")
                nested = [as_object(block.get("content")) or {} for block in as_list(update.get("content")) or [] if isinstance(block, dict)]
                text = self._result_text(raw) if raw is not None else "\n".join(str(part.get("text") or "") for part in nested if part.get("text"))
                outputs[call_id] = (text, status == "failed")
                continue

            if utype not in ("user_message_chunk", "agent_message_chunk", "agent_thought_chunk"):
                # plan, session_info_update, usage_update, etc. are not part of the history.
                continue
            content = as_object(update.get("content")) or {}
            if string_value(content, "type") != "text":
                continue
            text = string_value(content, "text") or ""
            if not text:
                continue
            message_id = string_value(update, "messageId") or ""
            if not message_id:
                anonymous += 1
                message_id = f"anonymous-{anonymous}"
            if utype == "user_message_chunk":
                entry(message_id, "user", timestamp)["text"] = str(entry(message_id, "user", timestamp)["text"]) + text
                continue
            current_agent = message_id
            item = entry(message_id, "assistant", timestamp)
            field = "thinking" if utype == "agent_thought_chunk" else "text"
            item[field] = str(item[field]) + text

        for key in list(pending.keys()):
            flush(key)
        return self._with_outputs(messages, outputs)

    @_shaped
    def from_freebuff(self, session: NativeSession) -> list[TextMessage]:
        """Extract text history from a Freebuff Desktop session.

        Freebuff stores messages in per-project SQLite: each record carries
        ``role`` + ``parts`` (parsed parts_json) + ``ts`` (epoch ms).  Only
        ``{"kind": "text"}`` parts are chat text; ``reasoning`` (thinking),
        ``tool`` (invocations), ``changes`` (diffs) and ``ad`` parts are
        skipped, matching the text-history contract of every other provider.
        """
        messages: list[TextMessage] = []
        from session_sdk.paths import epoch_ms_to_iso
        for record in session.records:
            if not isinstance(record, dict) or "role" not in record:
                continue
            role = str(record.get("role") or "")
            if role not in ("user", "assistant"):
                continue
            parts = record.get("parts")
            if not isinstance(parts, list):
                continue
            text_parts = [p for p in parts if isinstance(p, dict) and p.get("kind") == "text"]
            text = "\n\n".join(str(p.get("text") or "") for p in text_parts if p.get("text"))
            tool_calls = self._freebuff_tool_calls(parts) if role == "assistant" else ()
            if not text and not tool_calls:
                continue
            ts = record.get("ts")
            timestamp = epoch_ms_to_iso(int(ts)) if isinstance(ts, (int, float)) and ts else session.timestamp
            if role == "assistant":
                # Reasoning parts are separate thoughts (each has its own id), so they are joined, not merged.
                reasoning_parts = [p for p in parts if isinstance(p, dict) and p.get("kind") == "reasoning"]
                thinking = "\n\n".join(str(p.get("text") or "") for p in reasoning_parts if p.get("text")) or None
                messages.append(TextMessage("assistant", text, timestamp, provider="freebuff", thinking=thinking, tool_calls=tool_calls))
            else:
                contextual = self._is_contextual(text)
                messages.append(TextMessage("user", text, timestamp, is_contextual=contextual))
        return messages

    def from_session(self, session: NativeSession) -> list[TextMessage]:
        """Extract text history from a session of any provider, dispatching on its provider."""
        extractors: dict[str, Callable[[NativeSession], list[TextMessage]]] = {
            "codex": self.from_codex,
            "pi": self.from_pi,
            "omp": self.from_omp,
            "opencode": self.from_opencode,
            "claude": self.from_claude,
            "devin": self.from_devin,
            "factory": self.from_factory,
            "windsurf": self.from_windsurf,
            "grok": self.from_grok,
            "freebuff": self.from_freebuff,
            "t3": self.from_t3,
        }
        extractor = extractors.get(session.provider)
        return extractor(session) if extractor is not None else []

    @_shaped
    def from_t3(self, session: NativeSession) -> list[TextMessage]:
        """Extract text history from a T3 Code thread.

        T3 stores one row per message with role, text and an ISO timestamp. Only
        user and assistant rows carry the conversation. The thread's model selection
        rides on every record, so assistant messages keep their provider and model.
        """
        messages: list[TextMessage] = []
        for record in session.records:
            role = str(record.get("role") or "")
            text = str(record.get("text") or "")
            if role not in ("user", "assistant") or not text:
                continue
            timestamp = str(record.get("created_at") or session.timestamp)
            if role == "assistant":
                provider = str(record.get("instance_id") or "") or None
                model = str(record.get("model") or "") or None
                messages.append(TextMessage("assistant", text, timestamp, model=model, provider=provider))
            else:
                messages.append(TextMessage("user", text, timestamp, is_contextual=self._is_contextual(text)))
        return messages

    @staticmethod
    def _freebuff_tool_calls(parts: list[object]) -> tuple[ToolCall, ...]:
        """Freebuff ``tool`` parts: ``toolName``, ``input`` (object), ``output`` (text, may be missing),
        and ``status`` (success, failure, running, or missing). A failure is a failed status or a
        non-zero ``exitCode``."""
        calls: list[ToolCall] = []
        for part in parts:
            if not isinstance(part, dict) or part.get("kind") != "tool":
                continue
            output = part.get("output")
            exit_code = part.get("exitCode")
            calls.append(
                ToolCall(
                    id=str(part.get("id") or ""),
                    name=str(part.get("toolName") or ""),
                    arguments=part.get("input") if isinstance(part.get("input"), dict) else {},
                    output=output if isinstance(output, str) else None,
                    is_error=part.get("status") == "failure" or (isinstance(exit_code, int) and exit_code != 0),
                )
            )
        return tuple(calls)

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
    def _block_thinking(blocks: object, kinds: tuple[str, ...] = ("thinking",)) -> str | None:
        """Thinking text from content blocks. Several blocks are separate thoughts, joined by blank lines."""
        items = as_list(blocks) or []
        texts = [str(b.get("thinking") or b.get("text") or "") for b in items if isinstance(b, dict) and b.get("type") in kinds]
        return "\n\n".join(t for t in texts if t) or None

    @staticmethod
    def _block_tool_calls(blocks: object, kinds: tuple[str, ...] = ("tool_use",)) -> tuple[ToolCall, ...]:
        """Tool invocations from content blocks. Pi uses ``toolCall`` with ``arguments``, Claude and
        Factory use ``tool_use`` with ``input``."""
        calls: list[ToolCall] = []
        for block in as_list(blocks) or []:
            if not isinstance(block, dict) or block.get("type") not in kinds:
                continue
            arguments = block.get("arguments", block.get("input"))
            calls.append(
                ToolCall(
                    id=str(block.get("id") or ""),
                    name=str(block.get("name") or ""),
                    arguments=arguments if isinstance(arguments, dict) else {},
                )
            )
        return tuple(calls)

    @staticmethod
    def _result_text(value: object) -> str:
        """Text of a tool result. It is a string, or a list of text blocks, or other JSON."""
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "\n".join(str(b.get("text") or "") for b in value if isinstance(b, dict) and b.get("text"))
        if value is None:
            return ""
        return json.dumps(value, ensure_ascii=False)

    @staticmethod
    def _with_outputs(messages: list[TextMessage], outputs: dict[str, tuple[str, bool]]) -> list[TextMessage]:
        """Attach recorded tool results to the tool calls they answer (matched by call id)."""
        attached: list[TextMessage] = []
        for message in messages:
            if message.tool_calls:
                calls = tuple(
                    replace(call, output=outputs[call.id][0], is_error=outputs[call.id][1]) if call.id in outputs else call
                    for call in message.tool_calls
                )
                message = replace(message, tool_calls=calls)
            attached.append(message)
        return attached

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
            content: list[JsonObject] = []
            if message.thinking:
                # Same block shape Pi writes for its own reasoning (thinking + thinkingSignature).
                content.append({"type": "thinking", "thinking": message.thinking, "thinkingSignature": "reasoning_content"})
            if message.text:
                content.append({"type": "text", "text": message.text})
            # Pi's own shape for a tool invocation inside an assistant message.
            for call in message.tool_calls:
                content.append({"type": "toolCall", "id": call.id, "name": call.name, "arguments": call.arguments})
            message_payload: JsonObject = {
                "role": message.role,
                "content": content,
                "timestamp": self._epoch_ms(message.timestamp),
            }
            if message.role == "assistant":
                message_payload["api"] = message.api or "openai-completions"
                message_payload["provider"] = message.provider or "session-export"
                message_payload["model"] = message.model or "imported"
                message_payload["usage"] = self._usage(pending_input_tokens, message_tokens)
                message_payload["stopReason"] = "toolUse" if message.tool_calls else "stop"
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
            # Pi expects one toolResult message per toolCall, right after the assistant message.
            for call in message.tool_calls:
                result_id = _next_id()
                records.append(
                    {
                        "type": "message",
                        "id": result_id,
                        "parentId": parent_id,
                        "timestamp": message.timestamp,
                        "message": {
                            "role": "toolResult",
                            "toolCallId": call.id,
                            "toolName": call.name,
                            "content": [{"type": "text", "text": call.output if call.output is not None else "(no output recorded)"}],
                            "isError": call.is_error,
                            "timestamp": self._epoch_ms(message.timestamp),
                        },
                    }
                )
                parent_id = result_id
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


_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _omp_time(timestamp: str) -> datetime | None:
    """A source timestamp as an aware UTC datetime, or None when it does not parse. A timestamp
    without an offset is read as UTC."""
    if not timestamp:
        return None
    try:
        moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _omp_iso(moment: datetime) -> str:
    """omp's timestamp format, JavaScript's ``toISOString()``: UTC, milliseconds, ``Z``."""
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _omp_epoch_ms(moment: datetime) -> int:
    return (moment - _UNIX_EPOCH) // timedelta(milliseconds=1)


def _omp_session_timestamp(source: NativeSession) -> str:
    """The OMP header timestamp for a converted session, which also names its file. Falls back
    to the source file's modification time, so planning twice yields the same destination."""
    moment = _omp_time(source.timestamp)
    if moment is None:
        try:
            moment = datetime.fromtimestamp(source.path.stat().st_mtime, UTC)
        except OSError:
            moment = datetime.now(UTC)
    return _omp_iso(moment)


class OmpRecordBuilder:
    """Build an OMP session (see ``OmpStore``) from a TextMessage stream.

    Writes the fixed-width title slot, the ``session`` header, and a ``message`` entry for each
    user or assistant message, every entry the child of the one before. The title is the first
    line of the first user message. A compaction summary becomes a ``compaction`` entry that
    keeps nothing before it, so a resumed session starts from the summary, as after omp's own
    compactions. Contextual messages are dropped, as in every other builder.

    Assistant entries keep the source provider and model where known and otherwise name the
    ``session-export/imported`` placeholder; omp falls back to its configured model when the
    named one is unavailable. Token usage is zero: imported turns carry no usage data, and omp
    sizes the context from the stored conversation when none is reported.

    With ``source``, a final ``foreign_session_import`` custom entry records where the session
    came from, the entry omp's own Claude and Codex importers write. omp never sends custom
    entries to a model. Output is deterministic: the same input builds the same records.
    """

    def build(
        self,
        session_id: str,
        cwd: str,
        timestamp: str,
        messages: list[TextMessage],
        *,
        source: NativeSession | None = None,
    ) -> list[JsonObject]:
        session_time = _omp_time(timestamp) or datetime.now(UTC)
        session_iso = _omp_iso(session_time)
        title = self._title(messages)
        header: JsonObject = {"type": "session", "version": 3, "id": session_id, "timestamp": session_iso, "cwd": cwd}
        if title:
            header["title"] = title
            header["titleSource"] = "auto"
        records: list[JsonObject] = [omp_title_slot(title, session_iso, "auto" if title else None), header]

        estimator = TokenEstimator()
        context_tokens = 0
        parent_id: str | None = None
        last_iso = session_iso
        entry_count = 0
        for message in messages:
            if message.is_contextual:
                continue
            entry_count += 1
            entry_id = f"{entry_count:08x}"
            moment = _omp_time(message.timestamp) or session_time
            last_iso = _omp_iso(moment)
            if message.is_compaction:
                # firstKeptEntryId naming the compaction itself keeps no earlier entry.
                records.append({
                    "type": "compaction",
                    "id": entry_id,
                    "parentId": parent_id,
                    "timestamp": last_iso,
                    "summary": message.text,
                    "firstKeptEntryId": entry_id,
                    "tokensBefore": context_tokens,
                })
                context_tokens = estimator.count(message.text)
            else:
                payload: JsonObject = {"role": "assistant" if message.role == "assistant" else "user", "content": [{"type": "text", "text": message.text}]}
                if message.role == "assistant":
                    payload["api"] = message.api or "openai-completions"
                    payload["provider"] = message.provider or "session-export"
                    payload["model"] = message.model or "imported"
                    payload["usage"] = self._zero_usage()
                    payload["stopReason"] = "stop"
                else:
                    payload["attribution"] = "user"
                payload["timestamp"] = _omp_epoch_ms(moment)
                records.append({"type": "message", "id": entry_id, "parentId": parent_id, "timestamp": last_iso, "message": payload})
                context_tokens += estimator.count(message.text)
            parent_id = entry_id

        if source is not None:
            records.append({
                "type": "custom",
                "id": f"{entry_count + 1:08x}",
                "parentId": parent_id,
                "timestamp": last_iso,
                "customType": "foreign_session_import",
                "data": {
                    "source": source.provider,
                    "sourceId": source.session_id,
                    "sourcePath": str(source.path),
                    "sourceCwd": source.cwd,
                },
            })
        return records

    @staticmethod
    def _title(messages: list[TextMessage]) -> str:
        for message in messages:
            if message.role != "user" or message.is_contextual or message.is_compaction:
                continue
            for line in message.text.splitlines():
                words = line.split()
                if words:
                    return " ".join(words)[:80]
        return ""

    @staticmethod
    def _zero_usage() -> JsonObject:
        return {
            "input": 0,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0,
            "totalTokens": 0,
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
        }


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
            if message.thinking and message.role == "assistant":
                # Codex keeps reasoning as a response item with a summary list.
                records.append({
                    "timestamp": message.timestamp,
                    "type": "response_item",
                    "payload": {"type": "reasoning", "summary": [{"type": "summary_text", "text": message.thinking}]},
                })
            for call in message.tool_calls:
                records.append({
                    "timestamp": message.timestamp,
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                        "call_id": call.id,
                    },
                })
                records.append({
                    "timestamp": message.timestamp,
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": call.id,
                        "output": call.output if call.output is not None else "(no output recorded)",
                    },
                })
            content_type = "output_text" if message.role == "assistant" else "input_text"
            if message.text or not (message.tool_calls or message.thinking):
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
            parts: list[JsonObject] = []
            if message.thinking:
                parts.append(
                    {
                        "id": opencode_id("prt", message.timestamp),
                        "sessionID": session_id,
                        "messageID": message_id,
                        "type": "reasoning",
                        "text": message.thinking,
                        "time": {"start": created, "end": created},
                    }
                )
            if message.text or not (message.tool_calls or message.thinking):
                parts.append(
                    {
                        "id": part_id,
                        "sessionID": session_id,
                        "messageID": message_id,
                        "type": "text",
                        "text": message.text,
                        "time": {"start": created, "end": created},
                    }
                )
            for call in message.tool_calls:
                parts.append(self._tool_part(session_id, message_id, call, created))
            exported.append({"info": info, "parts": parts})
            if message.role == "user" or index == 0:
                parent_id = message_id
        return exported

    @staticmethod
    def _tool_part(session_id: str, message_id: str, call: ToolCall, created: int) -> JsonObject:
        """OpenCode ``tool`` part. A call with no recorded output is marked completed with a placeholder,
        so the session does not show it as still running."""
        span = {"start": created, "end": created}
        base: JsonObject = {
            "id": opencode_id("prt", epoch_ms_to_iso(created)),
            "sessionID": session_id,
            "messageID": message_id,
            "type": "tool",
            "callID": call.id,
            "tool": call.name,
        }
        if call.is_error:
            state: JsonObject = {"status": "error", "input": call.arguments, "error": call.output or "(failed)", "time": span}
        else:
            state = {
                "status": "completed",
                "input": call.arguments,
                "output": call.output if call.output is not None else "(no output recorded)",
                "title": call.name,
                "metadata": {},
                "time": span,
            }
        return {**base, "state": state}

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
                blocks: list[JsonObject] = []
                if message.thinking:
                    # Claude Code removes thinking blocks before it sends a request (see ANt in the
                    # bundle), so an unsigned block is kept for display and never sent back.
                    blocks.append({"type": "thinking", "thinking": message.thinking, "signature": ""})
                if message.text:
                    blocks.append({"type": "text", "text": message.text})
                blocks.extend({"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments} for call in message.tool_calls)
                msg_obj: JsonObject = {
                    "role": "assistant",
                    "content": blocks,
                    "model": message.model or "imported",
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                    "stop_reason": "tool_use" if message.tool_calls else "end_turn",
                }
            else:
                msg_obj = {"role": "user", "content": content}
            records.append({
                "type": message.role if message.role in ("user", "assistant") else "user",
                "message": msg_obj,
                "uuid": entry_uuid,
                "parentUuid": None if message.is_compaction else parent_uuid,
                "isCompactSummary": True if message.is_compaction else None,
                "isSidechain": False,
                "userType": "external",
                "entrypoint": "cli",
                "cwd": cwd,
                "sessionId": session_id,
                "timestamp": message.timestamp,
                "version": version,
            })
            parent_uuid = entry_uuid
            if message.role == "assistant" and message.tool_calls:
                # Claude Code answers all tool_use blocks of one turn in a single user entry.
                result_uuid = str(uuid4())
                records.append({
                    "type": "user",
                    "message": {"role": "user", "content": [self._tool_result(call) for call in message.tool_calls]},
                    "uuid": result_uuid,
                    "parentUuid": entry_uuid,
                    "sourceToolAssistantUUID": entry_uuid,
                    "isSidechain": False,
                    "userType": "external",
                    "entrypoint": "cli",
                    "cwd": cwd,
                    "sessionId": session_id,
                    "timestamp": message.timestamp,
                    "version": version,
                })
                parent_uuid = result_uuid
        return records

    @staticmethod
    def _tool_result(call: ToolCall) -> JsonObject:
        return {
            "type": "tool_result",
            "tool_use_id": call.id,
            "content": call.output if call.output is not None else "(no output recorded)",
            "is_error": call.is_error,
        }


def _count_grok_records(session_dir: Path) -> int:
    """Count ACP update envelopes in a Grok session dir (updates.jsonl)."""
    from session_sdk.jsonl import JsonlFile
    try:
        updates = Path(session_dir) / "updates.jsonl"
        if not updates.is_file():
            return -1
        records = JsonlFile(updates).read()
        return sum(1 for r in records if not (isinstance(r, dict) and "_summary" in r))
    except Exception:
        return -1


def _count_freebuff_records(db_path: Path) -> int:
    """Count message rows across all threads in a Freebuff project db."""
    from session_sdk.stores import _freebuff_connect
    try:
        if not Path(db_path).is_file():
            return -1
        conn = _freebuff_connect(db_path)
        try:
            return int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
        finally:
            conn.close()
    except Exception:
        return -1


class GrokRecordBuilder:
    """Build a grok-cli session (superagent-ai/grok-cli ``grok.db``) from a TextMessage stream.

    Records: a leading ``{"_session": {...}}`` header, then one AI SDK ``ModelMessage`` per turn with a
    ``created_at`` key. Tool results are their own ``tool`` message, as grok-cli stores them.
    ``GrokStore.write`` inserts the records into the database.
    """

    def build(
        self,
        session_id: str,
        cwd: str,
        timestamp: str,
        messages: list[TextMessage],
        model_id: str = "grok-4.6",
    ) -> list[JsonObject]:
        from session_sdk.paths import epoch_ms_to_iso, iso_to_epoch_ms

        base = timestamp or ""
        records: list[JsonObject] = []
        title = ""
        first_created = ""
        last_created = ""
        for message in messages:
            if message.is_contextual:
                continue
            if message.role not in ("user", "assistant") and not message.is_compaction:
                continue
            if not (message.text.strip() or message.thinking or message.tool_calls):
                continue
            created = epoch_ms_to_iso(iso_to_epoch_ms(message.timestamp or base)) if (message.timestamp or base) else ""
            first_created = first_created or created
            last_created = created or last_created
            if message.is_compaction or message.role == "user":
                if not title and message.role == "user" and not message.is_compaction:
                    title = message.text.strip().splitlines()[0][:80] if message.text.strip() else ""
                records.append({"role": "user", "content": message.text, "created_at": created})
                continue
            parts: list[JsonObject] = []
            if message.thinking:
                parts.append({"type": "reasoning", "text": message.thinking})
            if message.text:
                parts.append({"type": "text", "text": message.text})
            for call in message.tool_calls:
                parts.append({"type": "tool-call", "toolCallId": call.id, "toolName": call.name, "input": call.arguments})
            records.append({"role": "assistant", "content": parts, "created_at": created})
            if message.tool_calls:
                results: list[JsonObject] = []
                for call in message.tool_calls:
                    text = call.output if call.output is not None else "(no output recorded)"
                    value: JsonObject = {"success": not call.is_error, "output": text}
                    if call.is_error:
                        value["error"] = text
                    results.append({"type": "tool-result", "toolCallId": call.id, "toolName": call.name, "output": {"type": "json", "value": value}})
                records.append({"role": "tool", "content": results, "created_at": created})
        header: JsonObject = {
            "id": session_id,
            "cwd": cwd,
            "title": title or "Imported session",
            "model": model_id,
            "created_at": first_created or last_created,
            "updated_at": last_created or first_created,
        }
        return [{"_session": header}, *records]


class FreebuffRecordBuilder:
    """Build Freebuff Desktop records (SQLite rows) from a TextMessage stream.

    Emits a leading ``{"_thread": {...}}`` metadata record followed by one
    ``{"seq", "role", "parts", "ts"}`` record per message.  ``FreebuffStore``
    writes these into a project ``desktop-v2.db``.
    """

    def build(
        self,
        session_id: str,
        cwd: str,
        timestamp: str,
        messages: list[TextMessage],
        model_id: str = "",
        title: str = "",
    ) -> list[JsonObject]:
        from session_sdk.paths import iso_to_epoch_ms

        base_ms = iso_to_epoch_ms(timestamp) if timestamp else 0
        user_ts_ms: list[int] = []
        all_ts_ms: list[int] = []
        seq = 0
        for message in messages:
            if message.is_contextual:
                continue
            seq += 1
            text = message.text
            if message.is_compaction:
                text = "[compaction summary] " + text
            ts_ms = iso_to_epoch_ms(message.timestamp) if message.timestamp else base_ms + seq
            all_ts_ms.append(ts_ms)
            if message.role in ("user", "system"):
                user_ts_ms.append(ts_ms)
        last_prompt_at = max(user_ts_ms) if user_ts_ms else None
        last_turn_finished_at = max(all_ts_ms) if all_ts_ms else None
        records: list[JsonObject] = [{
            "_thread": {
                "id": session_id,
                "project_id": cwd,
                "project_path": cwd,
                "title": title or "Imported session",
                "status": "open",
                "model": model_id,
                "created_at": base_ms,
                "updated_at": base_ms,
                "last_prompt_at": last_prompt_at,
                "last_turn_finished_at": last_turn_finished_at,
                "last_turn_outcome": "completed",
                "attention_revision": 1,
                "attention_acknowledged_revision": 1,
                "attention_reason": "finished",
                "attention_at": last_turn_finished_at,
                "turn_alive_at": last_turn_finished_at,
            }
        }]
        for message in messages:
            if message.is_contextual:
                continue
            seq += 1
            text = message.text
            if message.is_compaction:
                text = "[compaction summary] " + text
            ts_ms = iso_to_epoch_ms(message.timestamp) if message.timestamp else base_ms + seq
            parts: list[JsonObject] = []
            if message.thinking and message.role == "assistant":
                parts.append({"kind": "reasoning", "id": f"reasoning-{seq}", "text": message.thinking, "open": False, "collapse": "preview"})
            if text or not (message.tool_calls or message.thinking):
                parts.append({"kind": "text", "text": text})
            for call in message.tool_calls:
                tool: JsonObject = {
                    "kind": "tool",
                    "id": call.id,
                    "toolName": call.name,
                    "input": call.arguments,
                    "status": "failure" if call.is_error else "success",
                }
                if call.output is not None:
                    tool["output"] = call.output
                parts.append(tool)
            records.append({
                "seq": seq,
                "role": "user" if message.role != "assistant" else "assistant",
                "parts": parts,
                "ts": ts_ms,
            })
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
            if message.role == "assistant" and not message.is_compaction:
                # ATIF keeps reasoning, tool calls, and their results on the agent step that made them.
                if message.thinking:
                    step["reasoning_content"] = message.thinking
                if message.tool_calls:
                    step["tool_calls"] = [
                        {"tool_call_id": call.id, "function_name": call.name, "arguments": call.arguments}
                        for call in message.tool_calls
                    ]
                    step["observation"] = {
                        "results": [
                            {"source_call_id": call.id, "content": call.output if call.output is not None else "(no output recorded)"}
                            for call in message.tool_calls
                        ]
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
        from uuid import uuid4

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
            content: list[JsonObject] = []
            if message.thinking and message.role == "assistant":
                # Same block shape as Claude Code, which Factory transcripts follow.
                content.append({"type": "thinking", "thinking": message.thinking, "signature": ""})
            if message.text or not (message.tool_calls or message.thinking):
                content.append({"type": "text", "text": message.text})
            content.extend({"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments} for call in message.tool_calls)
            records.append({
                "type": "message",
                "id": str(uuid4()),
                "timestamp": message.timestamp,
                "message": {
                    "role": message.role,
                    "content": content,
                },
            })
            if message.tool_calls:
                records.append({
                    "type": "message",
                    "id": str(uuid4()),
                    "timestamp": message.timestamp,
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": call.id,
                                "content": call.output if call.output is not None else "(no output recorded)",
                                "is_error": call.is_error,
                            }
                            for call in message.tool_calls
                        ],
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
    """Build encrypted Windsurf Cascade .pb files from a TextMessage stream.

    The layout follows ``exa.cortex_pb.CortexTrajectory`` (cortex.proto in the Devin provider of pi-ai):

    - a user turn is a USER_INPUT step (type 14, variant 19, text in field 2);
    - an assistant turn is a PLANNER_RESPONSE step (type 15, variant 20) with the reply (field 1),
      the thinking (field 3), and its tool calls (field 7);
    - each tool call follows as its own step with the type and variant of its tool, holding the
      call and its result. Tools without a Cascade step type are written as MCP tool steps.

    The trajectory is serialized as protobuf, then encrypted with AES-256-GCM using the Windsurf key.
    """

    _ENCRYPTION_KEY = b"safeCodeiumworldKeYsecretBalloon"
    _NONCE_SIZE = 12
    _SHELL = frozenset({"run_command", "bash", "exec", "shell", "terminal"})
    _GREP = frozenset({"grep_search", "grep"})
    _VIEW = frozenset({"view_file", "read_file", "read", "cat"})
    _LIST = frozenset({"list_directory", "list_dir", "ls"})
    _FIND = frozenset({"find_files", "find"})

    def build(self, session_id: str, cwd: str, timestamp: str, messages: list[TextMessage]) -> list[bytes]:
        from session_sdk.paths import iso_to_epoch_ms
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError as exc:
            raise ImportError("cryptography package required for Windsurf session encryption") from exc

        steps: list[bytes] = []
        for message in messages:
            if message.is_contextual:
                continue
            metadata = _windsurf_metadata(iso_to_epoch_ms(message.timestamp) // 1000)
            if message.is_compaction:
                steps.append(_windsurf_step(STEP_TYPE_CHECKPOINT, VARIANT_CHECKPOINT, _encode_string_field(5, message.text), metadata))
                continue
            if message.role == "assistant":
                steps.append(_windsurf_step(STEP_TYPE_PLANNER_RESPONSE, VARIANT_PLANNER_RESPONSE, self._planner_variant(message), metadata))
                for call in message.tool_calls:
                    steps.append(self._tool_step(call, metadata))
            else:
                steps.append(_windsurf_step(STEP_TYPE_USER_INPUT, VARIANT_USER_INPUT, _encode_string_field(2, message.text), metadata))

        trajectory = _encode_string_field(1, session_id)
        for step in steps:
            trajectory += _encode_message_field(2, step)
        trajectory += _encode_varint_field(4, 4)

        # Encrypt: [12-byte nonce][ciphertext+16-byte GCM tag]
        import os as _os
        nonce = _os.urandom(self._NONCE_SIZE)
        ciphertext = AESGCM(self._ENCRYPTION_KEY).encrypt(nonce, trajectory, None)
        return [nonce + ciphertext]

    @staticmethod
    def _planner_variant(message: TextMessage) -> bytes:
        variant = b""
        if message.text:
            variant += _encode_string_field(1, message.text)
        if message.thinking:
            variant += _encode_string_field(3, message.thinking)
        for call in message.tool_calls:
            chat_call = (
                _encode_string_field(1, call.id)
                + _encode_string_field(2, call.name)
                + _encode_string_field(3, json.dumps(call.arguments, ensure_ascii=False))
            )
            variant += _encode_message_field(7, chat_call)
        return variant

    def _tool_step(self, call: ToolCall, metadata: bytes) -> bytes:
        name = call.name.lower()
        args = call.arguments
        output = call.output

        def first(*keys: str) -> str:
            for key in keys:
                value = args.get(key)
                if isinstance(value, str) and value:
                    return value
            return ""

        if name in self._SHELL:
            command = first("command") or json.dumps(args, ensure_ascii=False)
            variant = _encode_string_field(23, command) + _encode_string_field(13, call.id)
            if output is not None:
                variant += _encode_string_field(24, output)
            variant += _encode_string_field(25, command)
            return _windsurf_step(STEP_TYPE_RUN_COMMAND, VARIANT_RUN_COMMAND, variant, metadata)
        if name in self._GREP:
            variant = _encode_string_field(1, first("query", "pattern"))
            if first("glob", "include"):
                variant += _encode_string_field(2, first("glob", "include"))
            if first("path"):
                variant += _encode_string_field(11, first("path"))
            if output is not None:
                variant += _encode_string_field(3, output)
            return _windsurf_step(STEP_TYPE_GREP_SEARCH, VARIANT_GREP, variant, metadata)
        if name in self._VIEW:
            variant = _encode_string_field(1, first("path", "uri", "file_path", "filePath"))
            if output is not None:
                variant += _encode_string_field(4, output)
            return _windsurf_step(STEP_TYPE_VIEW_FILE, VARIANT_VIEW_FILE, variant, metadata)
        if name in self._LIST:
            variant = _encode_string_field(1, first("path", "uri", "directory"))
            for line in (output or "").splitlines():
                if not line.strip():
                    continue
                is_dir = line.endswith("/")
                entry = _encode_string_field(1, line.rstrip("/")) + _encode_varint_field(2, 1 if is_dir else 0)
                variant += _encode_message_field(3, entry)
            return _windsurf_step(STEP_TYPE_LIST_DIRECTORY, VARIANT_LIST_DIRECTORY, variant, metadata)
        if name in self._FIND:
            variant = _encode_string_field(1, first("pattern", "glob", "query"))
            if output is not None:
                variant += _encode_string_field(11, output)
            return _windsurf_step(STEP_TYPE_FIND, VARIANT_FIND_FILES, variant, metadata)
        # Any other tool: an MCP tool step, which keeps the tool name and its arguments.
        chat_call = (
            _encode_string_field(1, call.id)
            + _encode_string_field(2, call.name)
            + _encode_string_field(3, json.dumps(args, ensure_ascii=False))
        )
        variant = _encode_string_field(1, "unisessions") + _encode_message_field(2, chat_call)
        if output is not None:
            variant += _encode_string_field(3, output)
        return _windsurf_step(STEP_TYPE_MCP_TOOL, VARIANT_MCP_CALL, variant, metadata)


def _windsurf_metadata(ts_seconds: int) -> bytes:
    return _encode_message_field(5, _encode_message_field(1, _encode_varint_field(1, ts_seconds)))


def _windsurf_step(step_type: int, variant_field: int, variant: bytes, metadata: bytes) -> bytes:
    """A CortexTrajectoryStep: type (1), status done (4), metadata (5), and the variant."""
    return _encode_varint_field(1, step_type) + _encode_varint_field(4, 3) + metadata + _encode_message_field(variant_field, variant)


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
        source = self._windsurf_store.load(session_id)
        resolved_id = self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        destination = self._pi_store.destination_path(resolved_id, timestamp, source.cwd)
        if not destination.exists():
            # Check if an older export exists under a different trajectory_id
            # (Windsurf .pb files can get updated with new trajectory_ids)
            source_path = source.path
            cascade_id = source_path.stem
            pi_summaries = self._pi_store.list()
            for s in pi_summaries:
                if s.cwd == source.cwd and s.timestamp == timestamp:
                    # Found a likely previous export under old ID
                    old_records = self._count_jsonl(s.path)
                    new_records = self._count_source_records(source)
                    return new_records != old_records
            return True
        old_records = self._count_jsonl(destination)
        new_records = self._count_source_records(source)
        return new_records != old_records

    @staticmethod
    def _count_jsonl(path: Path) -> int:
        from session_sdk.jsonl import JsonlFile
        try:
            return len(JsonlFile(path).read())
        except Exception:
            return -1

    @staticmethod
    def _count_source_records(source: NativeSession) -> int:
        messages = MessageExtractor().from_windsurf(source)
        # header + messages (compaction markers count as messages)
        return 1 + len(messages)

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


class GrokToPiConverter:
    def __init__(self, grok_store: GrokStore, pi_store: PiStore, dcp_store: PiDcpStore, id_factory: SessionIdFactory) -> None:
        self._grok_store = grok_store
        self._pi_store = pi_store
        self._dcp_store = dcp_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = PiRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._grok_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_grok(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        destination = self._pi_store.destination_path(resolved_id, timestamp, source.cwd)
        dcp_path = self._dcp_store.destination_path(resolved_id)
        return ConversionPlan(source, destination, records, (dcp_path,))

    def has_changes(self, session_id: str) -> bool:
        source_path = self._grok_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._pi_store.destination_path(self._id_factory.create(session_id), self._timestamp(""), "")
        if not destination.exists():
            return True
        return _count_grok_records(source_path) != _count_jsonl_records(destination)

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


class PiToGrokConverter:
    def __init__(self, pi_store: PiStore, grok_store: GrokStore, id_factory: SessionIdFactory) -> None:
        self._pi_store = pi_store
        self._grok_store = grok_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = GrokRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._pi_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_pi(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        destination = self._grok_store.destination_path(resolved_id, source.cwd)
        return ConversionPlan(source, destination, records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._pi_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._grok_store.destination_path(self._id_factory.create(session_id), "")
        if destination.exists():
            return _count_jsonl_records(source_path) != _count_grok_records(destination)
        return True

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._grok_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class GrokToCodexConverter:
    def __init__(self, grok_store: GrokStore, codex_store: CodexStore, id_factory: SessionIdFactory) -> None:
        self._grok_store = grok_store
        self._codex_store = codex_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = CodexRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._grok_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_grok(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        destination = self._codex_store.destination_path(resolved_id, timestamp)
        return ConversionPlan(source, destination, records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._grok_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._codex_store.destination_path(self._id_factory.create(session_id), self._timestamp(""))
        if not destination.exists():
            return True
        return _count_grok_records(source_path) != _count_jsonl_records(destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._codex_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class CodexToGrokConverter:
    def __init__(self, codex_store: CodexStore, grok_store: GrokStore, id_factory: SessionIdFactory) -> None:
        self._codex_store = codex_store
        self._grok_store = grok_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = GrokRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._codex_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_codex(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        destination = self._grok_store.destination_path(resolved_id, source.cwd)
        return ConversionPlan(source, destination, records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._codex_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._grok_store.destination_path(self._id_factory.create(session_id), "")
        if destination.exists():
            return _count_jsonl_records(source_path) != _count_grok_records(destination)
        return True

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._grok_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class GrokToOpenCodeConverter:
    def __init__(self, grok_store: GrokStore, opencode_store: OpenCodeStore, id_factory: SessionIdFactory) -> None:
        self._grok_store = grok_store
        self._opencode_store = opencode_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = OpenCodeExportBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._grok_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_grok(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        destination = self._opencode_store.destination_path(resolved_id)
        return ConversionPlan(source, destination, records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._grok_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._opencode_store.destination_path(self._id_factory.create(session_id))
        if not destination.exists():
            return True
        return _count_grok_records(source_path) != _count_opencode_records(destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._opencode_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class OpenCodeToGrokConverter:
    def __init__(self, opencode_store: OpenCodeStore, grok_store: GrokStore, id_factory: SessionIdFactory) -> None:
        self._opencode_store = opencode_store
        self._grok_store = grok_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = GrokRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._opencode_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_opencode(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        destination = self._grok_store.destination_path(resolved_id, source.cwd)
        return ConversionPlan(source, destination, records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._opencode_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._grok_store.destination_path(self._id_factory.create(session_id), "")
        if destination.exists():
            return _count_opencode_records(source_path) != _count_grok_records(destination)
        return True

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._grok_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class GrokToClaudeConverter:
    def __init__(self, grok_store: GrokStore, claude_store: ClaudeStore, id_factory: SessionIdFactory) -> None:
        self._grok_store = grok_store
        self._claude_store = claude_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = ClaudeRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._grok_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_grok(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        destination = self._claude_store.destination_path(resolved_id, source.cwd)
        return ConversionPlan(source, destination, records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._grok_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._claude_store.destination_path(self._id_factory.create(session_id), "")
        if not destination.exists():
            return True
        return _count_grok_records(source_path) != _count_jsonl_records(destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._claude_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ClaudeToGrokConverter:
    def __init__(self, claude_store: ClaudeStore, grok_store: GrokStore, id_factory: SessionIdFactory) -> None:
        self._claude_store = claude_store
        self._grok_store = grok_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = GrokRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._claude_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_claude(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        destination = self._grok_store.destination_path(resolved_id, source.cwd)
        return ConversionPlan(source, destination, records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._claude_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._grok_store.destination_path(self._id_factory.create(session_id), "")
        if destination.exists():
            return _count_jsonl_records(source_path) != _count_grok_records(destination)
        return True

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._grok_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class GrokToDevinConverter:
    def __init__(self, grok_store: GrokStore, devin_store: DevinStore, id_factory: SessionIdFactory) -> None:
        self._grok_store = grok_store
        self._devin_store = devin_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = DevinRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._grok_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_grok(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        destination = self._devin_store.destination_path(resolved_id)
        return ConversionPlan(source, destination, records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._grok_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._devin_store.destination_path(self._id_factory.create(session_id))
        if not destination.exists():
            return True
        return _count_grok_records(source_path) != _count_jsonl_records(destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._devin_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class DevinToGrokConverter:
    def __init__(self, devin_store: DevinStore, grok_store: GrokStore, id_factory: SessionIdFactory) -> None:
        self._devin_store = devin_store
        self._grok_store = grok_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = GrokRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._devin_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_devin(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        destination = self._grok_store.destination_path(resolved_id, source.cwd)
        return ConversionPlan(source, destination, records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._devin_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._grok_store.destination_path(self._id_factory.create(session_id), "")
        if destination.exists():
            return _count_jsonl_records(source_path) != _count_grok_records(destination)
        return True

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._grok_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class GrokToFactoryConverter:
    def __init__(self, grok_store: GrokStore, factory_store: FactoryStore, id_factory: SessionIdFactory) -> None:
        self._grok_store = grok_store
        self._factory_store = factory_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = FactoryRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._grok_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_grok(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        destination = self._factory_store.destination_path(resolved_id, source.cwd)
        return ConversionPlan(source, destination, records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._grok_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._factory_store.destination_path(self._id_factory.create(session_id), "")
        if not destination.exists():
            return True
        return _count_grok_records(source_path) != _count_jsonl_records(destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._factory_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class FactoryToGrokConverter:
    def __init__(self, factory_store: FactoryStore, grok_store: GrokStore, id_factory: SessionIdFactory) -> None:
        self._factory_store = factory_store
        self._grok_store = grok_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = GrokRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._factory_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_factory(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        destination = self._grok_store.destination_path(resolved_id, source.cwd)
        return ConversionPlan(source, destination, records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._factory_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._grok_store.destination_path(self._id_factory.create(session_id), "")
        if destination.exists():
            return _count_jsonl_records(source_path) != _count_grok_records(destination)
        return True

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._grok_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class GrokToWindsurfConverter:
    def __init__(self, grok_store: GrokStore, windsurf_store: WindsurfStore, id_factory: SessionIdFactory) -> None:
        self._grok_store = grok_store
        self._windsurf_store = windsurf_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = WindsurfRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._grok_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_grok(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        destination = self._windsurf_store.destination_path(resolved_id)
        return ConversionPlan(source, destination, records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._grok_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._windsurf_store.destination_path(self._id_factory.create(session_id))
        if not destination.exists():
            return True
        return _count_grok_records(source_path) != _count_jsonl_records(destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._windsurf_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class WindsurfToGrokConverter:
    def __init__(self, windsurf_store: WindsurfStore, grok_store: GrokStore, id_factory: SessionIdFactory) -> None:
        self._windsurf_store = windsurf_store
        self._grok_store = grok_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = GrokRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._windsurf_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_windsurf(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        destination = self._grok_store.destination_path(resolved_id, source.cwd)
        return ConversionPlan(source, destination, records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._windsurf_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._grok_store.destination_path(self._id_factory.create(session_id), "")
        if destination.exists():
            return _count_jsonl_records(source_path) != _count_grok_records(destination)
        return True

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._grok_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class FreebuffToPiConverter:
    def __init__(self, freebuff_store: FreebuffStore, pi_store: PiStore, dcp_store: PiDcpStore, id_factory: SessionIdFactory) -> None:
        self._freebuff_store = freebuff_store
        self._pi_store = pi_store
        self._dcp_store = dcp_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = PiRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._freebuff_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_freebuff(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        destination = self._pi_store.destination_path(resolved_id, timestamp, source.cwd)
        dcp_path = self._dcp_store.destination_path(resolved_id)
        return ConversionPlan(source, destination, records, (dcp_path,))

    def has_changes(self, session_id: str) -> bool:
        source_path = self._freebuff_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._pi_store.destination_path(self._id_factory.create(session_id), self._timestamp(""), "")
        if not destination.exists():
            return True
        return _count_freebuff_records(source_path) != _count_jsonl_records(destination)

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


class PiToFreebuffConverter:
    def __init__(self, pi_store: PiStore, freebuff_store: FreebuffStore, id_factory: SessionIdFactory) -> None:
        self._pi_store = pi_store
        self._freebuff_store = freebuff_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = FreebuffRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._pi_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_pi(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        destination = self._freebuff_store.destination_path(resolved_id, source.cwd)
        return ConversionPlan(source, destination, records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._pi_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._freebuff_store.destination_path(self._id_factory.create(session_id), "")
        if destination.exists():
            return _count_jsonl_records(source_path) != _count_freebuff_records(destination)
        return True

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._freebuff_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class FreebuffToCodexConverter:
    def __init__(self, freebuff_store: FreebuffStore, codex_store: CodexStore, id_factory: SessionIdFactory) -> None:
        self._freebuff_store = freebuff_store
        self._codex_store = codex_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = CodexRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._freebuff_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_freebuff(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        destination = self._codex_store.destination_path(resolved_id, timestamp)
        return ConversionPlan(source, destination, records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._freebuff_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._codex_store.destination_path(self._id_factory.create(session_id), self._timestamp(""))
        if not destination.exists():
            return True
        return _count_freebuff_records(source_path) != _count_jsonl_records(destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._codex_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class CodexToFreebuffConverter:
    def __init__(self, codex_store: CodexStore, freebuff_store: FreebuffStore, id_factory: SessionIdFactory) -> None:
        self._codex_store = codex_store
        self._freebuff_store = freebuff_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = FreebuffRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._codex_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_codex(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        destination = self._freebuff_store.destination_path(resolved_id, source.cwd)
        return ConversionPlan(source, destination, records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._codex_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._freebuff_store.destination_path(self._id_factory.create(session_id), "")
        if destination.exists():
            return _count_jsonl_records(source_path) != _count_freebuff_records(destination)
        return True

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._freebuff_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class FreebuffToOpenCodeConverter:
    def __init__(self, freebuff_store: FreebuffStore, opencode_store: OpenCodeStore, id_factory: SessionIdFactory) -> None:
        self._freebuff_store = freebuff_store
        self._opencode_store = opencode_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = OpenCodeExportBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._freebuff_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_freebuff(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        destination = self._opencode_store.destination_path(resolved_id)
        return ConversionPlan(source, destination, records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._freebuff_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._opencode_store.destination_path(self._id_factory.create(session_id))
        if not destination.exists():
            return True
        return _count_freebuff_records(source_path) != _count_opencode_records(destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._opencode_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class OpenCodeToFreebuffConverter:
    def __init__(self, opencode_store: OpenCodeStore, freebuff_store: FreebuffStore, id_factory: SessionIdFactory) -> None:
        self._opencode_store = opencode_store
        self._freebuff_store = freebuff_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = FreebuffRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._opencode_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_opencode(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        destination = self._freebuff_store.destination_path(resolved_id, source.cwd)
        return ConversionPlan(source, destination, records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._opencode_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._freebuff_store.destination_path(self._id_factory.create(session_id), "")
        if destination.exists():
            return _count_opencode_records(source_path) != _count_freebuff_records(destination)
        return True

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._freebuff_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class FreebuffToClaudeConverter:
    def __init__(self, freebuff_store: FreebuffStore, claude_store: ClaudeStore, id_factory: SessionIdFactory) -> None:
        self._freebuff_store = freebuff_store
        self._claude_store = claude_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = ClaudeRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._freebuff_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_freebuff(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        destination = self._claude_store.destination_path(resolved_id, source.cwd)
        return ConversionPlan(source, destination, records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._freebuff_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._claude_store.destination_path(self._id_factory.create(session_id), "")
        if not destination.exists():
            return True
        return _count_freebuff_records(source_path) != _count_jsonl_records(destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._claude_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ClaudeToFreebuffConverter:
    def __init__(self, claude_store: ClaudeStore, freebuff_store: FreebuffStore, id_factory: SessionIdFactory) -> None:
        self._claude_store = claude_store
        self._freebuff_store = freebuff_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = FreebuffRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._claude_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_claude(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        destination = self._freebuff_store.destination_path(resolved_id, source.cwd)
        return ConversionPlan(source, destination, records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._claude_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._freebuff_store.destination_path(self._id_factory.create(session_id), "")
        if destination.exists():
            return _count_jsonl_records(source_path) != _count_freebuff_records(destination)
        return True

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._freebuff_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class FreebuffToDevinConverter:
    def __init__(self, freebuff_store: FreebuffStore, devin_store: DevinStore, id_factory: SessionIdFactory) -> None:
        self._freebuff_store = freebuff_store
        self._devin_store = devin_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = DevinRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._freebuff_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_freebuff(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        destination = self._devin_store.destination_path(resolved_id)
        return ConversionPlan(source, destination, records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._freebuff_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._devin_store.destination_path(self._id_factory.create(session_id))
        if not destination.exists():
            return True
        return _count_freebuff_records(source_path) != _count_jsonl_records(destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._devin_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class DevinToFreebuffConverter:
    def __init__(self, devin_store: DevinStore, freebuff_store: FreebuffStore, id_factory: SessionIdFactory) -> None:
        self._devin_store = devin_store
        self._freebuff_store = freebuff_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = FreebuffRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._devin_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_devin(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        destination = self._freebuff_store.destination_path(resolved_id, source.cwd)
        return ConversionPlan(source, destination, records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._devin_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._freebuff_store.destination_path(self._id_factory.create(session_id), "")
        if destination.exists():
            return _count_jsonl_records(source_path) != _count_freebuff_records(destination)
        return True

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._freebuff_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class FreebuffToFactoryConverter:
    def __init__(self, freebuff_store: FreebuffStore, factory_store: FactoryStore, id_factory: SessionIdFactory) -> None:
        self._freebuff_store = freebuff_store
        self._factory_store = factory_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = FactoryRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._freebuff_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_freebuff(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        destination = self._factory_store.destination_path(resolved_id, source.cwd)
        return ConversionPlan(source, destination, records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._freebuff_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._factory_store.destination_path(self._id_factory.create(session_id), "")
        if not destination.exists():
            return True
        return _count_freebuff_records(source_path) != _count_jsonl_records(destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._factory_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class FactoryToFreebuffConverter:
    def __init__(self, factory_store: FactoryStore, freebuff_store: FreebuffStore, id_factory: SessionIdFactory) -> None:
        self._factory_store = factory_store
        self._freebuff_store = freebuff_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = FreebuffRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._factory_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_factory(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        destination = self._freebuff_store.destination_path(resolved_id, source.cwd)
        return ConversionPlan(source, destination, records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._factory_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._freebuff_store.destination_path(self._id_factory.create(session_id), "")
        if destination.exists():
            return _count_jsonl_records(source_path) != _count_freebuff_records(destination)
        return True

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._freebuff_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class FreebuffToWindsurfConverter:
    def __init__(self, freebuff_store: FreebuffStore, windsurf_store: WindsurfStore, id_factory: SessionIdFactory) -> None:
        self._freebuff_store = freebuff_store
        self._windsurf_store = windsurf_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = WindsurfRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._freebuff_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_freebuff(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        destination = self._windsurf_store.destination_path(resolved_id)
        return ConversionPlan(source, destination, records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._freebuff_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._windsurf_store.destination_path(self._id_factory.create(session_id))
        if not destination.exists():
            return True
        return _count_freebuff_records(source_path) != _count_jsonl_records(destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._windsurf_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class WindsurfToFreebuffConverter:
    def __init__(self, windsurf_store: WindsurfStore, freebuff_store: FreebuffStore, id_factory: SessionIdFactory) -> None:
        self._windsurf_store = windsurf_store
        self._freebuff_store = freebuff_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = FreebuffRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._windsurf_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_windsurf(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        destination = self._freebuff_store.destination_path(resolved_id, source.cwd)
        return ConversionPlan(source, destination, records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._windsurf_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._freebuff_store.destination_path(self._id_factory.create(session_id), "")
        if destination.exists():
            return _count_jsonl_records(source_path) != _count_freebuff_records(destination)
        return True

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._freebuff_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class FreebuffToGrokConverter:
    def __init__(self, freebuff_store: FreebuffStore, grok_store: GrokStore, id_factory: SessionIdFactory) -> None:
        self._freebuff_store = freebuff_store
        self._grok_store = grok_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = GrokRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._freebuff_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_freebuff(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        destination = self._grok_store.destination_path(resolved_id, source.cwd)
        return ConversionPlan(source, destination, records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._freebuff_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._grok_store.destination_path(self._id_factory.create(session_id), "")
        if destination.exists():
            return _count_freebuff_records(source_path) != _count_grok_records(destination)
        return True

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._grok_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class GrokToFreebuffConverter:
    def __init__(self, grok_store: GrokStore, freebuff_store: FreebuffStore, id_factory: SessionIdFactory) -> None:
        self._grok_store = grok_store
        self._freebuff_store = freebuff_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = FreebuffRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._grok_store.load(session_id)
        resolved_id = target_id or self._id_factory.create(source.session_id)
        timestamp = self._timestamp(source.timestamp)
        messages = self._extractor.from_grok(source)
        records = self._builder.build(resolved_id, source.cwd, timestamp, messages)
        destination = self._freebuff_store.destination_path(resolved_id, source.cwd)
        return ConversionPlan(source, destination, records)

    def has_changes(self, session_id: str) -> bool:
        source_path = self._grok_store._find_path(session_id)
        if source_path is None:
            return True
        destination = self._freebuff_store.destination_path(self._id_factory.create(session_id), "")
        if destination.exists():
            return _count_grok_records(source_path) != _count_freebuff_records(destination)
        return True

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._freebuff_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _timestamp(timestamp: str) -> str:
        if timestamp:
            return timestamp
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _timestamp_or_now(timestamp: str) -> str:
    if timestamp:
        return timestamp
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class _SessionConverterBase:
    """Shared source-to-target pipeline for converters that read their source through
    ``MessageExtractor.from_session``, which dispatches on the session's provider. The T3-to-X
    and OMP-to-X converters are built on it.

    Subclasses choose the target store, the record builder, destination naming, and
    how a destination is counted for ``has_changes``.
    """

    def __init__(self, source_store: SessionStore, id_factory: SessionIdFactory) -> None:
        self._source_store = source_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder: _RecordBuilder

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._source_store.load(session_id)
        timestamp = _timestamp_or_now(source.timestamp)
        resolved_id = target_id or self._resolve_id(source.session_id, timestamp)
        records = self._records(resolved_id, source, timestamp)
        destination = self._destination(resolved_id, timestamp, source.cwd)
        return ConversionPlan(source, destination, records, self._services(resolved_id))

    def has_changes(self, session_id: str) -> bool:
        try:
            source = self._source_store.load(session_id)
        except FileNotFoundError:
            return True
        timestamp = _timestamp_or_now(source.timestamp)
        resolved_id = self._resolve_id(source.session_id, timestamp)
        destination = self._destination(resolved_id, timestamp, source.cwd)
        if not destination_exists(destination):
            return True
        return self._expected(self._records(resolved_id, source, timestamp)) != self._count(destination)

    def _records(self, resolved_id: str, source: NativeSession, timestamp: str) -> list[JsonObject]:
        return self._builder.build(resolved_id, source.cwd, timestamp, self._extractor.from_session(source))

    def _expected(self, records: list[JsonObject]) -> int:
        """Record count the destination should hold, in the same unit as ``_count``."""
        return len(records)

    def _services(self, resolved_id: str) -> tuple[Path, ...]:
        return ()

    def _resolve_id(self, source_id: str, timestamp: str) -> str:
        raise NotImplementedError

    def _destination(self, resolved_id: str, timestamp: str, cwd: str) -> Path:
        raise NotImplementedError

    def _count(self, destination: Path) -> int:
        raise NotImplementedError


class _RecordBuilder(Protocol):
    def build(self, session_id: str, cwd: str, timestamp: str, messages: list[TextMessage]) -> list[JsonObject]: ...


class _ToPiConverter(_SessionConverterBase):
    def __init__(self, source_store: SessionStore, pi_store: PiStore, dcp_store: PiDcpStore, id_factory: SessionIdFactory) -> None:
        super().__init__(source_store, id_factory)
        self._pi_store = pi_store
        self._dcp_store = dcp_store
        self._builder = PiRecordBuilder()

    def _resolve_id(self, source_id: str, timestamp: str) -> str:
        return self._id_factory.create(stable_session_uuid(source_id))

    def _destination(self, resolved_id: str, timestamp: str, cwd: str) -> Path:
        return self._pi_store.destination_path(resolved_id, timestamp, cwd)

    def _services(self, resolved_id: str) -> tuple[Path, ...]:
        return (self._dcp_store.destination_path(resolved_id),)

    def _count(self, destination: Path) -> int:
        return _count_jsonl_records(destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._pi_store.write(plan.destination, plan.records, overwrite=overwrite)
        for service_path in plan.services:
            self._dcp_store.write_default(plan.destination.stem.rsplit("_", 1)[-1], service_path, overwrite=overwrite)


class _ToCodexConverter(_SessionConverterBase):
    def __init__(self, source_store: SessionStore, codex_store: CodexStore, id_factory: SessionIdFactory) -> None:
        super().__init__(source_store, id_factory)
        self._codex_store = codex_store
        self._builder = CodexRecordBuilder()

    def _records(self, resolved_id: str, source: NativeSession, timestamp: str) -> list[JsonObject]:
        messages = self._extractor.from_session(source)
        return self._builder.build(resolved_id, source.cwd, timestamp, messages, f"{source.provider}-import")

    def _resolve_id(self, source_id: str, timestamp: str) -> str:
        return self._id_factory.create(stable_session_uuid(source_id))

    def _destination(self, resolved_id: str, timestamp: str, cwd: str) -> Path:
        return self._codex_store.destination_path(resolved_id, timestamp)

    def _count(self, destination: Path) -> int:
        return _count_jsonl_records(destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._codex_store.write(plan.destination, plan.records, overwrite=overwrite)


class _ToOpenCodeConverter(_SessionConverterBase):
    def __init__(self, source_store: SessionStore, opencode_store: OpenCodeStore, id_factory: SessionIdFactory) -> None:
        super().__init__(source_store, id_factory)
        self._opencode_store = opencode_store
        self._builder = OpenCodeExportBuilder()

    def _resolve_id(self, source_id: str, timestamp: str) -> str:
        if self._id_factory.preserve_ids:
            return stable_opencode_id(stable_session_uuid(source_id), timestamp)
        return opencode_id("ses", timestamp)

    def _destination(self, resolved_id: str, timestamp: str, cwd: str) -> Path:
        return self._opencode_store.destination_path(resolved_id)

    def _expected(self, records: list[JsonObject]) -> int:
        messages = records[0].get("messages") if records else None
        return len(messages) if isinstance(messages, list) else 0

    def _count(self, destination: Path) -> int:
        return _count_opencode_records(destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._opencode_store.write(plan.destination, plan.records, overwrite=overwrite)


class _ToClaudeConverter(_SessionConverterBase):
    def __init__(self, source_store: SessionStore, claude_store: ClaudeStore, id_factory: SessionIdFactory) -> None:
        super().__init__(source_store, id_factory)
        self._claude_store = claude_store
        self._builder = ClaudeRecordBuilder()

    def _resolve_id(self, source_id: str, timestamp: str) -> str:
        return self._id_factory.create(stable_session_uuid(source_id))

    def _destination(self, resolved_id: str, timestamp: str, cwd: str) -> Path:
        return self._claude_store.destination_path(resolved_id, cwd)

    def _count(self, destination: Path) -> int:
        return _count_jsonl_records(destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._claude_store.write(plan.destination, plan.records, overwrite=overwrite)


class _ToDevinConverter(_SessionConverterBase):
    def __init__(self, source_store: SessionStore, devin_store: DevinStore, id_factory: SessionIdFactory) -> None:
        super().__init__(source_store, id_factory)
        self._devin_store = devin_store
        self._builder = DevinRecordBuilder()

    def _resolve_id(self, source_id: str, timestamp: str) -> str:
        return self._id_factory.create(stable_session_uuid(source_id))

    def _destination(self, resolved_id: str, timestamp: str, cwd: str) -> Path:
        return self._devin_store.destination_path(resolved_id)

    def _count(self, destination: Path) -> int:
        # Devin files hold ATIF steps, which the OpenCode counter cannot read. -1 keeps
        # has_changes conservative: an existing destination is rewritten.
        return -1

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._devin_store.write(plan.destination, plan.records, overwrite=overwrite)


class _ToFactoryConverter(_SessionConverterBase):
    def __init__(self, source_store: SessionStore, factory_store: FactoryStore, id_factory: SessionIdFactory) -> None:
        super().__init__(source_store, id_factory)
        self._factory_store = factory_store
        self._builder = FactoryRecordBuilder()

    def _resolve_id(self, source_id: str, timestamp: str) -> str:
        return self._id_factory.create(stable_session_uuid(source_id))

    def _destination(self, resolved_id: str, timestamp: str, cwd: str) -> Path:
        return self._factory_store.destination_path(resolved_id, cwd)

    def _count(self, destination: Path) -> int:
        return _count_jsonl_records(destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._factory_store.write(plan.destination, plan.records, overwrite=overwrite)


class _ToWindsurfConverter(_SessionConverterBase):
    def __init__(self, source_store: SessionStore, windsurf_store: WindsurfStore, id_factory: SessionIdFactory) -> None:
        super().__init__(source_store, id_factory)
        self._windsurf_store = windsurf_store
        self._builder = WindsurfRecordBuilder()

    def _resolve_id(self, source_id: str, timestamp: str) -> str:
        return self._id_factory.create(stable_session_uuid(source_id))

    def _destination(self, resolved_id: str, timestamp: str, cwd: str) -> Path:
        return self._windsurf_store.destination_path(resolved_id)

    def _count(self, destination: Path) -> int:
        # Windsurf destinations are encrypted protobuf files that cannot be counted cheaply,
        # so a destination that exists is always treated as changed (same as CodexToWindsurf).
        return -1

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._windsurf_store.write(plan.destination, plan.records, overwrite=overwrite)


class _ToGrokConverter(_SessionConverterBase):
    def __init__(self, source_store: SessionStore, grok_store: GrokStore, id_factory: SessionIdFactory) -> None:
        super().__init__(source_store, id_factory)
        self._grok_store = grok_store
        self._builder = GrokRecordBuilder()

    def _resolve_id(self, source_id: str, timestamp: str) -> str:
        return self._id_factory.create(stable_session_uuid(source_id))

    def _destination(self, resolved_id: str, timestamp: str, cwd: str) -> Path:
        return self._grok_store.destination_path(resolved_id, cwd)

    def _expected(self, records: list[JsonObject]) -> int:
        # The _session header is a sessions row, not a message row.
        return sum(1 for record in records if "_session" not in record and "_summary" not in record)

    def _count(self, destination: Path) -> int:
        return grok_db_message_count(destination.parent, destination.name)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._grok_store.write(plan.destination, plan.records, overwrite=overwrite)


class _ToFreebuffConverter(_SessionConverterBase):
    def __init__(self, source_store: SessionStore, freebuff_store: FreebuffStore, id_factory: SessionIdFactory) -> None:
        super().__init__(source_store, id_factory)
        self._freebuff_store = freebuff_store
        self._builder = FreebuffRecordBuilder()

    def _resolve_id(self, source_id: str, timestamp: str) -> str:
        return self._id_factory.create(stable_session_uuid(source_id))

    def _destination(self, resolved_id: str, timestamp: str, cwd: str) -> Path:
        return self._freebuff_store.destination_path(resolved_id, cwd)

    def _count(self, destination: Path) -> int:
        # The count covers every thread in the shared project database, not just this one,
        # so it cannot be compared with one source session. -1 keeps has_changes conservative.
        return -1

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._freebuff_store.write(plan.destination, plan.records, overwrite=overwrite)


class T3ToPiConverter(_ToPiConverter):
    """T3 Code thread to Pi session."""


class T3ToCodexConverter(_ToCodexConverter):
    """T3 Code thread to Codex rollout."""


class T3ToOpenCodeConverter(_ToOpenCodeConverter):
    """T3 Code thread to OpenCode export."""


class T3ToClaudeConverter(_ToClaudeConverter):
    """T3 Code thread to Claude Code transcript."""


class T3ToDevinConverter(_ToDevinConverter):
    """T3 Code thread to Devin transcript."""


class T3ToFactoryConverter(_ToFactoryConverter):
    """T3 Code thread to Factory transcript."""


class T3ToWindsurfConverter(_ToWindsurfConverter):
    """T3 Code thread to Windsurf Cascade trajectory."""


class T3ToGrokConverter(_ToGrokConverter):
    """T3 Code thread to Grok Build session."""


class T3ToFreebuffConverter(_ToFreebuffConverter):
    """T3 Code thread to Freebuff Desktop thread."""


class OmpToPiConverter(_ToPiConverter):
    """OMP session (active branch) to Pi session."""


class OmpToCodexConverter(_ToCodexConverter):
    """OMP session (active branch) to Codex rollout."""


class OmpToOpenCodeConverter(_ToOpenCodeConverter):
    """OMP session (active branch) to OpenCode export."""


class OmpToClaudeConverter(_ToClaudeConverter):
    """OMP session (active branch) to Claude Code transcript."""


class OmpToDevinConverter(_ToDevinConverter):
    """OMP session (active branch) to Devin transcript."""


class OmpToFactoryConverter(_ToFactoryConverter):
    """OMP session (active branch) to Factory transcript."""


class OmpToWindsurfConverter(_ToWindsurfConverter):
    """OMP session (active branch) to Windsurf Cascade trajectory."""


class OmpToGrokConverter(_ToGrokConverter):
    """OMP session (active branch) to Grok Build session."""


class OmpToFreebuffConverter(_ToFreebuffConverter):
    """OMP session (active branch) to Freebuff Desktop thread."""


class ToOmpConverter:
    """Write a session from any store as an OMP session.

    The source is read through ``MessageExtractor.from_session`` and built by
    ``OmpRecordBuilder`` into the bucket omp uses for the session's cwd, so ``omp --resume``
    lists it from that folder. A source without a cwd goes to the home folder's bucket. A source
    id that is not a UUID maps to a stable UUID, which keeps it out of omp's
    ``<timestamp>_<id>.jsonl`` file names.

    omp resumes a session on the model its last reply names, and outside the interactive UI it
    refuses to resume when that model is unavailable. Claude Code records the model but not
    the provider, which is always Anthropic, so Claude replies are written as
    ``anthropic/<model>``, as omp's own Claude importer does.
    """

    def __init__(self, source_store: SessionStore, omp_store: OmpStore, id_factory: SessionIdFactory) -> None:
        self._source_store = source_store
        self._omp_store = omp_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = OmpRecordBuilder()

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._source_store.load(session_id)
        timestamp = _omp_session_timestamp(source)
        resolved_id = target_id or self._id_factory.create(stable_session_uuid(source.session_id))
        cwd = source.cwd or str(Path.home())
        messages = self._extractor.from_session(source)
        if source.provider == "claude":
            messages = [
                replace(message, provider="anthropic", api=message.api or "anthropic-messages")
                if message.role == "assistant" and message.provider is None
                else message
                for message in messages
            ]
        records = self._builder.build(resolved_id, cwd, timestamp, messages, source=source)
        return ConversionPlan(source, self._omp_store.destination_path(resolved_id, timestamp, cwd), records)

    def has_changes(self, session_id: str) -> bool:
        try:
            plan = self.plan(session_id)
        except FileNotFoundError:
            return True
        if not plan.destination.exists():
            return True
        return len(plan.records) != _count_jsonl_records(plan.destination)

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._omp_store.write(plan.destination, plan.records, overwrite=overwrite)


class T3RecordBuilder:
    """EXPERIMENTAL. Build one T3 Code thread for the legacy V1 database (see ``T3Store.write``).

    Keeps user and assistant text, in order. Compaction summaries become assistant messages,
    and contextual messages are dropped, as in every other builder. T3 orders messages by
    ``created_at`` and then by message id, so timestamps are made strictly increasing.
    """

    def __init__(self, provider: str) -> None:
        if provider not in T3_PROVIDER_DRIVERS:
            raise ValueError(f"unsupported T3 provider: {provider}")
        self._provider = provider
        self._model = T3_PROVIDER_DRIVERS[provider]

    def build(self, session_id: str, cwd: str, timestamp: str, messages: list[TextMessage]) -> list[JsonObject]:
        kept = [
            message
            for message in messages
            if not message.is_contextual and (message.role in ("user", "assistant") or message.is_compaction) and message.text.strip()
        ]
        base_ms = iso_to_epoch_ms(timestamp)
        rows: list[JsonObject] = []
        previous_ms: int | None = None
        for ordinal, message in enumerate(kept):
            ms = iso_to_epoch_ms(message.timestamp) if message.timestamp else base_ms + ordinal
            if previous_ms is not None and ms <= previous_ms:
                ms = previous_ms + 1
            previous_ms = ms
            is_user = message.role == "user" and not message.is_compaction
            rows.append({
                "message_id": str(uuid5(NAMESPACE_URL, f"unisessions:{session_id}:message:{ordinal}")),
                "role": "user" if is_user else "assistant",
                "text": message.text,
                "created_at": epoch_ms_to_iso(ms),
            })

        created = str(rows[0]["created_at"]) if rows else epoch_ms_to_iso(base_ms)
        updated = str(rows[-1]["created_at"]) if rows else created
        first_user = next((str(row["text"]) for row in rows if row["role"] == "user"), "")
        title = first_user.strip().splitlines()[0].strip()[:80] if first_user.strip() else "Imported thread"

        workspace_root = cwd or str(Path.home())
        project_id = str(uuid5(NAMESPACE_URL, f"unisessions:project:{workspace_root}"))
        project: JsonObject = {
            "project_id": project_id,
            "title": Path(workspace_root).name or workspace_root,
            "workspace_root": workspace_root,
            "created_at": created,
            "updated_at": created,
        }
        thread: JsonObject = {
            "thread_id": session_id,
            "project_id": project_id,
            "title": title,
            "created_at": created,
            "updated_at": updated,
            # Imported history should ask before acting, not run with full access.
            "runtime_mode": "approval-required",
            "interaction_mode": "default",
            "model_selection_json": json.dumps({"instanceId": self._provider, "model": self._model}, separators=(",", ":")),
        }
        return [{"thread": thread, "project": project, "messages": rows}]


class ToT3Converter:
    """EXPERIMENTAL. Write any session into a T3 Code thread.

    The thread goes into a legacy V1 ``state.sqlite`` in a new T3 home. T3 imports it when it
    first creates ``statev2.sqlite``. Only text is written (no tool calls, approvals, or
    checkpoints), and the model is the target driver's default, because model names do not
    carry across providers.
    """

    def __init__(self, source_store: SessionStore, t3_store: T3Store, id_factory: SessionIdFactory, provider: str) -> None:
        self._source_store = source_store
        self._t3_store = t3_store
        self._id_factory = id_factory
        self._extractor = MessageExtractor()
        self._builder = T3RecordBuilder(provider)

    def plan(self, session_id: str, *, target_id: str | None = None) -> ConversionPlan:
        source = self._source_store.load(session_id)
        timestamp = _timestamp_or_now(source.timestamp)
        resolved_id = target_id or self._id_factory.create(stable_session_uuid(source.session_id))
        records = self._builder.build(resolved_id, source.cwd, timestamp, self._extractor.from_session(source))
        return ConversionPlan(source, self._t3_store.legacy_database, records)

    def destination_taken(self, plan: ConversionPlan) -> bool:
        """Whether this thread already exists. Every thread shares one database file."""
        return self._t3_store.thread_exists(plan.destination, self._thread_id(plan))

    def has_changes(self, session_id: str) -> bool:
        try:
            plan = self.plan(session_id)
        except FileNotFoundError:
            return True
        messages = plan.records[0].get("messages")
        expected = len(messages) if isinstance(messages, list) else -1
        count = self._t3_store.thread_message_count(plan.destination, self._thread_id(plan))
        return count is None or count != expected

    def write(self, plan: ConversionPlan, *, overwrite: bool = False) -> None:
        self._t3_store.write(plan.destination, plan.records, overwrite=overwrite)

    @staticmethod
    def _thread_id(plan: ConversionPlan) -> str:
        thread = plan.records[0].get("thread")
        return str(thread.get("thread_id", "")) if isinstance(thread, dict) else ""

