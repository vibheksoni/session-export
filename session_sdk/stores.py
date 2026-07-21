from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    import orjson
    _HAS_ORJSON = True
except ImportError:
    _HAS_ORJSON = False

from session_sdk.json_types import JsonObject, as_list, as_object, as_str, string_value
from session_sdk.jsonl import JsonlFile
from session_sdk.models import NativeSession, SessionSummary
from session_sdk.paths import codex_date_parts, codex_filename_timestamp, encode_pi_cwd, epoch_ms_to_iso, pi_filename_timestamp, sanitize_claude_cwd

import sqlite3 as _sqlite3


def _json_loads(data: str | bytes) -> object:
    if _HAS_ORJSON:
        if isinstance(data, str):
            data = data.encode("utf-8")
        return orjson.loads(data)
    return json.loads(data)


class SessionStore:
    provider_name: str

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
            return [summary for path in paths if (summary := self._safe_summary(path)) is not None]
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(workers, len(paths)), thread_name_prefix="cx-list") as executor:
            results = list(executor.map(self._safe_summary, paths))
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
            return [s for path in paths if (s := self._safe_summary(path)) is not None]
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(workers, len(paths)), thread_name_prefix="pi-list") as executor:
            results = list(executor.map(self._safe_summary, paths))
        return [s for s in results if s is not None]

    def list_metadata(self, *, workers: int = 1) -> list[SessionSummary]:
        paths = self._session_paths()
        if workers <= 1 or len(paths) <= 1:
            return [s for path in paths if (s := self._safe_metadata(path)) is not None]
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(workers, len(paths)), thread_name_prefix="pi-meta") as executor:
            results = list(executor.map(self._safe_metadata, paths))
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
            return [s for path in paths if (s := self._safe_summary(path)) is not None]
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(workers, len(paths)), thread_name_prefix="cc-list") as executor:
            results = list(executor.map(self._safe_summary, paths))
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
        return [s for path in paths if (s := self._safe_summary(path)) is not None]

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
            return [s for path in paths if (s := self._safe_summary(path)) is not None]
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(workers, len(paths)), thread_name_prefix="factory-list") as executor:
            results = list(executor.map(self._safe_summary, paths))
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
            return [s for path in paths if (s := self._safe_summary(path)) is not None]
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(workers, len(paths)), thread_name_prefix="ws-list") as executor:
            results = list(executor.map(self._safe_summary, paths))
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
        cwd = self._extract_cwd(steps)
        timestamp = self._extract_timestamp(steps) or ""
        return NativeSession("windsurf", session_id, cwd, timestamp, path, [{"_plaintext": plaintext, "_cascade_id": cascade_id}])

    def _safe_summary(self, path: Path) -> SessionSummary | None:
        try:
            from session_sdk.windsurf_pb import parse_trajectory, parse_step, VARIANT_USER_INPUT, VARIANT_PLANNER_RESPONSE
            plaintext = self._decrypt(path)
            traj = parse_trajectory(plaintext)
            session_id = str(traj.get("trajectory_id") or path.stem)
            steps = traj.get("steps") or []
            cwd = self._extract_cwd(steps)
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
    def _extract_cwd(steps: list[object]) -> str:
        """Extract cwd from user_input steps that mention a real file path.

        Collects all cwd candidates from PS prompts and cd commands across
        all user_input steps, then returns the most frequently occurring one.
        This handles sessions where the user changed directories mid-session —
        the most common cwd is where the bulk of the work happened.
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
            return [s for path in paths if (s := self._safe_summary(path)) is not None]
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(workers, len(paths)), thread_name_prefix="oc-list") as executor:
            results = list(executor.map(self._safe_summary, paths))
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
