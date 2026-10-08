from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    import orjson
    _HAS_ORJSON = True
except ImportError:
    _HAS_ORJSON = False

from session_sdk.json_types import JsonObject, as_list, as_object, as_str, string_value
from session_sdk.jsonl import JsonlFile
from session_sdk.models import NativeSession, SessionSummary
from session_sdk.paths import codex_date_parts, codex_filename_timestamp, decode_grok_cwd_dirname, encode_grok_cwd_dirname, encode_pi_cwd, epoch_ms_to_iso, pi_filename_timestamp, sanitize_claude_cwd

import sqlite3 as _sqlite3


def _json_loads(data: str | bytes) -> object:
    if _HAS_ORJSON:
        if isinstance(data, str):
            data = data.encode("utf-8")
        return orjson.loads(data)
    return json.loads(data)


class SessionStore:
    provider_name: str

    def _cached(self, reader, path: Path):
        """Result of a per-file listing reader (`_safe_summary`, `_safe_metadata`, ...), cached
        until the file's mtime or size changes (for SQLite stores, also its -wal file). Listing
        used to re-read every session on every call: ~3 s per search across all providers."""
        memo = self.__dict__.get("_summary_memo")
        if memo is None:
            memo = self.__dict__.setdefault("_summary_memo", {})
        try:
            st = os.stat(path)
        except OSError:
            return reader(path)
        stamp: tuple[int, ...] = (st.st_mtime_ns, st.st_size)
        try:
            wal = os.stat(f"{path}-wal")
            stamp += (wal.st_mtime_ns, wal.st_size)
        except OSError:
            pass
        key = (reader.__name__, str(path))
        hit = memo.get(key)
        if hit is not None and hit[0] == stamp:
            return hit[1]
        value = reader(path)
        memo[key] = (stamp, value)
        return value

    def invalidate_paths(self) -> None:
        """Forget the cached list of session files (and the id index), so the next listing sees
        sessions created since. Cached per-file summaries stay valid."""
        if hasattr(self, "_path_cache"):
            self._path_cache = None
        if hasattr(self, "_id_index"):
            self._id_index = None

    def list(self, *, workers: int = 1) -> list[SessionSummary]:
        raise NotImplementedError

    def list_metadata(self, *, workers: int = 1) -> list[SessionSummary]:
        return self.list(workers=workers)

    def load(self, session_id: str) -> NativeSession:
        raise NotImplementedError

    def load_path(self, path: Path) -> NativeSession:
        raise NotImplementedError


class CodexStore(SessionStore):
    provider_name = "codex"

    def __init__(self, codex_home: Path, session_dir: Path | None = None) -> None:
        self._codex_home = codex_home
        self._session_dir = session_dir
        self._path_cache: list[Path] | None = None
        self._id_index: dict[str, Path] | None = None

    @property
    def root(self) -> Path:
        return self._codex_home

    def list(self, *, workers: int = 1) -> list[SessionSummary]:
        paths = self._session_paths()
        if workers <= 1 or len(paths) <= 1:
            return [summary for path in paths if (summary := self._cached(self._safe_summary, path)) is not None]
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(workers, len(paths)), thread_name_prefix="cx-list") as executor:
            results = list(executor.map(lambda p: self._cached(self._safe_summary, p), paths))
        return [s for s in results if s is not None]

    def load(self, session_id: str) -> NativeSession:
        path = self._find_path(session_id)
        if path is not None:
            return self._load_file(path)
        raise FileNotFoundError(f"Codex session not found: {session_id}")

    def load_path(self, path: Path) -> NativeSession:
        return self._load_file(path)

    def _find_path(self, session_id: str) -> Path | None:
        index = self._id_index_cache()
        if session_id in index:
            return index[session_id]
        for path in self._session_paths():
            if session_id in path.name:
                return path
        return None

    def destination_path(self, session_id: str, timestamp: str) -> Path:
        year, month, day = codex_date_parts(timestamp)
        filename = f"rollout-{codex_filename_timestamp(timestamp)}-{session_id}.jsonl"
        return self._active_session_root() / year / month / day / filename

    def write(self, path: Path, records: list[JsonObject], *, overwrite: bool = False) -> None:
        JsonlFile(path).write(records, overwrite=overwrite)

    def _session_paths(self) -> list[Path]:
        if self._path_cache is not None:
            return self._path_cache
        roots = self._session_roots()
        paths: list[Path] = []
        for root in roots:
            if root.exists():
                paths.extend(root.rglob("*.jsonl"))
        paths.sort()
        self._path_cache = paths
        return paths

    def _id_index_cache(self) -> dict[str, Path]:
        if self._id_index is not None:
            return self._id_index
        index: dict[str, Path] = {}
        for path in self._session_paths():
            sid = self._id_from_filename(path)
            if sid:
                index[sid] = path
        self._id_index = index
        return index

    def _session_roots(self) -> list[Path]:
        if self._session_dir is not None:
            return [self._session_dir]
        return [self._codex_home / "sessions", self._codex_home / "archived_sessions"]

    def _active_session_root(self) -> Path:
        return self._session_dir or (self._codex_home / "sessions")

    def _load_file(self, path: Path) -> NativeSession:
        records = self._normalize_records(JsonlFile(path).read())
        meta = self._first_payload(records, "session_meta")
        session_id = string_value(meta, "id") or self._id_from_filename(path)
        cwd = string_value(meta, "cwd") or ""
        timestamp = string_value(meta, "timestamp") or self._timestamp_from_file(path)
        return NativeSession("codex", session_id, cwd, timestamp, path, records)

    def _safe_load_file(self, path: Path) -> NativeSession | None:
        try:
            return self._load_file(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"warning: skipped unreadable Codex session {path}: {exc}", file=sys.stderr)
            return None

    def _safe_summary(self, path: Path) -> SessionSummary | None:
        try:
            meta = self._read_head_meta(path)
            session_id = string_value(meta, "id") or self._id_from_filename(path)
            cwd = string_value(meta, "cwd") or ""
            timestamp = string_value(meta, "timestamp") or self._timestamp_from_file(path)
            return SessionSummary("codex", session_id, cwd, timestamp, path, -1)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"warning: skipped unreadable Codex session {path}: {exc}", file=sys.stderr)
            return None

    @staticmethod
    def _read_head_meta(path: Path) -> JsonObject:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if line_number > 200:
                    break
                stripped = line.strip()
                if not stripped:
                    continue
                value = _json_loads(stripped)
                if not isinstance(value, dict):
                    raise ValueError(f"{path}:{line_number} is not a JSON object")
                record = as_object(value)
                if record is None:
                    continue
                if record.get("type") == "session_meta":
                    payload = as_object(record.get("payload"))
                    if payload is not None:
                        return payload
                # Old format: first record has id/timestamp directly, no type field
                if line_number == 1 and "type" not in record and "id" in record:
                    return record
        return {}

    @staticmethod
    def _first_payload(records: list[JsonObject], record_type: str) -> JsonObject:
        for record in records:
            if record.get("type") == record_type:
                payload = as_object(record.get("payload"))
                if payload is not None:
                    return payload
        return {}

    @staticmethod
    def _normalize_records(records: list[JsonObject]) -> list[JsonObject]:
        """Normalize old flat Codex format to the new wrapped format."""
        if not records:
            return records
        first = records[0]
        # New format: first record has type == "session_meta"
        if first.get("type") == "session_meta":
            return records
        # Old format: first record has id/timestamp directly, no type field
        if "type" not in first and "id" in first:
            normalized: list[JsonObject] = [
                {
                    "type": "session_meta",
                    "timestamp": first.get("timestamp", ""),
                    "payload": {
                        "id": first.get("id", ""),
                        "timestamp": first.get("timestamp", ""),
                        "cwd": first.get("cwd", ""),
                    },
                }
            ]
            for record in records[1:]:
                # Skip state records
                if "record_type" in record:
                    continue
                # Already wrapped (unlikely in old format, but safe)
                if record.get("type") == "response_item":
                    normalized.append(record)
                    continue
                # Flat message: {"type":"message","role":"...","content":[...]}
                if record.get("type") == "message":
                    normalized.append({
                        "type": "response_item",
                        "timestamp": record.get("timestamp", first.get("timestamp", "")),
                        "payload": record,
                    })
                    continue
                # Unknown record type -- pass through as-is
                normalized.append(record)
            return normalized
        return records

    @staticmethod
    def _id_from_filename(path: Path) -> str:
        stem = path.stem
        if len(stem) >= 36:
            candidate = stem[-36:]
            if candidate.count("-") == 4:
                return candidate
        return stem

    @staticmethod
    def _timestamp_from_file(path: Path) -> str:
        stem = path.stem
        if not stem.startswith("rollout-"):
            return path.stat().st_mtime_ns.__str__()
        stripped = stem[len("rollout-"):]
        # UUID is always 36 chars; char before it is a dash (37 total)
        if len(stripped) < 37:
            return path.stat().st_mtime_ns.__str__()
        date_part = stripped[:len(stripped) - 37]
        return date_part if date_part else path.stat().st_mtime_ns.__str__()

    @staticmethod
    def _message_count(records: list[JsonObject]) -> int:
        total = 0
        for record in records:
            payload = as_object(record.get("payload"))
            if payload is not None and payload.get("type") == "message":
                total += 1
        return total


class PiStore(SessionStore):
    provider_name = "pi"

    def __init__(self, pi_agent_home: Path, session_dir: Path | None = None) -> None:
        self._pi_agent_home = pi_agent_home
        self._session_dir = session_dir
        self._path_cache: list[Path] | None = None
        self._id_index: dict[str, Path] | None = None

    @property
    def root(self) -> Path:
        return self._pi_agent_home

    def list(self, *, workers: int = 1) -> list[SessionSummary]:
        paths = self._session_paths()
        if workers <= 1 or len(paths) <= 1:
            return [s for path in paths if (s := self._cached(self._safe_summary, path)) is not None]
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(workers, len(paths)), thread_name_prefix="pi-list") as executor:
            results = list(executor.map(lambda p: self._cached(self._safe_summary, p), paths))
        return [s for s in results if s is not None]

    def list_metadata(self, *, workers: int = 1) -> list[SessionSummary]:
        paths = self._session_paths()
        if workers <= 1 or len(paths) <= 1:
            return [s for path in paths if (s := self._cached(self._safe_metadata, path)) is not None]
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(workers, len(paths)), thread_name_prefix="pi-meta") as executor:
            results = list(executor.map(lambda p: self._cached(self._safe_metadata, p), paths))
        return [s for s in results if s is not None]

    def load(self, session_id: str) -> NativeSession:
        path = self._find_path(session_id)
        if path is not None:
            return self._load_file(path)
        raise FileNotFoundError(f"Pi session not found: {session_id}")

    def load_path(self, path: Path) -> NativeSession:
        return self._load_file(path)

    def _find_path(self, session_id: str) -> Path | None:
        index = self._id_index_cache()
        if session_id in index:
            return index[session_id]
        for path in self._session_paths():
            if session_id in path.name:
                return path
        return None

    def destination_path(self, session_id: str, timestamp: str, cwd: str) -> Path:
        filename = f"{pi_filename_timestamp(timestamp)}_{session_id}.jsonl"
        return self._active_session_root() / encode_pi_cwd(cwd) / filename

    def write(self, path: Path, records: list[JsonObject], *, overwrite: bool = False) -> None:
        JsonlFile(path).write(records, overwrite=overwrite)

    def _session_paths(self) -> list[Path]:
        if self._path_cache is not None:
            return self._path_cache
        root = self._active_session_root()
        if not root.exists():
            self._path_cache = []
            return []
        paths = sorted(path for path in root.rglob("*.jsonl") if "\\tasks\\" not in str(path))
        self._path_cache = paths
        return paths

    def _id_index_cache(self) -> dict[str, Path]:
        if self._id_index is not None:
            return self._id_index
        index: dict[str, Path] = {}
        for path in self._session_paths():
            stem = path.stem
            if "_" in stem:
                sid = stem.rsplit("_", 1)[-1]
                if sid:
                    index[sid] = path
        self._id_index = index
        return index

    def _active_session_root(self) -> Path:
        return self._session_dir or (self._pi_agent_home / "sessions")

    def _load_file(self, path: Path) -> NativeSession:
        records = JsonlFile(path).read()
        header = records[0] if records else {}
        session_id = string_value(header, "id") or path.stem.rsplit("_", 1)[-1]
        cwd = string_value(header, "cwd") or ""
        timestamp = string_value(header, "timestamp") or ""
        return NativeSession("pi", session_id, cwd, timestamp, path, records)

    def _safe_load_file(self, path: Path) -> NativeSession | None:
        try:
            return self._load_file(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"warning: skipped unreadable Pi session {path}: {exc}", file=sys.stderr)
            return None

    def _safe_summary(self, path: Path) -> SessionSummary | None:
        try:
            metadata = self._safe_metadata(path)
            if metadata is None:
                return None
            message_count = 0
            with path.open("rb") as handle:
                for line in handle:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    value = _json_loads(stripped)
                    if isinstance(value, dict) and value.get("type") == "message":
                        message_count += 1
            return SessionSummary("pi", metadata.session_id, metadata.cwd, metadata.timestamp, path, message_count)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"warning: skipped unreadable Pi session {path}: {exc}", file=sys.stderr)
            return None

    def _safe_metadata(self, path: Path) -> SessionSummary | None:
        try:
            with path.open("rb") as handle:
                for line in handle:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    value = _json_loads(stripped)
                    if not isinstance(value, dict) or value.get("type") != "session":
                        return None
                    session_id = string_value(value, "id") or path.stem.rsplit("_", 1)[-1]
                    cwd = string_value(value, "cwd") or ""
                    timestamp = string_value(value, "timestamp") or ""
                    return SessionSummary("pi", session_id, cwd, timestamp, path, -1)
            return None
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"warning: skipped unreadable Pi session {path}: {exc}", file=sys.stderr)
            return None

    @staticmethod
    def _message_count(records: list[JsonObject]) -> int:
        return sum(1 for record in records if record.get("type") == "message")


class ClaudeStore(SessionStore):
    provider_name = "claude"

    def __init__(self, claude_home: Path, session_dir: Path | None = None) -> None:
        self._claude_home = claude_home
        self._session_dir = session_dir
        self._path_cache: list[Path] | None = None
        self._id_index: dict[str, Path] | None = None

    @property
    def root(self) -> Path:
        return self._claude_home

    def list(self, *, workers: int = 1) -> list[SessionSummary]:
        paths = self._session_paths()
        if workers <= 1 or len(paths) <= 1:
            return [s for path in paths if (s := self._cached(self._safe_summary, path)) is not None]
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(workers, len(paths)), thread_name_prefix="cc-list") as executor:
            results = list(executor.map(lambda p: self._cached(self._safe_summary, p), paths))
        return [s for s in results if s is not None]

    def list_metadata(self, *, workers: int = 1) -> list[SessionSummary]:
        return self.list(workers=workers)

    def load(self, session_id: str) -> NativeSession:
        path = self._find_path(session_id)
        if path is not None:
            return self._load_file(path)
        raise FileNotFoundError(f"Claude session not found: {session_id}")

    def load_path(self, path: Path) -> NativeSession:
        return self._load_file(path)

    def _find_path(self, session_id: str) -> Path | None:
        index = self._id_index_cache()
        if session_id in index:
            return index[session_id]
        for path in self._session_paths():
            if session_id in path.name:
                return path
        return None

    def destination_path(self, session_id: str, cwd: str) -> Path:
        return self._active_session_root() / sanitize_claude_cwd(cwd) / f"{session_id}.jsonl"

    def write(self, path: Path, records: list[JsonObject], *, overwrite: bool = False) -> None:
        JsonlFile(path).write(records, overwrite=overwrite)

    def _session_paths(self) -> list[Path]:
        if self._path_cache is not None:
            return self._path_cache
        root = self._active_session_root()
        if not root.exists():
            self._path_cache = []
            return []
        paths = sorted(p for p in root.rglob("*.jsonl") if "/subagents/" not in str(p).replace("\\", "/") and "/tool-results/" not in str(p).replace("\\", "/"))
        self._path_cache = paths
        return paths

    def _id_index_cache(self) -> dict[str, Path]:
        if self._id_index is not None:
            return self._id_index
        index: dict[str, Path] = {}
        for path in self._session_paths():
            stem = path.stem
            if len(stem) == 36 and stem.count("-") == 4:
                index[stem] = path
        self._id_index = index
        return index

    def _active_session_root(self) -> Path:
        return self._session_dir or (self._claude_home / "projects")

    def _load_file(self, path: Path) -> NativeSession:
        records = JsonlFile(path).read()
        session_id = path.stem
        cwd = ""
        timestamp = ""
        for record in records:
            if record.get("type") in ("user", "assistant", "system", "attachment"):
                session_id = string_value(record, "sessionId") or session_id
                cwd = string_value(record, "cwd") or cwd
                timestamp = string_value(record, "timestamp") or timestamp
                break
        return NativeSession("claude", session_id, cwd, timestamp, path, records)

    def _safe_summary(self, path: Path) -> SessionSummary | None:
        try:
            session_id = path.stem
            cwd = ""
            timestamp = ""
            message_count = 0
            with path.open("rb") as handle:
                for line in handle:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    value = _json_loads(stripped)
                    if not isinstance(value, dict):
                        continue
                    rtype = value.get("type")
                    if rtype in ("user", "assistant"):
                        if not cwd:
                            cwd = value.get("cwd", "")
                        if not timestamp:
                            timestamp = value.get("timestamp", "")
                        session_id = value.get("sessionId", session_id)
                        message_count += 1
                    elif rtype == "system":
                        if not cwd:
                            cwd = value.get("cwd", "")
                        if not timestamp:
                            timestamp = value.get("timestamp", "")
            return SessionSummary("claude", session_id, cwd, timestamp, path, message_count)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"warning: skipped unreadable Claude session {path}: {exc}", file=sys.stderr)
            return None


class DevinStore(SessionStore):
    provider_name = "devin"

    def __init__(self, devin_home: Path, session_dir: Path | None = None) -> None:
        self._devin_home = devin_home
        self._session_dir = session_dir
        self._path_cache: list[Path] | None = None
        self._id_index: dict[str, Path] | None = None
        self._db_cache: dict[str, dict[str, object]] | None = None

    @property
    def root(self) -> Path:
        return self._devin_home

    def list(self, *, workers: int = 1) -> list[SessionSummary]:
        rows = self._db_rows()
        if rows:
            result: list[SessionSummary] = []
            for row in rows.values():
                sid = str(row["id"])
                cwd = str(row["working_directory"])
                created = int(row["created_at"])
                timestamp = epoch_ms_to_iso(created * 1000)
                transcript_path = self._transcript_path(sid)
                message_count = -1
                if transcript_path.exists():
                    message_count = self._count_transcript_messages(transcript_path)
                result.append(SessionSummary("devin", sid, cwd, timestamp, transcript_path, message_count))
            result.sort(key=lambda s: s.timestamp, reverse=True)
            return result
        # No DB available -- fall back to scanning transcript files directly
        paths = self._session_paths()
        return [s for path in paths if (s := self._cached(self._safe_summary, path)) is not None]

    def list_metadata(self, *, workers: int = 1) -> list[SessionSummary]:
        return self.list(workers=workers)

    def load(self, session_id: str) -> NativeSession:
        path = self._find_path(session_id)
        if path is not None:
            return self._load_file(path)
        raise FileNotFoundError(f"Devin session not found: {session_id}")

    def load_path(self, path: Path) -> NativeSession:
        return self._load_file(path)

    def _find_path(self, session_id: str) -> Path | None:
        index = self._id_index_cache()
        if session_id in index:
            return index[session_id]
        for path in self._session_paths():
            if session_id in path.stem:
                return path
        return None

    def destination_path(self, session_id: str) -> Path:
        return self._active_session_root() / f"{session_id}.json"

    def write(self, path: Path, records: list[JsonObject], *, overwrite: bool = False) -> None:
        if path.exists() and not overwrite:
            raise FileExistsError(f"Refusing to overwrite existing file: {path}")
        if not records:
            raise ValueError("Devin export requires a JSON payload")
        path.parent.mkdir(parents=True, exist_ok=True)
        if _HAS_ORJSON:
            path.write_bytes(orjson.dumps(records[0], option=orjson.OPT_INDENT_2))
        else:
            path.write_text(json.dumps(records[0], indent=2), encoding="utf-8")

    def _session_paths(self) -> list[Path]:
        if self._path_cache is not None:
            return self._path_cache
        root = self._transcript_root()
        if not root.exists():
            self._path_cache = []
            return []
        paths = sorted(root.glob("*.json"))
        self._path_cache = paths
        return paths

    def _id_index_cache(self) -> dict[str, Path]:
        if self._id_index is not None:
            return self._id_index
        index: dict[str, Path] = {}
        for path in self._session_paths():
            index[path.stem] = path
        self._id_index = index
        return index

    def _active_session_root(self) -> Path:
        return self._session_dir or (self._devin_home / "cli" / "transcripts")

    def _transcript_root(self) -> Path:
        return self._session_dir or (self._devin_home / "cli" / "transcripts")

    def _transcript_path(self, session_id: str) -> Path:
        return self._transcript_root() / f"{session_id}.json"

    def _db_path(self) -> Path:
        return self._devin_home / "cli" / "sessions.db"

    def _db_rows(self) -> dict[str, dict[str, object]]:
        if self._db_cache is not None:
            return self._db_cache
        db_path = self._db_path()
        if not db_path.exists():
            self._db_cache = {}
            return self._db_cache
        conn = _sqlite3.connect(str(db_path))
        conn.row_factory = _sqlite3.Row
        try:
            cursor = conn.execute(
                "SELECT id, working_directory, model, agent_mode, created_at, last_activity_at, title "
                "FROM sessions ORDER BY last_activity_at DESC"
            )
            rows: dict[str, dict[str, object]] = {}
            for row in cursor:
                rows[str(row["id"])] = dict(row)
            self._db_cache = rows
            return rows
        finally:
            conn.close()

    def _db_row(self, session_id: str) -> dict[str, object] | None:
        rows = self._db_rows()
        return rows.get(session_id)

    def _load_file(self, path: Path) -> NativeSession:
        if _HAS_ORJSON:
            data = orjson.loads(path.read_bytes())
        else:
            data = json.loads(path.read_text(encoding="utf-8"))
        transcript = as_object(data)
        if transcript is None:
            raise ValueError(f"{path} is not a JSON object")
        session_id = string_value(transcript, "session_id") or path.stem
        agent = as_object(transcript.get("agent")) or {}
        extra = as_object(agent.get("extra")) or {}
        cwd = string_value(extra, "cwd") or ""
        steps = as_list(transcript.get("steps")) or []
        first_ts = ""
        if steps:
            first_step = as_object(steps[0])
            if first_step is not None:
                first_ts = string_value(first_step, "timestamp") or ""
        if not cwd or not first_ts:
            db_row = self._db_row(session_id)
            if db_row:
                if not cwd:
                    cwd = str(db_row.get("working_directory", ""))
                if not first_ts:
                    created = db_row.get("created_at")
                    if isinstance(created, int):
                        first_ts = epoch_ms_to_iso(created * 1000)
        return NativeSession("devin", session_id, cwd, first_ts, path, [transcript])

    def _safe_summary(self, path: Path) -> SessionSummary | None:
        try:
            if _HAS_ORJSON:
                data = orjson.loads(path.read_bytes())
            else:
                data = json.loads(path.read_text(encoding="utf-8"))
            transcript = as_object(data)
            if transcript is None:
                return None
            session_id = string_value(transcript, "session_id") or path.stem
            agent = as_object(transcript.get("agent")) or {}
            extra = as_object(agent.get("extra")) or {}
            cwd = string_value(extra, "cwd") or ""
            steps = as_list(transcript.get("steps")) or []
            first_ts = ""
            if steps:
                first_step = as_object(steps[0])
                if first_step is not None:
                    first_ts = string_value(first_step, "timestamp") or ""
            if not cwd or not first_ts:
                db_row = self._db_row(session_id)
                if db_row:
                    if not cwd:
                        cwd = str(db_row.get("working_directory", ""))
                    if not first_ts:
                        created = db_row.get("created_at")
                        if isinstance(created, int):
                            first_ts = epoch_ms_to_iso(created * 1000)
            message_count = self._count_transcript_messages_from_obj(transcript)
            return SessionSummary("devin", session_id, cwd, first_ts, path, message_count)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"warning: skipped unreadable Devin transcript {path}: {exc}", file=sys.stderr)
            return None

    @staticmethod
    def _count_transcript_messages(path: Path) -> int:
        try:
            if _HAS_ORJSON:
                data = orjson.loads(path.read_bytes())
            else:
                data = json.loads(path.read_text(encoding="utf-8"))
            transcript = as_object(data)
            if transcript is None:
                return 0
            return DevinStore._count_transcript_messages_from_obj(transcript)
        except (OSError, ValueError, json.JSONDecodeError):
            return 0

    @staticmethod
    def _count_transcript_messages_from_obj(transcript: JsonObject) -> int:
        steps = as_list(transcript.get("steps")) or []
        return sum(
            1 for step in steps
            if isinstance(step, dict) and step.get("source") in ("user", "agent")
        )


class FactoryStore(SessionStore):
    provider_name = "factory"

    def __init__(self, factory_home: Path, session_dir: Path | None = None) -> None:
        self._factory_home = factory_home
        self._session_dir = session_dir
        self._path_cache: list[Path] | None = None
        self._id_index: dict[str, Path] | None = None

    @property
    def root(self) -> Path:
        return self._factory_home

    def list(self, *, workers: int = 1) -> list[SessionSummary]:
        paths = self._session_paths()
        if workers <= 1 or len(paths) <= 1:
            return [s for path in paths if (s := self._cached(self._safe_summary, path)) is not None]
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(workers, len(paths)), thread_name_prefix="factory-list") as executor:
            results = list(executor.map(lambda p: self._cached(self._safe_summary, p), paths))
        return [s for s in results if s is not None]

    def list_metadata(self, *, workers: int = 1) -> list[SessionSummary]:
        return self.list(workers=workers)

    def load(self, session_id: str) -> NativeSession:
        path = self._find_path(session_id)
        if path is not None:
            return self._load_file(path)
        raise FileNotFoundError(f"Factory session not found: {session_id}")

    def load_path(self, path: Path) -> NativeSession:
        return self._load_file(path)

    def _find_path(self, session_id: str) -> Path | None:
        index = self._id_index_cache()
        if session_id in index:
            return index[session_id]
        for path in self._session_paths():
            if session_id in path.stem:
                return path
        return None

    def destination_path(self, session_id: str, cwd: str) -> Path:
        return self._active_session_root() / sanitize_claude_cwd(cwd) / f"{session_id}.jsonl"

    def write(self, path: Path, records: list[JsonObject], *, overwrite: bool = False) -> None:
        JsonlFile(path).write(records, overwrite=overwrite)

    def _session_paths(self) -> list[Path]:
        if self._path_cache is not None:
            return self._path_cache
        root = self._active_session_root()
        if not root.exists():
            self._path_cache = []
            return []
        paths = sorted(p for p in root.rglob("*.jsonl"))
        self._path_cache = paths
        return paths

    def _id_index_cache(self) -> dict[str, Path]:
        if self._id_index is not None:
            return self._id_index
        index: dict[str, Path] = {}
        for path in self._session_paths():
            stem = path.stem
            if len(stem) == 36 and stem.count("-") == 4:
                index[stem] = path
        self._id_index = index
        return index

    def _active_session_root(self) -> Path:
        return self._session_dir or (self._factory_home / "sessions")

    def _load_file(self, path: Path) -> NativeSession:
        records = JsonlFile(path).read()
        session_id = path.stem
        cwd = ""
        timestamp = ""
        for record in records:
            if record.get("type") == "session_start":
                session_id = string_value(record, "id") or session_id
                cwd = string_value(record, "cwd") or cwd
                timestamp = string_value(record, "timestamp") or ""
                break
        if not timestamp:
            for record in records:
                if record.get("type") == "message":
                    timestamp = string_value(record, "timestamp") or ""
                    break
        return NativeSession("factory", session_id, cwd, timestamp, path, records)

    def _safe_summary(self, path: Path) -> SessionSummary | None:
        try:
            session_id = path.stem
            cwd = ""
            timestamp = ""
            message_count = 0
            with path.open("rb") as handle:
                for line in handle:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    value = _json_loads(stripped)
                    if not isinstance(value, dict):
                        continue
                    rtype = value.get("type")
                    if rtype == "session_start":
                        session_id = value.get("id", session_id)
                        cwd = value.get("cwd", cwd)
                        timestamp = value.get("timestamp", timestamp)
                    elif rtype == "message":
                        if not timestamp:
                            timestamp = value.get("timestamp", timestamp)
                        message_count += 1
            return SessionSummary("factory", session_id, cwd, timestamp, path, message_count)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"warning: skipped unreadable Factory session {path}: {exc}", file=sys.stderr)
            return None


class WindsurfStore(SessionStore):
    provider_name = "windsurf"

    _ENCRYPTION_KEY = b"safeCodeiumworldKeYsecretBalloon"
    _NONCE_SIZE = 12

    def __init__(self, windsurf_home: Path, session_dir: Path | None = None) -> None:
        self._windsurf_home = windsurf_home
        self._session_dir = session_dir
        self._path_cache: list[Path] | None = None
        self._id_index: dict[str, Path] | None = None

    @property
    def root(self) -> Path:
        return self._windsurf_home

    def list(self, *, workers: int = 1) -> list[SessionSummary]:
        paths = self._session_paths()
        if workers <= 1 or len(paths) <= 1:
            return [s for path in paths if (s := self._cached(self._safe_summary, path)) is not None]
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(workers, len(paths)), thread_name_prefix="ws-list") as executor:
            results = list(executor.map(lambda p: self._cached(self._safe_summary, p), paths))
        return [s for s in results if s is not None]

    def list_metadata(self, *, workers: int = 1) -> list[SessionSummary]:
        return self.list(workers=workers)

    def load(self, session_id: str) -> NativeSession:
        path = self._find_path(session_id)
        if path is not None:
            return self._load_file(path)
        raise FileNotFoundError(f"Windsurf session not found: {session_id}")

    def load_path(self, path: Path) -> NativeSession:
        return self._load_file(path)

    def _find_path(self, session_id: str) -> Path | None:
        index = self._id_index_cache()
        if session_id in index:
            return index[session_id]
        for path in self._session_paths():
            if session_id in path.stem:
                return path
        return None

    def destination_path(self, session_id: str) -> Path:
        return self._active_session_root() / f"{session_id}.pb"

    def write(self, path: Path, records: list[JsonObject], *, overwrite: bool = False) -> None:
        if path.exists() and not overwrite:
            raise FileExistsError(f"Refusing to overwrite existing file: {path}")
        if not records:
            raise ValueError("Windsurf export requires a protobuf payload")
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = records[0]
        if isinstance(payload, bytes):
            path.write_bytes(payload)
        else:
            raise ValueError("Windsurf write requires bytes payload in records[0]")

    def _session_paths(self) -> list[Path]:
        if self._path_cache is not None:
            return self._path_cache
        root = self._active_session_root()
        if not root.exists():
            self._path_cache = []
            return []
        paths = sorted(root.glob("*.pb"))
        self._path_cache = paths
        return paths

    def _id_index_cache(self) -> dict[str, Path]:
        if self._id_index is not None:
            return self._id_index
        index: dict[str, Path] = {}
        from session_sdk.windsurf_pb import parse_trajectory
        for path in self._session_paths():
            # Index by cascade_id (filename stem)
            index[path.stem] = path
            # Also index by trajectory_id (inside protobuf)
            try:
                plaintext = self._decrypt(path)
                traj = parse_trajectory(plaintext)
                traj_id = str(traj.get("trajectory_id") or "")
                if traj_id:
                    index[traj_id] = path
            except Exception:
                pass
        self._id_index = index
        return index

    def _active_session_root(self) -> Path:
        return self._session_dir or (self._windsurf_home / "cascade")

    def _decrypt(self, path: Path) -> bytes:
        """Decrypt a .pb file and return plaintext protobuf bytes."""
        data = path.read_bytes()
        if len(data) < self._NONCE_SIZE + 16:
            raise ValueError(f"{path} too small ({len(data)} bytes)")
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError as exc:
            raise ImportError("cryptography package required for Windsurf session decryption") from exc
        nonce = data[:self._NONCE_SIZE]
        ciphertext = data[self._NONCE_SIZE:]
        return AESGCM(self._ENCRYPTION_KEY).decrypt(nonce, ciphertext, None)

    def _load_file(self, path: Path) -> NativeSession:
        from session_sdk.windsurf_pb import parse_trajectory
        plaintext = self._decrypt(path)
        traj = parse_trajectory(plaintext)
        session_id = str(traj.get("trajectory_id") or path.stem)
        cascade_id = str(traj.get("cascade_id") or "")
        steps = traj.get("steps") or []
        cwd = self._workspace_to_cwd(traj.get("workspace_uri")) or self._extract_cwd(steps)
        timestamp = self._extract_timestamp(steps) or ""
        return NativeSession("windsurf", session_id, cwd, timestamp, path, [{"_plaintext": plaintext, "_cascade_id": cascade_id}])

    def _safe_summary(self, path: Path) -> SessionSummary | None:
        try:
            from session_sdk.windsurf_pb import parse_trajectory, parse_step, VARIANT_USER_INPUT, VARIANT_PLANNER_RESPONSE
            plaintext = self._decrypt(path)
            traj = parse_trajectory(plaintext)
            session_id = str(traj.get("trajectory_id") or path.stem)
            steps = traj.get("steps") or []
            cwd = self._workspace_to_cwd(traj.get("workspace_uri")) or self._extract_cwd(steps)
            timestamp = self._extract_timestamp(steps) or ""
            message_count = sum(
                1 for step_buf in steps
                if isinstance(step_buf, (bytes, bytearray))
                and parse_step(step_buf)["variant_field"] in (VARIANT_USER_INPUT, VARIANT_PLANNER_RESPONSE)
            )
            return SessionSummary("windsurf", session_id, cwd, timestamp, path, message_count)
        except (OSError, ValueError, ImportError) as exc:
            print(f"warning: skipped unreadable Windsurf session {path}: {exc}", file=sys.stderr)
            return None

    @staticmethod
    def _workspace_to_cwd(uri: str | None) -> str:
        """Convert a Windsurf workspace_uri (file:// URI) to a filesystem path.

        Windsurf stores workspace scope as a file:// URI, e.g.
        ``file:///c:/Users/win/Desktop/myproject``.  This converts it to a
        native Windows path ``C:\\Users\\win\\Desktop\\myproject``.

        Some workspace URIs point to a specific file rather than a directory
        (e.g. ``file:///c:/Users/win/Desktop/myproject/script.py``).  In that
        case we return the parent directory.
        """
        if not uri:
            return ""
        from urllib.parse import urlparse, unquote
        import os.path
        parsed = urlparse(uri)
        if parsed.scheme != "file":
            return ""
        path = unquote(parsed.path)
        # On Windows, path looks like /c:/Users/win/Desktop/myproject
        if len(path) >= 2 and path[0] == "/" and path[2] == ":":
            path = path[1:]  # strip leading / before drive letter
        # Convert forward slashes to OS-native separators
        path = path.replace("/", "\\")
        # Normalize drive letter to uppercase (file:// URIs use lowercase)
        if len(path) >= 2 and path[1] == ":" and path[0].isalpha():
            path = path[0].upper() + path[1:]
        # If the URI points to a file (has an extension), use the parent dir
        if os.path.splitext(path)[1]:
            path = os.path.dirname(path)
        return path

    @staticmethod
    def _extract_cwd(steps: list[object]) -> str:
        """Extract cwd from user_input steps that mention a real file path.

        This is a fallback used when the trajectory's TrajectoryScope
        (field 7, workspace_uri) is not available.  It collects all cwd
        candidates from PS prompts and cd commands across all user_input
        steps, then returns the most frequently occurring one.
        """
        from session_sdk.windsurf_pb import parse_step, iter_fields, VARIANT_USER_INPUT
        import re
        from collections import Counter
        candidates: list[str] = []
        for step_buf in steps:
            if not isinstance(step_buf, (bytes, bytearray)):
                continue
            step = parse_step(step_buf)
            vf = step["variant_field"]
            vdata = step["variant_data"]
            if vf == VARIANT_USER_INPUT and vdata:
                text = ""
                for sfno, swt, _off, sval in iter_fields(vdata):
                    if sfno == 2 and swt == 2 and isinstance(sval, (bytes, bytearray)):
                        text = sval.decode("utf-8", errors="replace")
                        break
                # Skip context injection / system prompt text
                if text.startswith("You are a tool-calling assistant"):
                    continue
                m = re.search(r'(?:PS )?([A-Za-z]:\\[^\s>]+)>', text)
                if m:
                    candidates.append(m.group(1))
                    continue
                m = re.search(r'cd\s+[\'"]?([^\s\'"]+)', text)
                if m and m.group(1) != "<directory>":
                    candidates.append(m.group(1))
        if not candidates:
            return ""
        # Return the most common cwd (where the bulk of work happened)
        return Counter(candidates).most_common(1)[0][0]

    @staticmethod
    def _extract_timestamp(steps: list[object]) -> str:
        """Extract ISO timestamp from the first step metadata."""
        from session_sdk.windsurf_pb import parse_step_timestamp
        from session_sdk.paths import epoch_ms_to_iso
        for step_buf in steps:
            if not isinstance(step_buf, (bytes, bytearray)):
                continue
            ts_seconds = parse_step_timestamp(step_buf)
            if ts_seconds is not None:
                return epoch_ms_to_iso(ts_seconds * 1000)
        return ""


class GrokStore(SessionStore):
    """Filesystem store for Grok Build sessions.

    Grok Build (the ``grok`` CLI) stores each session under
    ``{grok_home}/sessions/{url_encoded_cwd}/{session_id}/`` with
    ``summary.json`` holding metadata (info.id, info.cwd, timestamps,
    model id, message counts) and ``updates.jsonl`` holding the
    authoritative ACP session update stream: one envelope per line with
    ``{timestamp, method: "session/update", params: {sessionId, update}}``.
    """

    provider_name = "grok"

    def __init__(self, grok_home: Path, session_dir: Path | None = None) -> None:
        self._grok_home = grok_home
        self._session_dir = session_dir
        self._path_cache: list[Path] | None = None
        self._id_index: dict[str, Path] | None = None

    @property
    def root(self) -> Path:
        return self._grok_home

    def list(self, *, workers: int = 1) -> list[SessionSummary]:
        paths = self._session_paths()
        if workers <= 1 or len(paths) <= 1:
            return [s for path in paths if (s := self._cached(self._safe_summary, path)) is not None]
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(workers, len(paths)), thread_name_prefix="grok-list") as executor:
            results = list(executor.map(lambda p: self._cached(self._safe_summary, p), paths))
        return [s for s in results if s is not None]

    def list_metadata(self, *, workers: int = 1) -> list[SessionSummary]:
        return self.list(workers=workers)

    def load(self, session_id: str) -> NativeSession:
        path = self._find_path(session_id)
        if path is not None:
            return self._load_file(path)
        raise FileNotFoundError(f"Grok session not found: {session_id}")

    def load_path(self, path: Path) -> NativeSession:
        return self._load_file(path)

    def _find_path(self, session_id: str) -> Path | None:
        index = self._id_index_cache()
        if session_id in index:
            return index[session_id]
        for path in self._session_paths():
            if session_id in path.name:
                return path
        return None

    def destination_path(self, session_id: str, cwd: str) -> Path:
        group = encode_grok_cwd_dirname(cwd)
        return self._active_session_root() / group / session_id

    def write(self, path: Path, records: list[JsonObject], *, overwrite: bool = False) -> None:
        """Write a Grok session directory: updates.jsonl + summary.json.

        ``records`` must be list of envelope dicts (already shaped as
        ``{"timestamp", "method", "params"}``); the builder produces them.
        A ``summary.json`` is derived from the first envelope's session id
        plus any ``{"_summary": {...}}`` metadata record the builder emits.
        """
        session_dir = Path(path)
        updates_file = session_dir / "updates.jsonl"
        if updates_file.exists() and not overwrite:
            raise FileExistsError(f"destination exists: {updates_file}")
        session_dir.mkdir(parents=True, exist_ok=True)

        summary: dict[str, object] = {}
        envelopes: list[JsonObject] = []
        for record in records:
            if isinstance(record, dict) and "_summary" in record:
                summary = dict(as_object(record.get("_summary")) or {})
            else:
                envelopes.append(record)
        if not summary:
            summary = _derive_grok_summary(envelopes, session_dir)

        JsonlFile(updates_file).write(envelopes, overwrite=True)
        summary_path = session_dir / "summary.json"
        if summary_path.exists() and not overwrite:
            raise FileExistsError(f"destination exists: {summary_path}")
        summary_path.write_text(_json_dumps_pretty(summary), encoding="utf-8")
        # Long-path groups record the original cwd so decode is reversible.
        group_dir = session_dir.parent
        if group_dir.name != encode_grok_cwd_dirname(str(summary.get("cwd") or "")):
            cwd_file = group_dir / ".cwd"
            if not cwd_file.exists():
                cwd = summary.get("cwd")
                if cwd:
                    cwd_file.write_text(str(cwd), encoding="utf-8")

    def _session_paths(self) -> list[Path]:
        if self._path_cache is not None:
            return self._path_cache
        root = self._active_session_root()
        if not root.exists():
            self._path_cache = []
            return []
        dirs: list[Path] = []
        for group in sorted(p for p in root.iterdir() if p.is_dir()):
            if (group / "updates.jsonl").is_file() or (group / "summary.json").is_file():
                # Empty-cwd export: the session dir collapsed into the group
                # dir (no encoded-cwd group nesting).
                dirs.append(group)
                continue
            for session_dir in sorted(p for p in group.iterdir() if p.is_dir()):
                if (session_dir / "updates.jsonl").is_file() or (session_dir / "summary.json").is_file():
                    dirs.append(session_dir)
        self._path_cache = dirs
        return dirs

    def _id_index_cache(self) -> dict[str, Path]:
        if self._id_index is not None:
            return self._id_index
        index: dict[str, Path] = {}
        for path in self._session_paths():
            name = path.name
            if name and name not in index:
                index[name] = path
        self._id_index = index
        return index

    def _active_session_root(self) -> Path:
        return self._session_dir or (self._grok_home / "sessions")

    def _load_file(self, path: Path) -> NativeSession:
        summary = _read_grok_summary(path)
        session_id = str(summary.get("id") or path.name)
        cwd = str(summary.get("cwd") or decode_grok_cwd_dirname(path.parent.name, path.parent) or "")
        timestamp = _summary_timestamp_to_iso(summary) or ""
        records = JsonlFile(path / "updates.jsonl").read() if (path / "updates.jsonl").is_file() else []
        return NativeSession("grok", session_id, cwd, timestamp, path, records)

    def _safe_summary(self, path: Path) -> SessionSummary | None:
        try:
            summary = _read_grok_summary(path)
            session_id = str(summary.get("id") or path.name)
            cwd = str(summary.get("cwd") or decode_grok_cwd_dirname(path.parent.name, path.parent) or "")
            timestamp = _summary_timestamp_to_iso(summary) or ""
            num_messages = summary.get("num_messages")
            message_count = int(num_messages) if isinstance(num_messages, int) else -1
            return SessionSummary("grok", session_id, cwd, timestamp, path, message_count)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"warning: skipped unreadable Grok session {path}: {exc}", file=sys.stderr)
            return None


def _json_dumps_pretty(value: object) -> str:
    if _HAS_ORJSON:
        return orjson.dumps(value, option=orjson.OPT_INDENT_2).decode("utf-8")
    return json.dumps(value, indent=2)


def _read_grok_summary(session_dir: Path) -> dict[str, object]:
    """Read and flatten a Grok summary.json into {id, cwd, title, ...}."""
    summary_path = session_dir / "summary.json"
    if not summary_path.is_file():
        return {}
    raw = _json_loads(summary_path.read_bytes())
    if not isinstance(raw, dict):
        return {}
    info = as_object(raw.get("info")) or {}
    created = raw.get("created_at")
    updated = raw.get("updated_at")
    return {
        "id": string_value(info, "id"),
        "cwd": string_value(info, "cwd"),
        "title": string_value(raw, "generated_title") or string_value(raw, "session_summary"),
        "created_at": created,
        "updated_at": updated,
        "num_messages": raw.get("num_messages"),
        "num_chat_messages": raw.get("num_chat_messages"),
        "current_model_id": string_value(raw, "current_model_id"),
        "parent_session_id": string_value(raw, "parent_session_id"),
        "agent_name": string_value(raw, "agent_name"),
    }


def _summary_timestamp_to_iso(summary: dict[str, object]) -> str:
    value = summary.get("created_at") or summary.get("updated_at")
    if isinstance(value, str) and value:
        return value
    if isinstance(value, (int, float)):
        return epoch_ms_to_iso(int(value) * 1000 if value < 10**12 else int(value))
    return ""


def _derive_grok_summary(envelopes: list[JsonObject], session_dir: Path) -> dict[str, object]:
    """Derive a minimal summary.json when the builder did not supply one."""
    session_id = session_dir.name
    cwd = decode_grok_cwd_dirname(session_dir.parent.name, session_dir.parent)
    title = ""
    model_id = ""
    count = 0
    for record in envelopes:
        if not isinstance(record, dict):
            continue
        params = as_object(record.get("params")) or {}
        sid = string_value(params, "sessionId")
        if sid:
            session_id = sid
        update = as_object(params.get("update")) or {}
        utype = string_value(update, "sessionUpdate")
        if utype in ("user_message_chunk", "agent_message_chunk", "agent_thought_chunk"):
            count += 1
        elif utype == "session_info_update":
            title = string_value(update, "title") or title
        meta = as_object(record.get("_meta")) or {}
        if not model_id:
            model_id = string_value(meta, "modelId") or string_value(meta, "model_id")
    return {
        "id": session_id,
        "cwd": cwd,
        "title": title,
        "created_at": "",
        "updated_at": "",
        "num_messages": count,
        "num_chat_messages": count,
        "current_model_id": model_id,
        "parent_session_id": "",
        "agent_name": "",
    }


class FreebuffStore(SessionStore):
    """Filesystem store for Freebuff Desktop sessions.

    Freebuff Desktop (freebuff.com, by CodebuffAI) stores each project
    under ``{home}/projects/<slug>-<project-id>/`` with a ``project.json``
    header and a SQLite database (``desktop-v2.db``) holding:

    - ``threads``: session headers (id, project_path = cwd, title, status,
      model, created_at/updated_at epoch-ms, fork_source_thread_id, ...)
    - ``messages``: one row per message (seq autoincrement, thread_id,
      role, parts_json array, ts epoch-ms)

    Message ``parts_json`` entries are kind-tagged:
    ``text`` (chat text), ``reasoning`` (thinking), ``tool``
    (tool invocation), ``changes`` (file diffs), ``ad`` (sponsored).  Only
    ``text`` parts carry the conversation; the rest are skipped by the
    text-history extractor.
    """

    provider_name = "freebuff"
    _DB_NAME = "desktop-v2.db"

    def __init__(self, freebuff_home: Path, session_dir: Path | None = None) -> None:
        self._freebuff_home = freebuff_home
        self._session_dir = session_dir
        self._path_cache: list[Path] | None = None
        self._id_index: dict[str, Path] | None = None

    @property
    def root(self) -> Path:
        return self._freebuff_home

    def list(self, *, workers: int = 1) -> list[SessionSummary]:
        paths = self._session_paths()
        if workers <= 1 or len(paths) <= 1:
            return [s for path in paths for s in self._cached(self._summaries_from_db, path)]
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(workers, len(paths)), thread_name_prefix="fb-list") as executor:
            results = list(executor.map(lambda p: self._cached(self._summaries_from_db, p), paths))
        return [s for batch in results for s in batch]

    def list_metadata(self, *, workers: int = 1) -> list[SessionSummary]:
        return self.list(workers=workers)

    def load(self, session_id: str) -> NativeSession:
        path = self._find_path(session_id)
        if path is not None:
            return self._load_file(path, session_id)
        raise FileNotFoundError(f"Freebuff session not found: {session_id}")

    def load_path(self, path: Path) -> NativeSession:
        # path may point at a db file or a session thread id is unknown here;
        # resolve by scanning the db's first thread.
        return self._load_file(path, None)

    def _find_path(self, session_id: str) -> Path | None:
        index = self._id_index_cache()
        if session_id in index:
            return index[session_id]
        return None

    def destination_path(self, session_id: str, cwd: str) -> Path:
        """Deterministic destination: {projects}/<slug(cwd)>-<project-id>/desktop-v2.db.

        Freebuff Desktop resolves a project's thread database through the
        project root's ``.freebuff/project-id`` file: when present, the app
        reads ``projects/<slug>-<project-id>/desktop-v2.db`` and anything
        written anywhere else is invisible to it.  Fall back to a
        session-id suffix when the project root does not declare an id.
        """
        from session_sdk.paths import opencode_slug
        from pathlib import PurePath
        base = PurePath(cwd).name if cwd else "imported"
        slug = opencode_slug(base)
        project_id = None
        if cwd:
            pid_file = Path(cwd) / ".freebuff" / "project-id"
            try:
                if pid_file.is_file():
                    project_id = pid_file.read_text(encoding="utf-8").strip()
            except OSError:
                pass
        if project_id:
            return self._active_session_root() / f"{slug}-{project_id}" / self._DB_NAME
        return self._active_session_root() / f"{slug}-{session_id[:8]}" / self._DB_NAME

    def write(self, path: Path, records: list[JsonObject], *, overwrite: bool = False) -> None:
        """Write a Freebuff project DB from records.

        ``records`` is a list of message dicts (``{"seq", "role", "parts",
        "ts"}``); a leading ``{"_thread": {...}}`` record carries thread
        metadata (id, project_path, title, model, created_at, updated_at).
        """
        db_path = Path(path)
        if db_path.exists() and not overwrite:
            raise FileExistsError(f"destination exists: {db_path}")
        db_path.parent.mkdir(parents=True, exist_ok=True)

        thread: dict[str, object] = {
            "id": db_path.parent.name.rsplit("-", 1)[-1],
            "project_path": "",
            "title": "Imported session",
            "status": "open",
            "model": "",
            "created_at": 0,
            "updated_at": 0,
        }
        messages: list[JsonObject] = []
        for record in records:
            if isinstance(record, dict) and "_thread" in record:
                thread.update(as_object(record.get("_thread")) or {})
            elif isinstance(record, dict) and "role" in record:
                messages.append(record)

        conn = _freebuff_connect(db_path, create=True)
        try:
            conn.executescript(_FREEBUFF_SCHEMA)
            # When merging into an existing project db (e.g. the app's own
            # database), continue the global message sequence to avoid
            # PRIMARY KEY collisions with rows the app already wrote.
            thread_id = str(thread.get("id") or db_path.parent.name)
            # Re-writes are idempotent per thread: drop this thread's old
            # message rows before inserting fresh ones.
            try:
                conn.execute("DELETE FROM messages WHERE thread_id = ?", (thread_id,))
            except Exception:
                pass
            # Continue the global message sequence so rows from other
            # threads (the app's own writes) keep their ids.
            seq_offset = 0
            try:
                seq_offset = int(conn.execute("SELECT COALESCE(MAX(seq), 0) FROM messages").fetchone()[0])
            except Exception:
                seq_offset = 0
            project_path = str(thread.get("project_path") or "")
            # Preserve the app's original project row (id, root_path,
            # created_at) so imports do not move the project birthday.
            try:
                existing_project = conn.execute(
                    "SELECT created_at FROM projects WHERE id = ?", (project_path,)
                ).fetchone()
            except Exception:
                existing_project = None
            if existing_project and not thread.get("_preserve_created_at") is False:
                thread["created_at"] = existing_project[0]
                if not thread.get("_created_at_set") is False:
                    thread["_project_created_at"] = existing_project[0]
            conn.execute(
                "INSERT OR REPLACE INTO projects (id, root_path, default_branch, created_at) VALUES (?,?,?,?)",
                (project_path, project_path, "main", int(existing_project[0] if existing_project else (thread.get("created_at") or 0))),
            )
            # Keep the original thread creation time when the thread already
            # exists in the database (re-import preserves provenance).
            try:
                existing_thread = conn.execute(
                    "SELECT created_at FROM threads WHERE id = ?", (thread_id,)
                ).fetchone()
            except Exception:
                existing_thread = None
            if existing_thread:
                thread["created_at"] = existing_thread[0]
            conn.execute(
                "INSERT OR REPLACE INTO threads (id, project_id, project_path, title, status, model, created_at, updated_at, last_prompt_at, last_turn_finished_at, last_turn_outcome, attention_revision, attention_acknowledged_revision, attention_reason, attention_at, turn_alive_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    thread_id,
                    project_path,
                    project_path,
                    str(thread.get("title") or "Imported session"),
                    str(thread.get("status") or "open"),
                    str(thread.get("model") or ""),
                    int(thread.get("created_at") or 0),
                    int(thread.get("updated_at") or 0),
                    thread.get("last_prompt_at"),
                    thread.get("last_turn_finished_at"),
                    thread.get("last_turn_outcome") or "completed",
                    int(thread.get("attention_revision") or 1),
                    int(thread.get("attention_acknowledged_revision") or 1),
                    thread.get("attention_reason") or "finished",
                    thread.get("attention_at"),
                    thread.get("turn_alive_at"),
                ),
            )
            for index, message in enumerate(messages, start=1):
                conn.execute(
                    "INSERT INTO messages (seq, thread_id, role, parts_json, attachments_json, metrics_json, ts) VALUES (?,?,?,?,?,?,?)",
                    (
                        seq_offset + index,
                        thread_id,
                        str(message.get("role") or "user"),
                        _json_dumps_default(message.get("parts") or [{"kind": "text", "text": ""}]),
                        "[]",
                        "{}",
                        int(message.get("ts") or 0),
                    ),
                )
            conn.commit()
        finally:
            conn.close()

        # Write project.json alongside the db.  Freebuff verifies
        # project.json.projectId against the project root's
        # .freebuff/project-id marker and refuses to open the database on
        # mismatch, so the id must be the project identity UUID -- never a
        # path.  Fall back to the existing file's id, then a fresh UUID.
        import uuid as _uuid
        project_id = None
        project_path = str(thread.get("project_path") or "")
        if project_path:
            pid_file = Path(project_path) / ".freebuff" / "project-id"
            try:
                if pid_file.is_file():
                    project_id = pid_file.read_text(encoding="utf-8").strip()
            except OSError:
                pass
        if not project_id:
            try:
                existing_meta = _json_loads((db_path.parent / "project.json").read_bytes())
                if isinstance(existing_meta, dict) and string_value(existing_meta, "projectId"):
                    project_id = string_value(existing_meta, "projectId")
            except Exception:
                pass
        if not project_id:
            project_id = str(_uuid.uuid4())
        (db_path.parent / "project.json").write_text(
            _json_dumps_pretty({
                "version": 1,
                "projectId": project_id,
                "projectPath": project_path,
                "database": self._DB_NAME,
            }),
            encoding="utf-8",
        )

    def _session_paths(self) -> list[Path]:
        if self._path_cache is not None:
            return self._path_cache
        root = self._active_session_root()
        if not root.exists():
            self._path_cache = []
            return []
        paths: list[Path] = []
        for project_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            db = project_dir / self._DB_NAME
            if db.is_file() and db.stat().st_size > 0:
                paths.append(db)
        self._path_cache = paths
        return paths

    def _id_index_cache(self) -> dict[str, Path]:
        if self._id_index is not None:
            return self._id_index
        index: dict[str, Path] = {}
        for db_path in self._session_paths():
            try:
                conn = _freebuff_connect(db_path)
                try:
                    for row in conn.execute("SELECT id FROM threads"):
                        index[str(row[0])] = db_path
                finally:
                    conn.close()
            except Exception:
                continue
        self._id_index = index
        return index

    def _active_session_root(self) -> Path:
        return self._session_dir or (self._freebuff_home / "projects")

    def _summaries_from_db(self, db_path: Path) -> list[SessionSummary]:
        summaries: list[SessionSummary] = []
        try:
            conn = _freebuff_connect(db_path)
            try:
                rows = conn.execute(
                    "SELECT t.id, t.project_path, t.created_at, t.updated_at, "
                    "(SELECT COUNT(*) FROM messages m WHERE m.thread_id = t.id) AS n "
                    "FROM threads t"
                ).fetchall()
            finally:
                conn.close()
        except (_sqlite3.Error, OSError) as exc:
            print(f"warning: skipped unreadable Freebuff session db {db_path}: {exc}", file=sys.stderr)
            return summaries
        for row in rows:
            ts_value = row[2] if row[2] else row[3] or 0
            timestamp = epoch_ms_to_iso(int(ts_value)) if isinstance(ts_value, (int, float)) else ""
            summaries.append(SessionSummary(
                "freebuff",
                str(row[0]),
                str(row[1] or ""),
                timestamp,
                db_path,
                int(row[4] or 0),
            ))
        return summaries

    def _load_file(self, db_path: Path, session_id: str | None) -> NativeSession:
        try:
            conn = _freebuff_connect(db_path)
        except _sqlite3.Error as exc:
            raise FileNotFoundError(f"Freebuff database unreadable: {db_path}: {exc}") from exc
        try:
            if session_id is None:
                row = conn.execute("SELECT id FROM threads LIMIT 1").fetchone()
                session_id = str(row[0]) if row else db_path.parent.name
            thread = conn.execute(
                "SELECT id, project_path, title, status, model, created_at, updated_at, fork_source_thread_id "
                "FROM threads WHERE id = ?",
                (session_id,),
            ).fetchone()
            if thread is None:
                raise FileNotFoundError(f"Freebuff thread not found: {session_id}")
            messages: list[JsonObject] = []
            for msg in conn.execute(
                "SELECT seq, role, parts_json, ts FROM messages WHERE thread_id = ? ORDER BY seq",
                (session_id,),
            ):
                parts = _json_loads(msg[2]) if msg[2] else []
                messages.append({
                    "seq": msg[0],
                    "role": msg[1],
                    "parts": parts if isinstance(parts, list) else [],
                    "ts": msg[3],
                })
        finally:
            conn.close()
        ts_value = thread[5] if thread[5] else thread[6] or 0
        timestamp = epoch_ms_to_iso(int(ts_value)) if isinstance(ts_value, (int, float)) else ""
        records: list[JsonObject] = [{
            "_thread": {
                "id": thread[0],
                "project_path": thread[1] or "",
                "title": thread[2] or "",
                "status": thread[3] or "",
                "model": thread[4] or "",
                "created_at": thread[5],
                "updated_at": thread[6],
                "fork_source_thread_id": thread[7],
            }
        }]
        records.extend(messages)
        return NativeSession("freebuff", session_id, str(thread[1] or ""), timestamp, db_path, records)


_FREEBUFF_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id             TEXT PRIMARY KEY,
    root_path      TEXT NOT NULL,
    default_branch TEXT NOT NULL DEFAULT 'main',
    created_at     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS threads (
    id             TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL,
    project_path   TEXT NOT NULL,
    title          TEXT NOT NULL DEFAULT 'New thread',
    status         TEXT NOT NULL DEFAULT 'open',
    harness_id     TEXT,
    model          TEXT,
    reasoning_effort TEXT,
    agent_mode     TEXT NOT NULL DEFAULT 'build',
    execution_mode TEXT NOT NULL DEFAULT 'local',
    branch         TEXT,
    worktree_path  TEXT,
    fork_source_thread_id TEXT,
    last_prompt_at INTEGER,
    last_turn_finished_at INTEGER,
    last_turn_outcome TEXT,
    attention_revision INTEGER NOT NULL DEFAULT 0,
    attention_acknowledged_revision INTEGER NOT NULL DEFAULT 0,
    attention_reason TEXT,
    attention_at INTEGER,
    turn_alive_at INTEGER,
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    seq              INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id        TEXT NOT NULL,
    request_id       TEXT,
    input_id         TEXT,
    role             TEXT NOT NULL,
    parts_json       TEXT NOT NULL DEFAULT '[]',
    attachments_json TEXT NOT NULL DEFAULT '[]',
    metrics_json     TEXT NOT NULL DEFAULT '{}',
    ts               INTEGER NOT NULL
);
"""


def _freebuff_connect(db_path: Path, *, create: bool = False) -> sqlite3.Connection:
    """Open a Freebuff SQLite db safely (read-only when possible).

    Freebuff Desktop may hold the database open with a WAL journal.  A
    read-only connection reads the WAL without locking; if that fails
    (e.g. exclusive locks), fall back to the SQLite Online Backup API to
    snapshot into a temp file and open the snapshot.
    """
    if create:
        return _sqlite3.connect(str(db_path), timeout=10)
    try:
        conn = _sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=5)
        conn.execute("PRAGMA query_only=ON")
        return conn
    except _sqlite3.Error:
        import tempfile
        src = _sqlite3.connect(str(db_path), timeout=10)
        try:
            tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            tmp_name = tmp.name
            tmp.close()
            dst = _sqlite3.connect(tmp_name)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        return _sqlite3.connect(tmp_name, timeout=10)


def _json_dumps_default(value: object) -> str:
    if _HAS_ORJSON:
        return orjson.dumps(value).decode("utf-8")
    return json.dumps(value)


class PiDcpStore:
    def __init__(self, pi_dcp_home: Path) -> None:
        self._pi_dcp_home = pi_dcp_home

    def destination_path(self, session_id: str) -> Path:
        return self._pi_dcp_home / "sessions" / f"{session_id}.json"

    def write_default(self, session_id: str, path: Path, *, overwrite: bool = False) -> None:
        if path.exists() and not overwrite:
            raise FileExistsError(f"Refusing to overwrite existing file: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: JsonObject = {
            "version": 1,
            "sessionId": session_id,
            "savedAt": 0,
            "nextCompressionId": 1,
            "turnIndex": 0,
            "compressions": [],
            "dedupedCallIds": [],
            "purgedErrorCallIds": [],
            "appliedCompressionTargets": [],
            "erroredAt": [],
            "stats": {
                "dedupPruned": 0,
                "errorInputsPurged": 0,
                "compressionsApplied": 0,
                "tokensSaved": 0,
            },
        }
        if _HAS_ORJSON:
            path.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2))
        else:
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


class OpenCodeStore(SessionStore):
    provider_name = "opencode"

    def __init__(self, data_home: Path, session_dir: Path | None = None) -> None:
        self._data_home = data_home
        self._session_dir = session_dir
        self._path_cache: list[Path] | None = None
        self._id_index: dict[str, Path] | None = None

    @property
    def root(self) -> Path:
        return self._data_home

    def list(self, *, workers: int = 1) -> list[SessionSummary]:
        paths = self._session_paths()
        if workers <= 1 or len(paths) <= 1:
            return [s for path in paths if (s := self._cached(self._safe_summary, path)) is not None]
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(workers, len(paths)), thread_name_prefix="oc-list") as executor:
            results = list(executor.map(lambda p: self._cached(self._safe_summary, p), paths))
        return [s for s in results if s is not None]

    def load(self, session_id: str) -> NativeSession:
        path = self._find_path(session_id)
        if path is not None:
            return self._load_file(path)
        raise FileNotFoundError(f"OpenCode session export not found: {session_id}")

    def load_path(self, path: Path) -> NativeSession:
        return self._load_file(path)

    def _find_path(self, session_id: str) -> Path | None:
        index = self._id_index_cache()
        if session_id in index:
            return index[session_id]
        for path in self._session_paths():
            if session_id in path.name:
                return path
        return None

    def destination_path(self, session_id: str) -> Path:
        return self._active_session_root() / f"{session_id}.json"

    def write(self, path: Path, records: list[JsonObject], *, overwrite: bool = False) -> None:
        if path.exists() and not overwrite:
            raise FileExistsError(f"Refusing to overwrite existing file: {path}")
        if not records:
            raise ValueError("OpenCode export requires a JSON payload")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(records[0], indent=2), encoding="utf-8")

    def _session_paths(self) -> list[Path]:
        if self._path_cache is not None:
            return self._path_cache
        root = self._active_session_root()
        if not root.exists():
            self._path_cache = []
            return []
        paths = sorted(root.rglob("*.json"))
        self._path_cache = paths
        return paths

    def _id_index_cache(self) -> dict[str, Path]:
        if self._id_index is not None:
            return self._id_index
        index: dict[str, Path] = {}
        for path in self._session_paths():
            index[path.stem] = path
        self._id_index = index
        return index

    def _load_file(self, path: Path) -> NativeSession:
        if _HAS_ORJSON:
            value = orjson.loads(path.read_bytes())
        else:
            value = json.loads(path.read_text(encoding="utf-8"))
        export = as_object(value)
        if export is None:
            raise ValueError(f"{path} is not a JSON object")
        info = as_object(export.get("info")) or {}
        session_id = string_value(info, "id") or path.stem
        cwd = string_value(info, "directory") or ""
        time = as_object(info.get("time")) or {}
        created = time.get("created")
        timestamp = epoch_ms_to_iso(created) if isinstance(created, int) else ""
        return NativeSession("opencode", session_id, cwd, timestamp, path, [export])

    def _safe_load_file(self, path: Path) -> NativeSession | None:
        try:
            return self._load_file(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"warning: skipped unreadable OpenCode session export {path}: {exc}", file=sys.stderr)
            return None

    def _safe_summary(self, path: Path) -> SessionSummary | None:
        try:
            if _HAS_ORJSON:
                value = orjson.loads(path.read_bytes())
            else:
                value = json.loads(path.read_text(encoding="utf-8"))
            export = as_object(value)
            if export is None:
                return None
            info = as_object(export.get("info")) or {}
            session_id = string_value(info, "id") or path.stem
            cwd = string_value(info, "directory") or ""
            time = as_object(info.get("time")) or {}
            created = time.get("created")
            timestamp = epoch_ms_to_iso(created) if isinstance(created, int) else ""
            messages = export.get("messages")
            message_count = len(messages) if isinstance(messages, list) else 0
            return SessionSummary("opencode", session_id, cwd, timestamp, path, message_count)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"warning: skipped unreadable OpenCode session export {path}: {exc}", file=sys.stderr)
            return None

    def _active_session_root(self) -> Path:
        return self._session_dir or (self._data_home / "session-export")

    @staticmethod
    def _message_count(records: list[JsonObject]) -> int:
        if not records:
            return 0
        export = records[0]
        messages = export.get("messages")
        if isinstance(messages, list):
            return len(messages)
        return 0


# Model defaults for each T3 provider driver. The instance id of a built-in driver is the driver
# name (T3 Code packages/contracts/src/providerInstance.ts, defaultInstanceIdForDriver), and the
# model is DEFAULT_MODEL_BY_PROVIDER (packages/contracts/src/model.ts). Keep in sync with T3.
T3_PROVIDER_DRIVERS: dict[str, str] = {
    "codex": "gpt-6-astra",
    "claudeAgent": "claude-fable-5-1",
    "opencode": "openai/gpt-5",
    "pi": "default",
    "grok": "grok-build",
}

# Marks legacy databases created by unisessions, in the SQLite header. T3 ignores it.
_T3_UNISESSIONS_APPLICATION_ID = 0x554E4953
_T3_LEGACY_TEMPLATE = Path(__file__).resolve().parent / "data" / "t3_state_v1.sql"


class T3WriteError(RuntimeError):
    """A T3 write was refused: the target is unsafe or not a unisessions database."""


def _live_userdata() -> Path:
    """The live T3 data directory. Writes must never target it."""
    env = os.environ.get("T3CODE_HOME")
    base = Path(env) if env else Path.home() / ".t3"
    return base / "userdata"


def _record_object(record: JsonObject, key: str) -> JsonObject:
    return _as_object(record.get(key), key)


def _as_object(value: object, label: str) -> JsonObject:
    if not isinstance(value, dict):
        raise T3WriteError(f"T3 record field '{label}' must be an object")
    return value


def _record_list(record: JsonObject, key: str) -> list[object]:
    value = record.get(key)
    if not isinstance(value, list):
        raise T3WriteError(f"T3 record field '{key}' must be a list")
    return value


def _record_text(record: JsonObject, key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str):
        raise T3WriteError(f"T3 record field '{key}' must be text")
    return value


@dataclass(frozen=True, slots=True)
class _T3Thread:
    thread_id: str
    database: Path
    cwd: str
    created_at: str
    instance_id: str
    model: str
    message_count: int
    v2: bool


class T3Store(SessionStore):
    """Read-only store for T3 Code (t3.codes) threads.

    T3 keeps its data under ``<T3 home>/userdata``. Two SQLite databases can exist:

    - ``statev2.sqlite`` (current): threads and messages are projected into
      ``orchestration_v2_projection_threads`` and ``orchestration_v2_projection_messages``.
      The full object of each row is in ``payload_json``.
    - ``state.sqlite`` (legacy V1): plain ``projection_threads`` and
      ``projection_thread_messages`` tables.

    A thread comes from ``statev2.sqlite`` when that database has it, otherwise from
    ``state.sqlite``. Both databases are opened read-only. T3 keeps the live database
    open while the app runs, so this store never writes to it.

    Each thread gets the virtual path ``<database>/<thread id>``. The file itself holds
    many threads, and the search index keys sessions by path.
    """

    provider_name = "t3"
    _V2_DB = "statev2.sqlite"
    _V1_DB = "state.sqlite"

    def __init__(self, t3_home: Path) -> None:
        self._userdata = Path(t3_home) / "userdata"

    @property
    def root(self) -> Path:
        return self._userdata

    def list(self, *, workers: int = 1) -> list[SessionSummary]:
        return [
            SessionSummary(
                provider=self.provider_name,
                session_id=thread.thread_id,
                cwd=thread.cwd,
                timestamp=thread.created_at,
                path=self._virtual_path(thread),
                message_count=thread.message_count,
            )
            for thread in self._catalog().values()
        ]

    def load(self, session_id: str) -> NativeSession:
        thread = self._catalog().get(session_id)
        if thread is None:
            raise FileNotFoundError(f"T3 thread not found: {session_id}")
        return self._load_thread(thread)

    def load_path(self, path: Path) -> NativeSession:
        database, thread_id = Path(path).parent, Path(path).name
        for thread in self._threads_in(database):
            if thread.thread_id == thread_id:
                return self._load_thread(thread)
        raise FileNotFoundError(f"T3 thread not found: {path}")

    def _databases(self) -> list[Path]:
        """Databases in preference order: the current V2 file, then the legacy V1 file."""
        return [path for path in (self._userdata / self._V2_DB, self._userdata / self._V1_DB) if path.is_file()]

    def _catalog(self) -> dict[str, _T3Thread]:
        catalog: dict[str, _T3Thread] = {}
        for database in self._databases():
            for thread in self._threads_in(database):
                catalog.setdefault(thread.thread_id, thread)
        return catalog

    @staticmethod
    def _virtual_path(thread: _T3Thread) -> Path:
        return thread.database / thread.thread_id

    @staticmethod
    def _connect(database: Path) -> _sqlite3.Connection:
        # mode=ro: the database is never written, even by accident.
        uri = database.resolve().as_uri() + "?mode=ro"
        connection = _sqlite3.connect(uri, uri=True)
        connection.row_factory = _sqlite3.Row
        return connection

    @staticmethod
    def _has_table(connection: _sqlite3.Connection, name: str) -> bool:
        return connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None

    def _threads_in(self, database: Path) -> list[_T3Thread]:
        connection = self._connect(database)
        try:
            if self._has_table(connection, "orchestration_v2_projection_threads"):
                return self._v2_threads(connection, database)
            return self._v1_threads(connection, database)
        finally:
            connection.close()

    def _v2_threads(self, connection: _sqlite3.Connection, database: Path) -> list[_T3Thread]:
        roots = self._workspace_roots(connection)
        counts = {
            str(row["thread_id"]): int(row["count"])
            for row in connection.execute(
                "SELECT thread_id, COUNT(*) AS count FROM orchestration_v2_projection_messages WHERE role IN ('user', 'assistant') GROUP BY thread_id"
            )
        }
        threads: list[_T3Thread] = []
        for row in connection.execute("SELECT thread_id, project_id, created_at, payload_json FROM orchestration_v2_projection_threads WHERE deleted_at IS NULL"):
            payload = _json_object(row["payload_json"])
            selection = _json_object_value(payload, "modelSelection")
            worktree = payload.get("worktreePath")
            cwd = worktree if isinstance(worktree, str) and worktree else roots.get(str(row["project_id"]), "")
            threads.append(
                _T3Thread(
                    thread_id=str(row["thread_id"]),
                    database=database,
                    cwd=cwd,
                    created_at=str(payload.get("createdAt") or row["created_at"]),
                    instance_id=_string_field(selection, "instanceId"),
                    model=_string_field(selection, "model"),
                    message_count=counts.get(str(row["thread_id"]), 0),
                    v2=True,
                )
            )
        return threads

    def _v1_threads(self, connection: _sqlite3.Connection, database: Path) -> list[_T3Thread]:
        roots = self._workspace_roots(connection)
        counts = {
            str(row["thread_id"]): int(row["count"])
            for row in connection.execute(
                "SELECT thread_id, COUNT(*) AS count FROM projection_thread_messages WHERE role IN ('user', 'assistant') GROUP BY thread_id"
            )
        }
        threads: list[_T3Thread] = []
        for row in connection.execute("SELECT * FROM projection_threads WHERE deleted_at IS NULL"):
            keys = row.keys()
            selection = _json_object(row["model_selection_json"]) if "model_selection_json" in keys else {}
            worktree = row["worktree_path"]
            cwd = worktree if isinstance(worktree, str) and worktree else roots.get(str(row["project_id"]), "")
            threads.append(
                _T3Thread(
                    thread_id=str(row["thread_id"]),
                    database=database,
                    cwd=cwd,
                    created_at=str(row["created_at"]),
                    instance_id=_string_field(selection, "instanceId") or _string_field(selection, "provider"),
                    model=_string_field(selection, "model") or str(row["model"] or ""),
                    message_count=counts.get(str(row["thread_id"]), 0),
                    v2=False,
                )
            )
        return threads

    def _workspace_roots(self, connection: _sqlite3.Connection) -> dict[str, str]:
        if not self._has_table(connection, "projection_projects"):
            return {}
        return {str(row["project_id"]): str(row["workspace_root"]) for row in connection.execute("SELECT project_id, workspace_root FROM projection_projects")}

    def _load_thread(self, thread: _T3Thread) -> NativeSession:
        connection = self._connect(thread.database)
        try:
            records = self._v2_records(connection, thread) if thread.v2 else self._v1_records(connection, thread)
        finally:
            connection.close()
        return NativeSession(
            provider=self.provider_name,
            session_id=thread.thread_id,
            cwd=thread.cwd,
            timestamp=thread.created_at,
            path=self._virtual_path(thread),
            records=records,
        )

    @staticmethod
    def _v2_records(connection: _sqlite3.Connection, thread: _T3Thread) -> list[JsonObject]:
        rows = connection.execute(
            "SELECT payload_json FROM orchestration_v2_projection_messages WHERE thread_id = ? ORDER BY created_at ASC, message_id ASC",
            (thread.thread_id,),
        )
        records: list[JsonObject] = []
        for row in rows:
            payload = _json_object(row["payload_json"])
            records.append(_t3_record(payload.get("role"), payload.get("text"), payload.get("createdAt"), thread))
        return records

    @staticmethod
    def _v1_records(connection: _sqlite3.Connection, thread: _T3Thread) -> list[JsonObject]:
        rows = connection.execute(
            "SELECT role, text, created_at FROM projection_thread_messages WHERE thread_id = ? AND role IN ('user', 'assistant') ORDER BY created_at ASC, message_id ASC",
            (thread.thread_id,),
        )
        return [_t3_record(row["role"], row["text"], row["created_at"], thread) for row in rows]

    @property
    def legacy_database(self) -> Path:
        """``state.sqlite``. Writes go here. T3 imports it when it creates ``statev2.sqlite``."""
        return self._userdata / self._V1_DB

    def thread_exists(self, database: Path, thread_id: str) -> bool:
        return self.thread_message_count(database, thread_id) is not None

    def thread_message_count(self, database: Path, thread_id: str) -> int | None:
        """Messages stored for a legacy thread, or None when the database or thread is missing."""
        if not database.is_file():
            return None
        connection = self._connect(database)
        try:
            if not self._has_table(connection, "projection_threads"):
                return None
            found = connection.execute("SELECT 1 FROM projection_threads WHERE thread_id = ?", (thread_id,)).fetchone()
            if found is None:
                return None
            row = connection.execute("SELECT COUNT(*) AS count FROM projection_thread_messages WHERE thread_id = ?", (thread_id,)).fetchone()
            return int(row["count"])
        finally:
            connection.close()

    def write(self, path: Path, records: list[JsonObject], *, overwrite: bool = False) -> None:
        """EXPERIMENTAL. Write thread bundles (see ``T3RecordBuilder``) into a legacy V1 database.

        T3 only imports legacy threads when it creates ``statev2.sqlite``, so the target home
        must not have one yet. A new database is built in a staging file and moved into place,
        so a failed write never leaves a partial ``state.sqlite``. An existing database must
        have been created by unisessions (checked through ``application_id``).
        """
        self._check_write_target(path)
        if path.exists():
            self._append(path, records, overwrite=overwrite)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        staging = path.with_name(f"{path.name}.unisessions-tmp")
        staging.unlink(missing_ok=True)
        try:
            connection = _sqlite3.connect(staging)
            try:
                connection.executescript(_T3_LEGACY_TEMPLATE.read_text(encoding="utf-8"))
                connection.execute(f"PRAGMA application_id = {_T3_UNISESSIONS_APPLICATION_ID}")
                with connection:
                    for record in records:
                        self._write_thread(connection, record, overwrite=False)
            finally:
                connection.close()
            os.replace(staging, path)
        except BaseException:
            staging.unlink(missing_ok=True)
            raise

    def _append(self, path: Path, records: list[JsonObject], *, overwrite: bool) -> None:
        connection = _sqlite3.connect(path)
        try:
            marker = int(connection.execute("PRAGMA application_id").fetchone()[0])
            if marker != _T3_UNISESSIONS_APPLICATION_ID:
                raise T3WriteError(f"{path} was not created by unisessions. Refusing to modify it.")
            with connection:
                for record in records:
                    self._write_thread(connection, record, overwrite=overwrite)
        finally:
            connection.close()

    def _write_thread(self, connection: _sqlite3.Connection, record: JsonObject, *, overwrite: bool) -> None:
        thread = _record_object(record, "thread")
        project = _record_object(record, "project")
        messages = _record_list(record, "messages")
        thread_id = _record_text(thread, "thread_id")
        if self.thread_exists_in(connection, thread_id):
            if not overwrite:
                raise FileExistsError(f"T3 thread already exists in the target database: {thread_id}")
            connection.execute("DELETE FROM projection_thread_messages WHERE thread_id = ?", (thread_id,))
            connection.execute("DELETE FROM projection_threads WHERE thread_id = ?", (thread_id,))
        connection.execute(
            "INSERT OR IGNORE INTO projection_projects (project_id, title, workspace_root, scripts_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                _record_text(project, "project_id"),
                _record_text(project, "title"),
                _record_text(project, "workspace_root"),
                "[]",
                _record_text(project, "created_at"),
                _record_text(project, "updated_at"),
            ),
        )
        connection.execute(
            """
            INSERT INTO projection_threads (
                thread_id, project_id, title, branch, worktree_path, created_at, updated_at,
                runtime_mode, interaction_mode, model_selection_json
            ) VALUES (?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?)
            """,
            (
                thread_id,
                _record_text(thread, "project_id"),
                _record_text(thread, "title"),
                _record_text(thread, "created_at"),
                _record_text(thread, "updated_at"),
                _record_text(thread, "runtime_mode"),
                _record_text(thread, "interaction_mode"),
                _record_text(thread, "model_selection_json"),
            ),
        )
        rows = [
            (
                _record_text(message, "message_id"),
                thread_id,
                _record_text(message, "role"),
                _record_text(message, "text"),
                _record_text(message, "created_at"),
                _record_text(message, "created_at"),
            )
            for message in (_as_object(item, "message") for item in messages)
        ]
        connection.executemany(
            """
            INSERT INTO projection_thread_messages (
                message_id, thread_id, turn_id, role, text, is_streaming, created_at, updated_at, attachments_json, context_json
            ) VALUES (?, ?, NULL, ?, ?, 0, ?, ?, '[]', NULL)
            """,
            rows,
        )

    @staticmethod
    def thread_exists_in(connection: _sqlite3.Connection, thread_id: str) -> bool:
        return connection.execute("SELECT 1 FROM projection_threads WHERE thread_id = ?", (thread_id,)).fetchone() is not None

    def _check_write_target(self, path: Path) -> None:
        if path.name != self._V1_DB:
            raise T3WriteError(f"T3 writes go to {self._V1_DB}, not {path.name}.")
        userdata = path.parent
        if (userdata / self._V2_DB).exists():
            raise T3WriteError(
                f"{userdata} already has {self._V2_DB}. T3 imports legacy threads only when it creates that file, "
                "so write to a new, empty T3 home."
            )
        if userdata.resolve() == _live_userdata().resolve():
            raise T3WriteError(
                f"{userdata} is the live T3 Code data directory. Writes must target a new, empty T3 home (--t3-home)."
            )


def _json_object(value: object) -> JsonObject:
    """Parse a JSON object column. NULL, bad JSON, and non-object values become an empty dict."""
    if not isinstance(value, (str, bytes)):
        return {}
    try:
        parsed = _json_loads(value)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_object_value(payload: JsonObject, key: str) -> JsonObject:
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def _string_field(payload: JsonObject, key: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) else ""


def _t3_record(role: object, text: object, created_at: object, thread: _T3Thread) -> JsonObject:
    return {
        "role": role if isinstance(role, str) else "",
        "text": text if isinstance(text, str) else "",
        "created_at": created_at if isinstance(created_at, str) else thread.created_at,
        "instance_id": thread.instance_id,
        "model": thread.model,
    }
