from __future__ import annotations

import os
import string
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID
from uuid import uuid4


class SessionIdFactory:
    def __init__(self, *, preserve_ids: bool = True) -> None:
        self._preserve_ids = preserve_ids

    def create(self, source_id: str) -> str:
        if self._preserve_ids:
            return source_id
        return str(uuid4())

    def create_codex(self, source_id: str) -> str:
        if self._preserve_ids and is_uuid(source_id):
            return source_id
        return str(uuid4())

    def create_opencode(self, source_id: str, timestamp: str) -> str:
        if self._preserve_ids and source_id.startswith("ses_"):
            return source_id
        return opencode_id("ses", timestamp)


class WindowsDefaults:
    def __init__(self, home: Path | None = None) -> None:
        self._home = home or Path.home()

    @property
    def codex_home(self) -> Path:
        return self._home / ".codex"

    @property
    def pi_home(self) -> Path:
        return self._home / ".pi"

    @property
    def pi_agent_home(self) -> Path:
        return self.pi_home / "agent"

    @property
    def pi_dcp_home(self) -> Path:
        return self._home / ".pi-dcp"

    @property
    def opencode_data_home(self) -> Path:
        env = os.environ.get("OPENCODE_GLOBAL_DATA_DIR")
        if env:
            return Path(env)
        if sys.platform == "win32":
            appdata = os.environ.get("APPDATA")
            if appdata:
                return Path(appdata) / "opencode"
            return self._home / "AppData" / "Roaming" / "opencode"
        xdg_data = os.environ.get("XDG_DATA_HOME")
        if xdg_data:
            return Path(xdg_data) / "opencode"
        return Path.home() / ".local" / "share" / "opencode"

    @property
    def claude_home(self) -> Path:
        env = os.environ.get("CLAUDE_CONFIG_DIR")
        if env:
            return Path(env)
        return self._home / ".claude"

    @property
    def devin_home(self) -> Path:
        env = os.environ.get("DEVIN_CONFIG_DIR")
        if env:
            return Path(env)
        if sys.platform == "win32":
            appdata = os.environ.get("APPDATA")
            if appdata:
                return Path(appdata) / "devin"
            return self._home / "AppData" / "Roaming" / "devin"
        xdg_config = os.environ.get("XDG_CONFIG_HOME")
        if xdg_config:
            return Path(xdg_config) / "devin"
        return self._home / ".config" / "devin"

    @property
    def factory_home(self) -> Path:
        env = os.environ.get("FACTORY_CONFIG_DIR")
        if env:
            return Path(env)
        return self._home / ".factory"

    @property
    def windsurf_home(self) -> Path:
        env = os.environ.get("WINDSURF_CONFIG_DIR")
        if env:
            return Path(env)
        return self._home / ".codeium" / "windsurf"

    @property
    def grok_home(self) -> Path:
        env = os.environ.get("GROK_HOME")
        if env:
            return Path(env)
        return self._home / ".grok"

    @property
    def opencode_session_dir(self) -> Path:
        return self.opencode_data_home / "session-export"


def pi_filename_timestamp(timestamp: str) -> str:
    return timestamp.replace(":", "-").replace(".", "-")


def codex_filename_timestamp(timestamp: str) -> str:
    return timestamp.replace(":", "-").replace(".", "-").replace("Z", "")


def codex_date_parts(timestamp: str) -> tuple[str, str, str]:
    date_part = timestamp.split("T", 1)[0]
    year, month, day = date_part.split("-", 2)
    return year, month, day


def is_uuid(value: str) -> bool:
    try:
        UUID(value)
    except ValueError:
        return False
    return True


def iso_to_epoch_ms(timestamp: str) -> int:
    if not timestamp:
        return int(datetime.now(UTC).timestamp() * 1000)
    normalized = timestamp.replace("Z", "+00:00")
    try:
        return int(datetime.fromisoformat(normalized).timestamp() * 1000)
    except ValueError:
        return int(datetime.now(UTC).timestamp() * 1000)


def epoch_ms_to_iso(value: int) -> str:
    return datetime.fromtimestamp(value / 1000, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


_BASE62_CHARS = string.digits + string.ascii_lowercase + string.ascii_uppercase


def _random_base62(length: int) -> str:
    import secrets
    return "".join(secrets.choice(_BASE62_CHARS) for _ in range(length))


def opencode_id(prefix: Literal["ses", "msg", "prt"], timestamp: str) -> str:
    epoch_ms = iso_to_epoch_ms(timestamp)
    encoded = (epoch_ms * 0x1000 + 1) & ((1 << 48) - 1)
    if prefix == "ses":
        encoded = ~encoded & ((1 << 48) - 1)
    hex_part = f"{encoded:012x}"
    return f"{prefix}_{hex_part}{_random_base62(14)}"


def opencode_slug(text: str) -> str:
    value = "".join(ch.lower() if ch.isalnum() else "-" for ch in text.strip())
    parts = [part for part in value.split("-") if part]
    return "-".join(parts[:8]) or "imported-session"


def sanitize_claude_cwd(cwd: str) -> str:
    sanitized = cwd.replace("\\?\\", "")
    sanitized = "".join(ch if ch.isalnum() else "-" for ch in sanitized)
    return sanitized


def encode_pi_cwd(cwd: str) -> str:
    # Strip Windows extended-length path prefix (\\?\) which produces invalid dir chars
    if cwd.startswith("\\\\?\\"):
        cwd = cwd[4:]
    # Only call resolve() if the path is absolute on the current platform.
    # On Linux, Path("C:\\home\\user").resolve() treats it as relative and
    # prepends the cwd, producing a wrong encoding for Windows-style paths.
    p = Path(cwd)
    if p.is_absolute():
        resolved = str(p.resolve())
        # Path.resolve() may re-add the prefix on Windows; strip again
        if resolved.startswith("\\\\?\\"):
            resolved = resolved[4:]
    else:
        resolved = cwd
    stripped = resolved.lstrip("/\\")
    encoded = stripped.replace("/", "-").replace("\\", "-").replace(":", "-")
    return f"--{encoded}--"


def encode_grok_cwd_dirname(cwd: str) -> str:
    """Encode a cwd into a Grok sessions group directory name.

    Grok URL-encodes the working directory to name the session group
    (``sessions/<encoded-cwd>/<session-id>/``).  When the URL-encoded form
    exceeds 255 bytes it falls back to ``{slug}-{blake3_hex16}`` and records
    the original path in a ``.cwd`` file inside the group.  This mirrors
    ``encode_cwd_dirname`` in ``crates/codegen/xai-grok-config/src/paths.rs``.
    """
    from urllib.parse import quote

    encoded = quote(cwd, safe="")
    if len(encoded) <= 255:
        return encoded
    # Long-path fallback: slug + 16-hex digest.  Python's stdlib has no
    # blake3, so use a deterministic blake2b digest truncated to 16 hex
    # chars; the authoritative original path is written to .cwd on disk.
    import hashlib
    import re

    slug = re.sub(r"[^a-zA-Z0-9]+", "-", cwd).lower().strip("-")
    slug = slug[:40] or "workspace"
    digest = hashlib.blake2b(cwd.encode("utf-8"), digest_size=8).hexdigest()
    return f"{slug}-{digest}"


def decode_grok_cwd_dirname(name: str, group_dir: Path | None = None) -> str:
    """Recover the original cwd from a Grok sessions group directory name.

    Mirrors ``decode_cwd_from_dirname`` in the Grok source: URL-decoded
    absolute paths start with ``/`` (Unix) or a drive letter (Windows); the
    slug-hash fallback never does, so a ``.cwd`` file is read instead.
    """
    from urllib.parse import unquote

    decoded = unquote(name)
    if decoded.startswith("/") or (len(decoded) >= 2 and decoded[1] == ":"):
        return decoded
    if group_dir is not None:
        try:
            cwd_file = Path(group_dir) / ".cwd"
            if cwd_file.is_file():
                return cwd_file.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    return ""
