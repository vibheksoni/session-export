from __future__ import annotations

import json
import sys
from pathlib import Path

from session_sdk.json_types import JsonObject

try:
    import orjson

    _HAS_ORJSON = True
except ImportError:
    _HAS_ORJSON = False


def _loads(data: bytes | str) -> object:
    if _HAS_ORJSON:
        try:
            return orjson.loads(data)
        except orjson.JSONDecodeError:
            # orjson refuses a lone UTF-16 surrogate escape (half an emoji, which
            # some agents write when they cut text mid-character). One such line
            # used to make a whole session unreadable. The standard parser takes it.
            pass
    return _without_lone_surrogates(json.loads(data))


def _without_lone_surrogates(value: object) -> object:
    """Replace unpaired surrogates with U+FFFD so the text encodes as UTF-8
    again (SQLite, orjson.dumps, and file writes all refuse them)."""
    if isinstance(value, str):
        try:
            value.encode("utf-8")
            return value
        except UnicodeEncodeError:
            return value.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    if isinstance(value, dict):
        return {_without_lone_surrogates(k): _without_lone_surrogates(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_without_lone_surrogates(item) for item in value]
    return value


def _dumps(obj: JsonObject) -> bytes:
    if _HAS_ORJSON:
        return orjson.dumps(obj)
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class JsonlFile:
    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def read(self) -> list[JsonObject]:
        records: list[JsonObject] = []
        with self._path.open("rb") as handle:
            for line_number, line in enumerate(handle, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    value = _loads(stripped)
                except (ValueError, json.JSONDecodeError) as exc:
                    # Skip corrupted lines (e.g. backslash-prefixed lines from Codex bugs)
                    print(f"warning: skipped unparseable line {self._path}:{line_number}: {exc}", file=sys.stderr)
                    continue
                if not isinstance(value, dict):
                    raise ValueError(f"{self._path}:{line_number} is not a JSON object")
                records.append(value)
        return records

    def write(self, records: list[JsonObject], *, overwrite: bool = False) -> None:
        if self._path.exists() and not overwrite:
            raise FileExistsError(f"Refusing to overwrite existing file: {self._path}")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        with temp_path.open("wb") as handle:
            for record in records:
                handle.write(_dumps(record))
                handle.write(b"\n")
        temp_path.replace(self._path)

