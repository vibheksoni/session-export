from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from session_sdk.json_types import JsonObject


@dataclass(frozen=True, slots=True)
class SessionSummary:
    provider: str
    session_id: str
    cwd: str
    timestamp: str
    path: Path
    message_count: int


@dataclass(frozen=True, slots=True)
class TextMessage:
    role: str
    text: str
    timestamp: str
    model: str | None = None
    provider: str | None = None
    api: str | None = None
    is_compaction: bool = False
    is_contextual: bool = False


@dataclass(frozen=True, slots=True)
class NativeSession:
    provider: str
    session_id: str
    cwd: str
    timestamp: str
    path: Path
    records: list[JsonObject] = field(repr=False)

    def summary(self, message_count: int) -> SessionSummary:
        return SessionSummary(
            provider=self.provider,
            session_id=self.session_id,
            cwd=self.cwd,
            timestamp=self.timestamp,
            path=self.path,
            message_count=message_count,
        )


@dataclass(frozen=True, slots=True)
class ConversionPlan:
    source: NativeSession
    destination: Path
    records: list[JsonObject] = field(repr=False)
    services: tuple[Path, ...] = ()

