from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TypeAlias, cast

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]


def as_object(value: JsonValue | object) -> JsonObject | None:
    if isinstance(value, dict):
        return cast(JsonObject, value)
    return None


def as_list(value: JsonValue | object) -> list[JsonValue] | None:
    if isinstance(value, list):
        return cast(list[JsonValue], value)
    return None


def as_str(value: JsonValue | object) -> str | None:
    if isinstance(value, str):
        return value
    return None


def string_value(mapping: Mapping[str, JsonValue], key: str) -> str | None:
    return as_str(mapping.get(key))


def json_object(**kwargs: JsonValue) -> JsonObject:
    return dict(kwargs)


def sequence_to_text(parts: Sequence[JsonValue]) -> str:
    text_parts: list[str] = []
    for part in parts:
        if isinstance(part, str):
            text_parts.append(part)
            continue
        obj = as_object(part)
        if obj is None:
            continue
        text = as_str(obj.get("text")) or as_str(obj.get("input_text")) or as_str(obj.get("output_text"))
        if text is not None:
            text_parts.append(text)
    return "\n".join(piece for piece in text_parts if piece)

