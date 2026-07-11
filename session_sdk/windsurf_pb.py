"""Protobuf wire-format parser for decrypted Windsurf Cascade trajectory files.

Windsurf stores Cascade sessions as AES-256-GCM encrypted protobuf messages
in ``.pb`` files under ``~/.codeium/windsurf/cascade/``. After decryption,
the plaintext is a ``CortexTrajectory`` protobuf message containing:

- Field 1: trajectory_id (string, UUID)
- Field 2: repeated CortexTrajectoryStep messages
- Field 4: trajectory_type (enum)
- Field 6: cascade_id (string, UUID)
- Field 8: source (enum)

Each ``CortexTrajectoryStep`` has:
- Field 1: type (enum)
- Field 4: status (enum)
- Field 5: metadata (message with timestamp)
- Field 19: user_input variant (field 2 = prompt text)
- Field 20: planner_response variant (field 1 = visible text, field 3 = thinking)
- Field 28: run_command variant (field 23 = command, field 24 = output)
- Field 30: checkpoint variant (compaction summary)
- Field 38: context/system injection (field 1 = "user_global", field 5 = content)

This module uses raw wire-format parsing (no .proto compilation needed).
"""

from __future__ import annotations

from typing import Iterator


def read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    """Read a protobuf varint. Return (value, new_pos)."""
    val = 0
    shift = 0
    while pos < len(buf):
        b = buf[pos]
        pos += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, pos
        shift += 7
    raise ValueError("unterminated varint")


def parse_tag(tag: int) -> tuple[int, int]:
    """Return (field_no, wire_type) from a protobuf tag."""
    return tag >> 3, tag & 7


def iter_fields(buf: bytes, start: int = 0, end: int | None = None) -> Iterator[tuple[int, int, int, bytes | int]]:
    """Yield (field_no, wire_type, offset, value) over a protobuf message body.

    For wire_type 0 (varint): value is int.
    For wire_type 2 (length-delimited): value is bytes.
    For wire_type 1 (64-bit): value is bytes (8 bytes).
    For wire_type 5 (32-bit): value is bytes (4 bytes).
    """
    if end is None:
        end = len(buf)
    pos = start
    while pos < end:
        tag, pos = read_varint(buf, pos)
        fno, wt = parse_tag(tag)
        if wt == 0:
            val, pos = read_varint(buf, pos)
            yield fno, wt, pos, val
        elif wt == 2:
            length, pos = read_varint(buf, pos)
            yield fno, wt, pos, buf[pos:pos + length]
            pos += length
        elif wt == 1:
            yield fno, wt, pos, buf[pos:pos + 8]
            pos += 8
        elif wt == 5:
            yield fno, wt, pos, buf[pos:pos + 4]
            pos += 4
        else:
            raise ValueError(f"wire type {wt} unsupported at offset {pos}")


def read_string_field(buf: bytes, target_fno: int) -> str | None:
    """Walk a message body, return the first string-typed value at field number target_fno."""
    for fno, wt, _off, val in iter_fields(buf):
        if fno == target_fno and wt == 2 and isinstance(val, (bytes, bytearray)):
            return val.decode("utf-8", errors="replace")
    return None


def read_all_string_fields(buf: bytes, target_fno: int) -> list[str]:
    """Walk a message body, return all string-typed values at field number target_fno."""
    results: list[str] = []
    for fno, wt, _off, val in iter_fields(buf):
        if fno == target_fno and wt == 2 and isinstance(val, (bytes, bytearray)):
            results.append(val.decode("utf-8", errors="replace"))
    return results


def parse_trajectory(buf: bytes) -> dict[str, object]:
    """Parse top-level CortexTrajectory.

    Returns dict with trajectory_id, cascade_id, trajectory_type, source, steps.
    Each step in steps is raw bytes (the CortexTrajectoryStep message body).
    """
    info: dict[str, object] = {
        "trajectory_id": None,
        "cascade_id": None,
        "trajectory_type": None,
        "source": None,
        "steps": [],
    }
    for fno, wt, _off, val in iter_fields(buf):
        if fno == 1 and wt == 2 and isinstance(val, (bytes, bytearray)):
            info["trajectory_id"] = val.decode("utf-8", errors="replace")
        elif fno == 6 and wt == 2 and isinstance(val, (bytes, bytearray)):
            info["cascade_id"] = val.decode("utf-8", errors="replace")
        elif fno == 4 and wt == 0:
            info["trajectory_type"] = val
        elif fno == 8 and wt == 0:
            info["source"] = val
        elif fno == 2 and wt == 2 and isinstance(val, (bytes, bytearray)):
            info["steps"].append(val)
    return info


# Step variant field numbers (from empirical analysis of decrypted trajectories)
VARIANT_USER_INPUT = 19
VARIANT_PLANNER_RESPONSE = 20
VARIANT_RUN_COMMAND = 28
VARIANT_CHECKPOINT = 30
VARIANT_FILE_CONTEXT = 15
VARIANT_COMMAND_RESULT = 37
VARIANT_CONTEXT_INJECTION = 38


def parse_step(step_buf: bytes) -> dict[str, int | bytes | None]:
    """Parse a CortexTrajectoryStep.

    Returns dict with type, status, variant_field, variant_data.
    variant_field is the first length-delimited field >= 7 (the oneof step variant).
    """
    out: dict[str, int | bytes | None] = {
        "type": None,
        "status": None,
        "variant_field": None,
        "variant_data": None,
    }
    for fno, wt, _off, val in iter_fields(step_buf):
        if fno == 1 and wt == 0:
            out["type"] = val
        elif fno == 4 and wt == 0:
            out["status"] = val
        elif 7 <= fno <= 110 and wt == 2 and isinstance(val, (bytes, bytearray)):
            if out["variant_field"] is None:
                out["variant_field"] = fno
                out["variant_data"] = val
    return out


def parse_step_timestamp(step_buf: bytes) -> int | None:
    """Extract timestamp from step metadata (field 5, sub-field 1, sub-field 1 = seconds)."""
    for fno, wt, _off, val in iter_fields(step_buf):
        if fno == 5 and wt == 2 and isinstance(val, (bytes, bytearray)):
            for sfno, swt, _soff, sval in iter_fields(val):
                if sfno == 1 and swt == 2 and isinstance(sval, (bytes, bytearray)):
                    for ssfno, sswt, _ssoff, ssval in iter_fields(sval):
                        if ssfno == 1 and sswt == 0 and isinstance(ssval, int):
                            return ssval
    return None


def parse_checkpoint(cp_buf: bytes) -> dict[str, object]:
    """Parse CortexStepCheckpoint (variant_field=30).

    Returns dict with conversation_title, user_intent, session_summary,
    code_change_summary, memory_summary, plan_snapshot fields.
    """
    out: dict[str, object] = {
        "checkpoint_index": None,
        "user_intent": None,
        "session_summary": None,
        "code_change_summary": None,
        "memory_summary": None,
        "conversation_title": None,
        "plan_snapshot": None,
        "intent_only": None,
    }
    for fno, wt, _off, val in iter_fields(cp_buf):
        if fno == 1 and wt == 0:
            out["checkpoint_index"] = val
        elif fno == 4 and wt == 2 and isinstance(val, (bytes, bytearray)):
            out["user_intent"] = val.decode("utf-8", errors="replace")
        elif fno == 5 and wt == 2 and isinstance(val, (bytes, bytearray)):
            out["session_summary"] = val.decode("utf-8", errors="replace")
        elif fno == 6 and wt == 2 and isinstance(val, (bytes, bytearray)):
            out["code_change_summary"] = val.decode("utf-8", errors="replace")
        elif fno == 8 and wt == 2 and isinstance(val, (bytes, bytearray)):
            out["memory_summary"] = val.decode("utf-8", errors="replace")
        elif fno == 9 and wt == 0:
            out["intent_only"] = bool(val)
        elif fno == 10 and wt == 2 and isinstance(val, (bytes, bytearray)):
            out["conversation_title"] = val.decode("utf-8", errors="replace")
        elif fno == 13 and wt == 2 and isinstance(val, (bytes, bytearray)):
            out["plan_snapshot"] = val.decode("utf-8", errors="replace")
    return out
