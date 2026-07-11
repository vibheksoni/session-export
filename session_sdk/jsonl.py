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


def _loads(data: bytes) -> object:
    if _HAS_ORJSON:
        return orjson.loads(data)
    return json.loads(data)


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

