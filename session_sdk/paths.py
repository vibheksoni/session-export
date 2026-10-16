from __future__ import annotations

import os
import re
import string
import sys
from datetime import UTC, datetime
from pathlib import Path
from hashlib import sha256
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5


class SessionIdFactory:
    def __init__(self, *, preserve_ids: bool = True) -> None:
        self._preserve_ids = preserve_ids

    @property
    def preserve_ids(self) -> bool:
        return self._preserve_ids

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
    def freebuff_home(self) -> Path:
        env = os.environ.get("FREEBUFF_CONFIG_DIR")
        if env:
            return Path(env)
        return self._home / ".config" / "freebuff-desktop"

    @property
    def t3_home(self) -> Path:
        env = os.environ.get("T3CODE_HOME")
        if env:
            return Path(env)
        return self._home / ".t3"

    @property
    def omp_agent_home(self) -> Path:
        """The directory holding OMP's ``sessions/`` folder, resolved the way omp resolves it.

        ``~/.omp/agent`` by default. ``PI_CONFIG_DIR`` renames ``.omp``, ``OMP_PROFILE`` (or the
        legacy ``PI_PROFILE``) selects ``~/.omp/profiles/<name>/agent``, and ``PI_CODING_AGENT_DIR``
        replaces the agent directory when no profile is active. On Linux and macOS, data that
        ``omp config init-xdg`` moved to ``$XDG_DATA_HOME/omp`` is used when that folder exists.
        """
        profile = _omp_profile()
        config_root = self._home / (os.environ.get("PI_CONFIG_DIR") or ".omp")
        if profile:
            config_root = config_root / "profiles" / profile
        default_agent = config_root / "agent"
        override = None if profile else os.environ.get("PI_CODING_AGENT_DIR")
        agent = Path(os.path.abspath(override)) if override else default_agent
        xdg_data = os.environ.get("XDG_DATA_HOME")
        if agent == default_agent and xdg_data and sys.platform in ("linux", "darwin"):
            app_root = Path(xdg_data) / "omp"
            if profile:
                app_root = app_root / "profiles" / profile
            if app_root.exists():
                return app_root
        return agent

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


def stable_session_uuid(source_id: str) -> str:
    """UUID for a source session id. A UUID passes through unchanged. Any other id maps to a
    UUIDv5, so exporting the same session twice writes the same file. T3 thread ids contain
    colons, which Windows does not allow in file names."""
    if is_uuid(source_id):
        return source_id
    return str(uuid5(NAMESPACE_URL, f"unisessions:{source_id}"))


def stable_opencode_id(seed: str, timestamp: str) -> str:
    """Deterministic OpenCode session id. Same time prefix as opencode_id, with a suffix hashed
    from seed instead of drawn at random."""
    epoch_ms = iso_to_epoch_ms(timestamp)
    encoded = ~((epoch_ms * 0x1000 + 1) & ((1 << 48) - 1)) & ((1 << 48) - 1)
    digest = int(sha256(seed.encode("utf-8")).hexdigest(), 16)
    suffix = []
    for _ in range(14):
        digest, remainder = divmod(digest, 62)
        suffix.append(_BASE62_CHARS[remainder])
    return f"ses_{encoded:012x}{''.join(suffix)}"


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


_OMP_PROFILE_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_WINDOWS_RESERVED_NAME_RE = re.compile(r"(?:CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(?:\..*)?", re.IGNORECASE)
_OMP_SEPARATORS_RE = re.compile(r"[/\\:]")


def _omp_profile() -> str | None:
    """The active omp profile: ``OMP_PROFILE``, else the legacy ``PI_PROFILE``. An empty value or
    ``default`` selects the default profile. omp refuses to start with an invalid profile name;
    here an invalid name also falls back to the default profile."""
    selected = os.environ.get("OMP_PROFILE")
    if selected is None:
        selected = os.environ.get("PI_PROFILE")
    name = (selected or "").strip()
    if not name or name == "default" or name.endswith("."):
        return None
    if not _OMP_PROFILE_RE.fullmatch(name) or _WINDOWS_RESERVED_NAME_RE.fullmatch(name):
        return None
    return name


def _node_tmpdir() -> str:
    """``os.tmpdir()`` as Node and Bun compute it. omp names its ``-tmp`` buckets against it, and
    it differs from ``tempfile.gettempdir()``, which also probes other folders."""
    if sys.platform == "win32":
        system_root = os.environ.get("SystemRoot") or os.environ.get("windir") or ""
        return os.environ.get("TEMP") or os.environ.get("TMP") or f"{system_root}\\temp"
    return os.environ.get("TMPDIR") or os.environ.get("TMP") or os.environ.get("TEMP") or "/tmp"


def _omp_canonical_path(path: str) -> str:
    """Absolute path with symlinks resolved, or just absolute when the path does not exist (omp's
    ``resolveEquivalentPath``). A non-strict realpath would resolve the existing prefix only."""
    absolute = os.path.abspath(path)
    try:
        return os.path.realpath(absolute, strict=True)
    except OSError:
        return absolute


def _omp_relative_within(root: str, path: str) -> str | None:
    """``path`` relative to ``root`` ("" when equal), or None when it lies outside ``root``."""
    try:
        relative = os.path.relpath(path, root)
    except ValueError:  # different drives on Windows
        return None
    if relative == ".":
        return ""
    # Same test as omp, which also treats a child named like "..name" as outside.
    if relative.startswith("..") or os.path.isabs(relative):
        return None
    return relative


def _encode_omp_relative(prefix: str, relative: str) -> str:
    encoded = _OMP_SEPARATORS_RE.sub("-", relative)
    if not encoded:
        return prefix
    return f"{prefix}{encoded}" if prefix.endswith("-") else f"{prefix}-{encoded}"


def _encode_omp_absolute(path: str) -> str:
    stripped = path[1:] if path[:1] in ("/", "\\") else path
    return f"--{_OMP_SEPARATORS_RE.sub('-', stripped)}--"


def encode_omp_cwd(cwd: str, *, home: Path | None = None, temp_root: Path | None = None) -> str:
    """OMP's session bucket name for a cwd (``getDefaultSessionDirName`` in omp's session-paths.ts).

    A cwd inside the temp root becomes ``-tmp-<relative>``, one inside home ``-<relative>`` (home
    itself is ``-``), and any other ``--<absolute>--``; ``/``, ``\\`` and ``:`` turn into ``-``.
    All paths are canonicalized first, as omp does, so a symlinked cwd lands in the bucket of its
    target. A cwd that is not absolute on this platform (a Windows path read on Linux, say) has no
    canonical form here and is encoded as given.
    """
    if not os.path.isabs(cwd):
        return _encode_omp_absolute(cwd)
    canonical = _omp_canonical_path(cwd)
    # The temp root is checked first: it is the more specific root where it nests inside home.
    relative = _omp_relative_within(_omp_canonical_path(str(temp_root) if temp_root else _node_tmpdir()), canonical)
    if relative is not None:
        return _encode_omp_relative("-tmp", relative)
    relative = _omp_relative_within(_omp_canonical_path(str(home or Path.home())), canonical)
    if relative is not None:
        return _encode_omp_relative("-", relative)
    return _encode_omp_absolute(canonical)


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
