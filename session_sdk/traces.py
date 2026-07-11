"""Trace format builders for exporting sessions to HuggingFace, OpenAI, and ShareGPT trace formats.

Three trace formats are supported:

1. **STS-Format** (Session Trace Simple Format) — HuggingFace Hub's native trace viewer format.
   JSONL with a session header line followed by message lines.
   https://huggingface.co/docs/hub/session-traces-format

2. **OpenAI Fine-tuning format** — standard ``{"messages": [{"role": ..., "content": ...}]}`` JSONL,
   one conversation per line. Compatible with OpenAI, Azure, and HuggingFace TRL fine-tuning pipelines.
   https://platform.openai.com/docs/guides/fine-tuning

3. **ShareGPT format** — community standard ``{"conversations": [{"from": ..., "value": ...}]}`` JSONL,
   used by LLaMA-Factory, Axolotl, torchtune, and many open-source training frameworks.
   https://github.com/lm-sys/FastChat/blob/main/docs/dataset_formats.md

All builders consume the same ``list[TextMessage]`` produced by ``MessageExtractor`` and emit
JSONL records that can be written to a file and uploaded to HuggingFace Hub, OpenAI, or any
training framework that accepts the target format.
"""

from __future__ import annotations

from session_sdk.json_types import JsonObject
from session_sdk.models import NativeSession, TextMessage


class STSTraceBuilder:
    """Build HuggingFace Session Trace Simple Format (STS-Format) JSONL.

    Output: one JSON object per line. First line is the session header,
    subsequent lines are message envelopes.

    Example::

        {"type":"session","harness":"codex","id":"abc123","name":"Fix the bug"}
        {"type":"message","message":{"role":"user","content":"Fix the bug"}}
        {"type":"message","message":{"role":"assistant","content":"Looking at it now","timestamp":1719900000000,"model":"gpt-4o"}}
    """

    def build(self, session: NativeSession, messages: list[TextMessage]) -> list[JsonObject]:
        header: JsonObject = {
            "type": "session",
            "harness": session.provider,
            "id": session.session_id,
        }
        if session.cwd:
            header["cwd"] = session.cwd
        records: list[JsonObject] = [header]
        for message in messages:
            if message.is_contextual:
                continue
            if message.is_compaction:
                # Compaction summaries become system messages with a marker
                records.append({
                    "type": "message",
                    "message": {
                        "role": "system",
                        "content": f"[compaction summary]\n{message.text}",
                    },
                })
                continue
            msg: JsonObject = {
                "role": message.role,
                "content": message.text,
            }
            if message.model:
                msg["model"] = message.model
            if message.timestamp:
                msg["timestamp"] = _to_epoch_ms(message.timestamp)
            records.append({"type": "message", "message": msg})
        return records


class OpenAITraceBuilder:
    """Build OpenAI fine-tuning conversation format JSONL.

    Output: one JSON object per line, each containing a ``messages`` array
    with the full conversation. Designed for ``client.files.create(purpose='fine-tune')``.

    Example::

        {"messages":[{"role":"user","content":"Fix the bug"},{"role":"assistant","content":"Looking at it now"}]}
    """

    def build(self, session: NativeSession, messages: list[TextMessage]) -> list[JsonObject]:
        conversation: list[JsonObject] = []
        for message in messages:
            if message.is_contextual:
                continue
            if message.is_compaction:
                conversation.append({
                    "role": "system",
                    "content": f"[compaction summary]\n{message.text}",
                })
                continue
            conversation.append({
                "role": message.role,
                "content": message.text,
            })
        return [{"messages": conversation}]


class ShareGPTTraceBuilder:
    """Build ShareGPT conversation format JSONL.

    Output: one JSON object per line with a ``conversations`` array.
    Role mapping: user→human, assistant→gpt, system→system, tool→tool.

    Example::

        {"conversations":[{"from":"human","value":"Fix the bug"},{"from":"gpt","value":"Looking at it now"}]}
    """

    _ROLE_MAP: dict[str, str] = {
        "user": "human",
        "assistant": "gpt",
        "system": "system",
        "tool": "tool",
    }

    def build(self, session: NativeSession, messages: list[TextMessage]) -> list[JsonObject]:
        conversations: list[JsonObject] = []
        for message in messages:
            if message.is_contextual:
                continue
            if message.is_compaction:
                conversations.append({
                    "from": "system",
                    "value": f"[compaction summary]\n{message.text}",
                })
                continue
            conversations.append({
                "from": self._ROLE_MAP.get(message.role, message.role),
                "value": message.text,
            })
        return [{"conversations": conversations}]


# Trace format registry
TRACE_BUILDERS: dict[str, type] = {
    "sts": STSTraceBuilder,
    "openai": OpenAITraceBuilder,
    "sharegpt": ShareGPTTraceBuilder,
}

TRACE_FORMATS = tuple(TRACE_BUILDERS.keys())


def build_trace(
    format: str,
    session: NativeSession,
    messages: list[TextMessage],
) -> list[JsonObject]:
    """Build trace records for the given format.

    Args:
        format: One of ``"sts"``, ``"openai"``, ``"sharegpt"``.
        session: The source NativeSession.
        messages: Extracted TextMessages from MessageExtractor.

    Returns:
        List of JSON objects to write as JSONL lines.

    Raises:
        ValueError: If the format is not recognized.
    """
    builder_cls = TRACE_BUILDERS.get(format)
    if builder_cls is None:
        raise ValueError(f"Unknown trace format: {format!r}. Supported: {', '.join(TRACE_FORMATS)}")
    return builder_cls().build(session, messages)


def _to_epoch_ms(timestamp: str) -> int:
    """Convert ISO timestamp string to epoch milliseconds."""
    from session_sdk.paths import iso_to_epoch_ms
    return iso_to_epoch_ms(timestamp)
