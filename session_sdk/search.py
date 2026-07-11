from __future__ import annotations

import re
import sqlite3

try:
    import re2 as _re2
except ImportError:
    _re2 = None
    _RE2_ERRORS: tuple[type[BaseException], ...] = ()
else:
    _RE2_ERRORS = tuple({ValueError, RuntimeError, getattr(_re2, "Error", ValueError), getattr(_re2, "error", ValueError)})
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal, Sequence

from session_sdk.converters import MessageExtractor
from session_sdk.models import NativeSession, SessionSummary, TextMessage
from session_sdk.stores import ClaudeStore, CodexStore, DevinStore, FactoryStore, OpenCodeStore, PiStore, SessionStore

Provider = Literal["all", "codex", "pi", "opencode", "claude", "devin", "factory"]
CwdMatch = Literal["exact", "contains", "prefix"]
MatchMode = Literal["literal", "regex", "all_keywords", "any_keywords"]
StalePolicy = Literal["refresh", "skip", "error"]
Matcher = Callable[[str], tuple[int, int] | None]

_RAW_MATCH_CAP = 200
_DEFAULT_MAX_PER_SESSION = 5

_CODE_BLOCK_RE = re.compile(r"```[\s\S]*?```|`[^`\n]+`", re.MULTILINE)


def _batched(seq: list[str], size: int) -> list[list[str]]:
    return [seq[i:i + size] for i in range(0, len(seq), size)]


@dataclass(frozen=True, slots=True)
class ChatSearchResult:
    provider: str
    session_id: str
    cwd: str
    timestamp: str
    path: str
    message_index: int
    role: str
    message_type: str
    snippet: str
    relevance_score: float = 0.0
    rank: int = 0
    duplicate_count: int = 1
    first_seen: str | None = None
    match_positions: list[list[int]] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "rank": self.rank,
            "relevance_score": round(self.relevance_score, 2),
            "provider": self.provider,
            "session_id": self.session_id,
            "timestamp": self.timestamp,
            "role": self.role,
            "message_type": self.message_type,
            "snippet": self.snippet,
            "duplicate_count": self.duplicate_count,
            "first_seen": self.first_seen,
            "match_positions": self.match_positions,
        }


@dataclass(frozen=True, slots=True)
class SessionSearchResult:
    provider: str
    session_id: str
    cwd: str
    timestamp: str
    match_count: int
    top_snippets: list[str]

    def as_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "session_id": self.session_id,
            "cwd": self.cwd,
            "timestamp": self.timestamp,
            "match_count": self.match_count,
            "top_snippets": self.top_snippets,
        }


@dataclass(frozen=True, slots=True)
class SearchResponse:
    results: list[ChatSearchResult]
    total_matches: int
    deduplicated: int
    sessions_searched: int
    messages_searched: int
    truncated: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "search_metadata": {
                "total_matches": self.total_matches,
                "returned": len(self.results),
                "deduplicated": self.deduplicated,
                "sessions_searched": self.sessions_searched,
                "messages_searched": self.messages_searched,
                "truncated": self.truncated,
            },
            "results": [result.as_dict() for result in self.results],
        }


@dataclass(frozen=True, slots=True)
class _PreparedSession:
    provider: str
    session_id: str
    cwd: str
    timestamp: str
    path: Path
    mtime_ns: int
    size: int
    messages: list[TextMessage]


class SessionSearchIndex:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path) if path else ":memory:")
        self._db.row_factory = sqlite3.Row
        self._setup()

    def close(self) -> None:
        self._db.close()

    def ensure(
        self,
        store: SessionStore,
        summaries: Sequence[SessionSummary],
        extractor: MessageExtractor,
        *,
        workers: int = 1,
        max_refresh_sessions: int | None = None,
        max_refresh_bytes: int | None = None,
    ) -> int:
        stale = [summary for summary in summaries if self._needs_index(summary)]
        refresh_bytes = sum(summary.path.stat().st_size for summary in stale)
        if max_refresh_sessions is not None and len(stale) > max_refresh_sessions:
            raise ValueError(f"refresh would parse {len(stale)} sessions; max_refresh_sessions={max_refresh_sessions}")
        if max_refresh_bytes is not None and refresh_bytes > max_refresh_bytes:
            raise ValueError(f"refresh would parse {refresh_bytes} bytes; max_refresh_bytes={max_refresh_bytes}")
        if not stale:
            return 0
        with self._db:
            if workers <= 1 or len(stale) == 1:
                for summary in stale:
                    self._index_prepared(self._prepare_session(store, summary, extractor))
            else:
                with ThreadPoolExecutor(max_workers=min(workers, len(stale)), thread_name_prefix="session-index") as executor:
                    futures = [executor.submit(self._prepare_session, store, summary, extractor) for summary in stale]
                    for future in as_completed(futures):
                        self._index_prepared(future.result())
        self._db.execute("PRAGMA optimize")
        return len(stale)

    def status(self, summaries: Sequence[SessionSummary]) -> dict[str, int]:
        rows = {str(row["path"]): row for row in self._db.execute("SELECT path, mtime_ns, size, message_count FROM sessions")}
        indexed = 0
        stale = 0
        missing = 0
        indexed_messages = 0
        refresh_bytes = 0
        for summary in summaries:
            stat = summary.path.stat()
            row = rows.get(self._path_key(summary.path))
            if row is None:
                missing += 1
                refresh_bytes += stat.st_size
                continue
            indexed += 1
            indexed_messages += int(row["message_count"])
            if int(row["mtime_ns"]) != stat.st_mtime_ns or int(row["size"]) != stat.st_size:
                stale += 1
                refresh_bytes += stat.st_size
        deleted = sum(1 for path in rows if not Path(path).exists())
        return {
            "sessions": len(summaries),
            "indexed_sessions": indexed,
            "missing_sessions": missing,
            "stale_sessions": stale,
            "refresh_sessions": missing + stale,
            "refresh_bytes": refresh_bytes,
            "indexed_messages": indexed_messages,
            "deleted_indexed_paths": deleted,
        }

    def message_count(self, summaries: Sequence[SessionSummary]) -> int:
        path_keys = [self._path_key(s.path) for s in summaries]
        if not path_keys:
            return 0
        total = 0
        for batch in _batched(path_keys, 900):
            placeholders = ",".join("?" for _ in batch)
            row = self._db.execute(
                f"SELECT COALESCE(SUM(message_count), 0) AS total FROM sessions WHERE path IN ({placeholders})",
                batch,
            ).fetchone()
            total += int(row["total"]) if row else 0
        return total

    def raw_search_rows(
        self,
        *,
        query: str | None = None,
        keywords: list[str] | None = None,
        regex: str | None = None,
        mode: MatchMode = "literal",
        provider: Provider = "all",
        roles: set[str] | None = None,
        message_types: set[str] | None = None,
        include_contextual: bool = True,
        include_compactions: bool = True,
        after: str | None = None,
        before: str | None = None,
        limit: int | None = 1000,
    ) -> list[sqlite3.Row]:
        """Return raw indexed candidate rows for SDK callers that want custom ranking or filtering."""
        fts = self.build_fts_query(query=query, keywords=keywords, regex=regex, mode=mode)
        clauses: list[str] = []
        params: list[object] = []
        if provider != "all":
            clauses.append("provider = ?")
            params.append(provider)
        if fts:
            clauses.append("messages_fts MATCH ?")
            params.append(fts)
        if roles:
            clauses.append(f"role IN ({','.join('?' for _ in roles)})")
            params.extend(sorted(roles))
        if message_types:
            clauses.append(f"message_type IN ({','.join('?' for _ in message_types)})")
            params.extend(sorted(message_types))
        if not include_contextual:
            clauses.append("message_type != 'contextual'")
        if not include_compactions:
            clauses.append("message_type != 'compaction'")
        if after:
            clauses.append("timestamp >= ?")
            params.append(after)
        if before:
            clauses.append("timestamp <= ?")
            params.append(before)
        where = " AND ".join(clauses) if clauses else "1 = 1"
        sql = f"""
            SELECT provider, session_id, path, cwd, timestamp, message_index, role, message_type, text
            FROM messages_fts
            WHERE {where}
            {"ORDER BY rank" if fts else "ORDER BY timestamp DESC, message_index ASC"}
            {"LIMIT ?" if limit is not None else ""}
        """
        if limit is not None:
            params.append(limit)
        return list(self._db.execute(sql, params))

    def search(
        self,
        *,
        summaries: Sequence[SessionSummary],
        matcher: Matcher,
        query: str | None,
        keywords: list[str] | None,
        regex: str | None,
        mode: MatchMode,
        roles: set[str],
        message_types: set[str],
        include_contextual: bool,
        include_compactions: bool,
        exclude_keywords: list[str] | None,
        after: str | None,
        before: str | None,
        max_results: int,
        context_chars: int,
        max_per_session: int,
    ) -> SearchResponse:
        path_keys = [self._path_key(summary.path) for summary in summaries]
        if not path_keys or max_results <= 0:
            return SearchResponse([], 0, 0, len(summaries), 0, False)

        fts = self._fts_query(query=query, keywords=keywords, regex=regex, mode=mode)
        is_regex = bool(regex or mode == "regex")
        row_limit = max(max_results * 20, _RAW_MATCH_CAP) if not is_regex else 2000
        rows = self._rows(
            path_keys=path_keys,
            fts=fts,
            roles=roles,
            message_types=message_types,
            include_contextual=include_contextual,
            include_compactions=include_compactions,
            after=after,
            before=before,
            limit=row_limit,
        )
        messages_searched = self.message_count(summaries)

        exclude_patterns = [re.compile(ex, re.IGNORECASE) for ex in (exclude_keywords or []) if ex]

        scored: list[tuple[float, str, ChatSearchResult]] = []
        seen_text: dict[str, int] = {}
        first_seen: dict[str, str] = {}
        per_session: dict[str, int] = {}
        raw_count = 0
        truncated = False

        for row in rows:
            text = str(row["text"])
            match = matcher(text)
            if match is None:
                continue
            if any(pat.search(text) for pat in exclude_patterns):
                continue
            raw_count += 1
            if raw_count > _RAW_MATCH_CAP:
                truncated = True
                break

            text_key = text.strip()[:500]
            row_ts = str(row["timestamp"])
            if text_key in seen_text:
                seen_text[text_key] += 1
                if row_ts < first_seen.get(text_key, row_ts):
                    first_seen[text_key] = row_ts
                continue
            seen_text[text_key] = 1
            first_seen[text_key] = row_ts

            sid = str(row["session_id"])
            if max_per_session > 0 and per_session.get(sid, 0) >= max_per_session:
                del seen_text[text_key]
                del first_seen[text_key]
                continue
            per_session[sid] = per_session.get(sid, 0) + 1

            score = self._relevance_score(text, match, str(row["message_type"]), keywords, mode)
            positions = self._all_match_positions(text, matcher)

            scored.append((score, text_key, ChatSearchResult(
                provider=str(row["provider"]),
                session_id=sid,
                cwd=str(row["cwd"]),
                timestamp=row_ts,
                path=str(row["path"]),
                message_index=int(row["message_index"]),
                role=str(row["role"]),
                message_type=str(row["message_type"]),
                snippet=self._snippet(text, match, context_chars),
                relevance_score=score,
                match_positions=positions,
            )))

            if len(scored) >= max_results:
                break

        scored.sort(key=lambda pair: pair[0], reverse=True)
        results = []
        for rank, (score, text_key, result) in enumerate(scored[:max_results], 1):
            dup_count = seen_text.get(text_key, 1)
            first_ts = first_seen.get(text_key)
            results.append(ChatSearchResult(
                provider=result.provider,
                session_id=result.session_id,
                cwd=result.cwd,
                timestamp=result.timestamp,
                path=result.path,
                message_index=result.message_index,
                role=result.role,
                message_type=result.message_type,
                snippet=result.snippet,
                relevance_score=score,
                rank=rank,
                duplicate_count=dup_count,
                first_seen=first_ts if dup_count > 1 else None,
                match_positions=result.match_positions,
            ))

        deduped = sum(count - 1 for count in seen_text.values() if count > 1)
        return SearchResponse(
            results=results,
            total_matches=raw_count,
            deduplicated=deduped,
            sessions_searched=len(summaries),
            messages_searched=messages_searched,
            truncated=truncated,
        )

    def search_sessions(
        self,
        *,
        summaries: Sequence[SessionSummary],
        matcher: Matcher,
        query: str | None,
        keywords: list[str] | None,
        regex: str | None,
        mode: MatchMode,
        roles: set[str],
        message_types: set[str],
        include_contextual: bool,
        include_compactions: bool,
        exclude_keywords: list[str] | None,
        after: str | None,
        before: str | None,
        max_snippets: int = 3,
    ) -> list[SessionSearchResult]:
        path_keys = [self._path_key(summary.path) for summary in summaries]
        if not path_keys:
            return []
        fts = self._fts_query(query=query, keywords=keywords, regex=regex, mode=mode)
        is_regex = bool(regex or mode == "regex")
        rows = self._rows(
            path_keys=path_keys,
            fts=fts,
            roles=roles,
            message_types=message_types,
            include_contextual=include_contextual,
            include_compactions=include_compactions,
            after=after,
            before=before,
            limit=2000 if is_regex else _RAW_MATCH_CAP,
        )
        exclude_patterns = [re.compile(ex, re.IGNORECASE) for ex in (exclude_keywords or []) if ex]
        by_session: dict[str, SessionSearchResult] = {}
        session_counts: dict[str, int] = {}
        session_snippets: dict[str, list[str]] = {}
        global_seen_snippets: set[str] = set()
        session_seen_text: dict[str, set[str]] = {}

        for row in rows:
            text = str(row["text"])
            match = matcher(text)
            if match is None:
                continue
            if any(pat.search(text) for pat in exclude_patterns):
                continue
            sid = str(row["session_id"])
            text_key = text.strip()[:500]
            seen = session_seen_text.setdefault(sid, set())
            if text_key in seen:
                continue
            seen.add(text_key)
            session_counts[sid] = session_counts.get(sid, 0) + 1
            snippet = self._snippet(text, match, 200)
            snippet_key = snippet[:200]
            if len(session_snippets.get(sid, [])) < max_snippets and snippet_key not in global_seen_snippets:
                session_snippets.setdefault(sid, []).append(snippet)
                global_seen_snippets.add(snippet_key)
            if sid not in by_session:
                by_session[sid] = SessionSearchResult(
                    provider=str(row["provider"]),
                    session_id=sid,
                    cwd=str(row["cwd"]),
                    timestamp=str(row["timestamp"]),
                    match_count=0,
                    top_snippets=[],
                )

        results = []
        for sid, result in by_session.items():
            results.append(SessionSearchResult(
                provider=result.provider,
                session_id=sid,
                cwd=result.cwd,
                timestamp=result.timestamp,
                match_count=session_counts[sid],
                top_snippets=session_snippets.get(sid, []),
            ))
        results.sort(key=lambda r: r.match_count, reverse=True)
        return results

    def _setup(self) -> None:
        self._db.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=NORMAL;
            CREATE TABLE IF NOT EXISTS sessions (
                path TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                session_id TEXT NOT NULL,
                cwd TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                mtime_ns INTEGER NOT NULL,
                size INTEGER NOT NULL,
                message_count INTEGER NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                provider UNINDEXED,
                session_id UNINDEXED,
                path UNINDEXED,
                cwd UNINDEXED,
                timestamp UNINDEXED,
                message_index UNINDEXED,
                role UNINDEXED,
                message_type UNINDEXED,
                text,
                tokenize='unicode61'
            );
            """
        )

    def _needs_index(self, summary: SessionSummary) -> bool:
        stat = summary.path.stat()
        row = self._db.execute(
            "SELECT mtime_ns, size FROM sessions WHERE path = ?",
            (self._path_key(summary.path),),
        ).fetchone()
        return row is None or int(row["mtime_ns"]) != stat.st_mtime_ns or int(row["size"]) != stat.st_size

    def _prepare_session(self, store: SessionStore, summary: SessionSummary, extractor: MessageExtractor) -> _PreparedSession:
        session = store.load_path(summary.path)
        stat = session.path.stat()
        return _PreparedSession(
            provider=session.provider,
            session_id=session.session_id,
            cwd=session.cwd,
            timestamp=session.timestamp,
            path=session.path,
            mtime_ns=stat.st_mtime_ns,
            size=stat.st_size,
            messages=self._messages(session, extractor),
        )

    def _index_prepared(self, prepared: _PreparedSession) -> None:
        path = self._path_key(prepared.path)
        self._db.execute("DELETE FROM messages_fts WHERE path = ?", (path,))
        self._db.execute("DELETE FROM sessions WHERE path = ?", (path,))
        rows = [
                (
                    prepared.provider,
                    prepared.session_id,
                    path,
                    prepared.cwd,
                    message.timestamp or prepared.timestamp,
                    index,
                    message.role,
                    self._message_type(message),
                    message.text,
                )
                for index, message in enumerate(prepared.messages)
            ]
        self._db.executemany(
            """
            INSERT INTO messages_fts(provider, session_id, path, cwd, timestamp, message_index, role, message_type, text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        self._db.execute(
            """
            INSERT INTO sessions(path, provider, session_id, cwd, timestamp, mtime_ns, size, message_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (path, prepared.provider, prepared.session_id, prepared.cwd, prepared.timestamp, prepared.mtime_ns, prepared.size, len(prepared.messages)),
        )

    def _rows(
        self,
        *,
        path_keys: list[str],
        fts: str | None,
        roles: set[str],
        message_types: set[str],
        include_contextual: bool,
        include_compactions: bool,
        after: str | None,
        before: str | None,
        limit: int | None,
    ) -> list[sqlite3.Row]:
        all_rows: list[sqlite3.Row] = []
        for batch in _batched(path_keys, 900):
            all_rows.extend(self._rows_batch(
                path_keys=batch,
                fts=fts,
                roles=roles,
                message_types=message_types,
                include_contextual=include_contextual,
                include_compactions=include_compactions,
                after=after,
                before=before,
                limit=limit,
            ))
        return all_rows

    def _rows_batch(
        self,
        *,
        path_keys: list[str],
        fts: str | None,
        roles: set[str],
        message_types: set[str],
        include_contextual: bool,
        include_compactions: bool,
        after: str | None,
        before: str | None,
        limit: int | None,
    ) -> list[sqlite3.Row]:
        clauses = [f"path IN ({','.join('?' for _ in path_keys)})"]
        params: list[object] = list(path_keys)
        if fts:
            clauses.append("messages_fts MATCH ?")
            params.append(fts)
        if roles:
            clauses.append(f"role IN ({','.join('?' for _ in roles)})")
            params.extend(sorted(roles))
        if message_types:
            clauses.append(f"message_type IN ({','.join('?' for _ in message_types)})")
            params.extend(sorted(message_types))
        if not include_contextual:
            clauses.append("message_type != 'contextual'")
        if not include_compactions:
            clauses.append("message_type != 'compaction'")
        if after:
            clauses.append("timestamp >= ?")
            params.append(after)
        if before:
            clauses.append("timestamp <= ?")
            params.append(before)
        sql = """
            SELECT provider, session_id, path, cwd, timestamp, message_index, role, message_type, text
            FROM {table}
            WHERE {where}
            {order_by}
            {limit}
        """.format(
            table="messages_fts",
            where=" AND ".join(clauses),
            order_by="ORDER BY rank" if fts else "ORDER BY timestamp DESC, message_index ASC",
            limit="LIMIT ?" if limit is not None else "",
        )
        if limit is not None:
            params.append(limit)
        return list(self._db.execute(sql, params))

    @staticmethod
    def _messages(session: NativeSession, extractor: MessageExtractor) -> list[TextMessage]:
        if session.provider == "codex":
            return extractor.from_codex(session)
        if session.provider == "pi":
            return extractor.from_pi(session)
        if session.provider == "opencode":
            return extractor.from_opencode(session)
        if session.provider == "claude":
            return extractor.from_claude(session)
        if session.provider == "devin":
            return extractor.from_devin(session)
        if session.provider == "factory":
            return extractor.from_factory(session)
        return []

    @staticmethod
    def build_fts_query(*, query: str | None, keywords: list[str] | None, regex: str | None, mode: MatchMode) -> str | None:
        if regex or mode == "regex":
            return SessionSearchIndex._regex_fts_query(regex or query or "")
        values = [value for value in (keywords or ([query] if query else [])) if value]
        if not values:
            return None
        if mode == "literal":
            return SessionSearchIndex._terms_query(values[0], joiner=" AND ")
        if mode == "all_keywords":
            return " AND ".join(filter(None, (SessionSearchIndex._terms_query(value, joiner=" AND ") for value in values))) or None
        if mode == "any_keywords":
            return " OR ".join(filter(None, (SessionSearchIndex._terms_query(value, joiner=" AND ") for value in values))) or None
        return None

    _fts_query = build_fts_query

    @staticmethod
    def _regex_fts_query(pattern: str) -> str | None:
        tokens = re.findall(r"[A-Za-z][A-Za-z0-9_]{1,}", pattern)
        seen: set[str] = set()
        unique: list[str] = []
        for tok in tokens:
            low = tok.lower()
            if low not in seen:
                seen.add(low)
                unique.append(low)
        if not unique:
            return None
        if len(unique) > 20:
            unique = unique[:20]
        escaped = [t.replace(chr(34), chr(34) + chr(34)) for t in unique]
        return " OR ".join(f'"{t}"' for t in escaped)

    @staticmethod
    def _terms_query(value: str, *, joiner: str) -> str | None:
        variants = SessionSearchIndex._term_phrase_variants(value)
        if not variants:
            return None
        queries = []
        for terms in variants:
            if len(terms) == 1:
                escaped = terms[0].replace(chr(34), chr(34) + chr(34))
                queries.append(f'"{escaped}"')
            else:
                phrase = " ".join(terms)
                escaped = phrase.replace(chr(34), chr(34) + chr(34))
                queries.append(f'"{escaped}"')
        if len(queries) == 1:
            return queries[0]
        return "(" + " OR ".join(queries) + ")"

    @staticmethod
    def _term_phrase_variants(value: str) -> list[list[str]]:
        raw = value.replace("\u2019", "'")
        variants: list[list[str]] = []
        seen: set[tuple[str, ...]] = set()
        for candidate in (raw, raw.replace("'", ""), SessionSearchIndex._split_common_contractions(raw.replace("'", ""))):
            terms = re.findall(r"[\w]+", candidate, flags=re.UNICODE)
            key = tuple(term.lower() for term in terms)
            if terms and key not in seen:
                seen.add(key)
                variants.append(list(key))
        return variants

    @staticmethod
    def _split_common_contractions(value: str) -> str:
        splits = {
            "dont": "don t",
            "cant": "can t",
            "wont": "won t",
            "isnt": "isn t",
            "arent": "aren t",
            "wasnt": "wasn t",
            "werent": "weren t",
            "didnt": "didn t",
            "doesnt": "doesn t",
            "couldnt": "couldn t",
            "shouldnt": "shouldn t",
            "wouldnt": "wouldn t",
            "havent": "haven t",
            "hasnt": "hasn t",
            "hadnt": "hadn t",
        }
        words = re.findall(r"\w+|\W+", value, flags=re.UNICODE)
        return "".join(splits.get(word.lower(), word) for word in words)

    @staticmethod
    def _path_key(path: Path) -> str:
        return str(path.resolve()).lower()

    @staticmethod
    def _message_type(message: TextMessage) -> str:
        if message.is_contextual:
            return "contextual"
        if message.is_compaction:
            return "compaction"
        return "message"

    @staticmethod
    def _snippet(text: str, span: tuple[int, int], context_chars: int) -> str:
        start = max(0, span[0] - context_chars)
        end = min(len(text), span[1] + context_chars)
        prefix = "..." if start else ""
        suffix = "..." if end < len(text) else ""
        return prefix + text[start:end].replace("\r", " ").replace("\n", " ") + suffix

    @staticmethod
    def _relevance_score(
        text: str,
        span: tuple[int, int],
        message_type: str,
        keywords: list[str] | None,
        mode: MatchMode,
    ) -> float:
        match_len = span[1] - span[0]
        text_len = max(len(text), 1)
        density = match_len / text_len
        position_score = 1.0 - (span[0] / text_len) * 0.3
        type_penalty = 0.15 if message_type == "compaction" else 0.0
        code_penalty = 0.0
        if _CODE_BLOCK_RE.search(text):
            code_ratio = sum(m.end() - m.start() for m in _CODE_BLOCK_RE.finditer(text)) / text_len
            code_penalty = code_ratio * 0.5
        proximity_multiplier = 1.0
        if keywords and len(keywords) >= 2 and mode in ("all_keywords", "any_keywords"):
            lower_text = text.lower().replace("\u2019", "'").replace("'", "")
            positions = []
            for kw in keywords:
                kw_lower = kw.lower().replace("\u2019", "'").replace("'", "")
                idx = lower_text.find(kw_lower)
                if idx >= 0:
                    positions.append(idx)
            if len(positions) >= 2:
                positions.sort()
                spread = positions[-1] - positions[0]
                if spread <= 30:
                    proximity_multiplier = 5.0
                elif spread <= 100:
                    proximity_multiplier = 3.0
                elif spread <= 300:
                    proximity_multiplier = 2.0
                elif spread <= 1000:
                    proximity_multiplier = 1.0
                else:
                    proximity_multiplier = 0.1
        return max(0.0, (density * 10.0 + position_score - type_penalty - code_penalty) * proximity_multiplier)

    @staticmethod
    def _all_match_positions(text: str, matcher: Matcher) -> list[list[int]]:
        positions: list[list[int]] = []
        start = 0
        while True:
            span = matcher(text[start:])
            if span is None:
                break
            abs_start = start + span[0]
            abs_end = start + span[1]
            positions.append([abs_start, abs_end])
            start = abs_end if abs_end > start else start + 1
            if len(positions) >= 5:
                break
        return positions


class SessionSearchEngine:
    def __init__(self, codex: CodexStore, pi: PiStore, opencode: OpenCodeStore, index_path: Path | None = None, claude: ClaudeStore | None = None, devin: DevinStore | None = None, factory: FactoryStore | None = None) -> None:
        self._stores: dict[str, SessionStore] = {
            "codex": codex,
            "pi": pi,
            "opencode": opencode,
        }
        if claude is not None:
            self._stores["claude"] = claude
        if devin is not None:
            self._stores["devin"] = devin
        if factory is not None:
            self._stores["factory"] = factory
        self._extractor = MessageExtractor()
        self._index = SessionSearchIndex(index_path)

    def list_chats(
        self,
        *,
        provider: Provider = "all",
        cwd: str | None = None,
        cwd_match: CwdMatch = "exact",
        limit: int = 100,
        workers: int = 1,
    ) -> list[dict[str, object]]:
        chats: list[SessionSummary] = []
        for _name, store in self._selected_stores(provider):
            for summary in store.list_metadata(workers=max(1, workers)):
                if self._cwd_matches(summary.cwd, cwd, cwd_match):
                    chats.append(summary)
        chats.sort(key=lambda s: s.timestamp, reverse=True)
        return [self._summary_dict(chat) for chat in chats[: max(0, limit)]]

    def index_status(
        self,
        *,
        provider: Provider = "all",
        cwd: str | None = None,
        cwd_match: CwdMatch = "exact",
        workers: int = 1,
    ) -> dict[str, dict[str, int] | int]:
        status: dict[str, dict[str, int] | int] = {}
        for name, store in self._selected_stores(provider):
            summaries = self._scoped_summaries(store, cwd=cwd, cwd_match=cwd_match, workers=workers)
            status[name] = self._index.status(summaries)
        status["total_refresh_sessions"] = sum(int(value["refresh_sessions"]) for value in status.values() if isinstance(value, dict))
        status["total_refresh_bytes"] = sum(int(value["refresh_bytes"]) for value in status.values() if isinstance(value, dict))
        return status

    def refresh_index(
        self,
        *,
        provider: Provider = "all",
        cwd: str | None = None,
        cwd_match: CwdMatch = "exact",
        workers: int = 1,
        max_refresh_sessions: int | None = None,
        max_refresh_bytes: int | None = None,
    ) -> dict[str, int]:
        indexed: dict[str, int] = {}
        for name, store in self._selected_stores(provider):
            summaries = self._scoped_summaries(store, cwd=cwd, cwd_match=cwd_match, workers=workers)
            indexed[name] = self._index.ensure(
                store,
                summaries,
                self._extractor,
                workers=workers,
                max_refresh_sessions=max_refresh_sessions,
                max_refresh_bytes=max_refresh_bytes,
            )
        indexed["total"] = sum(indexed.values())
        return indexed

    def raw_search_rows(
        self,
        *,
        query: str | None = None,
        keywords: list[str] | None = None,
        regex: str | None = None,
        mode: MatchMode = "literal",
        provider: Provider = "all",
        roles: list[str] | None = None,
        message_types: list[str] | None = None,
        include_contextual: bool = True,
        include_compactions: bool = True,
        after: str | None = None,
        before: str | None = None,
        limit: int | None = 1000,
    ) -> list[dict[str, object]]:
        rows = self._index.raw_search_rows(
            query=query,
            keywords=keywords,
            regex=regex,
            mode=mode,
            provider=provider,
            roles=set(roles or []),
            message_types=set(message_types or []),
            include_contextual=include_contextual,
            include_compactions=include_compactions,
            after=after,
            before=before,
            limit=limit,
        )
        return [dict(row) for row in rows]

    def search(
        self,
        *,
        query: str | None = None,
        keywords: list[str] | None = None,
        regex: str | None = None,
        exclude_keywords: list[str] | None = None,
        provider: Provider = "all",
        cwd: str | None = None,
        cwd_match: CwdMatch = "exact",
        session_ids: list[str] | None = None,
        roles: list[str] | None = None,
        message_types: list[str] | None = None,
        mode: MatchMode = "literal",
        case_sensitive: bool = False,
        include_contextual: bool = False,
        include_compactions: bool = False,
        max_results: int = 50,
        context_chars: int = 300,
        max_per_session: int = _DEFAULT_MAX_PER_SESSION,
        after: str | None = None,
        before: str | None = None,
        workers: int = 1,
        stale_policy: StalePolicy = "skip",
        max_refresh_sessions: int | None = None,
        max_refresh_bytes: int | None = None,
    ) -> dict[str, object]:
        matcher = self._matcher(query=query, keywords=keywords, regex=regex, mode=mode, case_sensitive=case_sensitive)
        wanted_ids = set(session_ids or [])
        wanted_roles = set(roles or [])
        wanted_types = set(message_types or [])
        if wanted_roles and not wanted_types:
            include_compactions = False

        all_results: list[ChatSearchResult] = []
        total_matches = 0
        total_deduped = 0
        total_sessions = 0
        total_messages = 0
        truncated = False

        for name, store in self._selected_stores(provider):
            summaries = [
                summary
                for summary in self._scoped_summaries(store, cwd=cwd, cwd_match=cwd_match, workers=workers)
                if not wanted_ids or summary.session_id in wanted_ids
            ]
            status = self._index.status(summaries)
            if stale_policy == "error" and status["refresh_sessions"]:
                raise ValueError(f"search index is stale: {status['refresh_sessions']} sessions need refresh")
            if stale_policy == "refresh":
                self._index.ensure(
                    store,
                    summaries,
                    self._extractor,
                    workers=workers,
                    max_refresh_sessions=max_refresh_sessions,
                    max_refresh_bytes=max_refresh_bytes,
                )
            response = self._index.search(
                summaries=summaries,
                matcher=matcher,
                query=query,
                keywords=keywords,
                regex=regex,
                mode=mode,
                roles=wanted_roles,
                message_types=wanted_types,
                include_contextual=include_contextual,
                include_compactions=include_compactions,
                exclude_keywords=exclude_keywords,
                after=after,
                before=before,
                max_results=max_results - len(all_results),
                context_chars=context_chars,
                max_per_session=max_per_session,
            )
            all_results.extend(response.results)
            total_matches += response.total_matches
            total_deduped += response.deduplicated
            total_sessions += response.sessions_searched
            total_messages += response.messages_searched
            truncated = truncated or response.truncated
            if len(all_results) >= max_results:
                break

        all_results.sort(key=lambda r: r.relevance_score, reverse=True)
        cross_deduped = 0
        seen_snippets: set[str] = set()
        deduped_results: list[ChatSearchResult] = []
        for result in all_results:
            snippet_key = result.snippet[:500]
            if snippet_key in seen_snippets:
                cross_deduped += 1
                continue
            seen_snippets.add(snippet_key)
            deduped_results.append(result)
        for rank, result in enumerate(deduped_results[:max_results], 1):
            all_results[rank - 1] = ChatSearchResult(
                provider=result.provider,
                session_id=result.session_id,
                cwd=result.cwd,
                timestamp=result.timestamp,
                path=result.path,
                message_index=result.message_index,
                role=result.role,
                message_type=result.message_type,
                snippet=result.snippet,
                relevance_score=result.relevance_score,
                rank=rank,
                duplicate_count=result.duplicate_count,
                first_seen=result.first_seen,
                match_positions=result.match_positions,
            )

        return SearchResponse(
            results=deduped_results[:max_results],
            total_matches=total_matches,
            deduplicated=total_deduped + cross_deduped,
            sessions_searched=total_sessions,
            messages_searched=total_messages,
            truncated=truncated,
        ).as_dict()

    def search_sessions(
        self,
        *,
        query: str | None = None,
        keywords: list[str] | None = None,
        regex: str | None = None,
        exclude_keywords: list[str] | None = None,
        provider: Provider = "all",
        cwd: str | None = None,
        cwd_match: CwdMatch = "exact",
        session_ids: list[str] | None = None,
        roles: list[str] | None = None,
        message_types: list[str] | None = None,
        mode: MatchMode = "literal",
        case_sensitive: bool = False,
        include_contextual: bool = False,
        include_compactions: bool = False,
        after: str | None = None,
        before: str | None = None,
        workers: int = 1,
        stale_policy: StalePolicy = "skip",
        max_refresh_sessions: int | None = None,
        max_refresh_bytes: int | None = None,
    ) -> list[dict[str, object]]:
        matcher = self._matcher(query=query, keywords=keywords, regex=regex, mode=mode, case_sensitive=case_sensitive)
        wanted_ids = set(session_ids or [])
        wanted_roles = set(roles or [])
        wanted_types = set(message_types or [])
        if wanted_roles and not wanted_types:
            include_compactions = False

        all_results: list[SessionSearchResult] = []
        for name, store in self._selected_stores(provider):
            summaries = [
                summary
                for summary in self._scoped_summaries(store, cwd=cwd, cwd_match=cwd_match, workers=workers)
                if not wanted_ids or summary.session_id in wanted_ids
            ]
            status = self._index.status(summaries)
            if stale_policy == "error" and status["refresh_sessions"]:
                raise ValueError(f"search index is stale: {status['refresh_sessions']} sessions need refresh")
            if stale_policy == "refresh":
                self._index.ensure(
                    store,
                    summaries,
                    self._extractor,
                    workers=workers,
                    max_refresh_sessions=max_refresh_sessions,
                    max_refresh_bytes=max_refresh_bytes,
                )
            results = self._index.search_sessions(
                summaries=summaries,
                matcher=matcher,
                query=query,
                keywords=keywords,
                regex=regex,
                mode=mode,
                roles=wanted_roles,
                message_types=wanted_types,
                include_contextual=include_contextual,
                include_compactions=include_compactions,
                exclude_keywords=exclude_keywords,
                after=after,
                before=before,
            )
            all_results.extend(results)

        all_results.sort(key=lambda r: r.match_count, reverse=True)
        return [result.as_dict() for result in all_results]

    def close(self) -> None:
        self._index.close()

    def _scoped_summaries(self, store: SessionStore, *, cwd: str | None, cwd_match: CwdMatch, workers: int) -> list[SessionSummary]:
        return [
            summary
            for summary in store.list_metadata(workers=max(1, workers))
            if self._cwd_matches(summary.cwd, cwd, cwd_match)
        ]

    def _selected_stores(self, provider: Provider) -> list[tuple[str, SessionStore]]:
        if provider == "all":
            return list(self._stores.items())
        if provider not in self._stores:
            raise ValueError(f"Unknown provider: {provider!r}. Available: {', '.join(sorted(self._stores))}")
        return [(provider, self._stores[provider])]

    @staticmethod
    def _cwd_matches(value: str, expected: str | None, mode: CwdMatch) -> bool:
        if not expected:
            return True
        left = str(Path(value)).lower()
        right = str(Path(expected)).lower()
        if mode == "contains":
            return right in left
        if mode == "prefix":
            return left.startswith(right)
        return left == right

    @staticmethod
    def _summary_dict(summary: SessionSummary) -> dict[str, object]:
        return {
            "provider": summary.provider,
            "session_id": summary.session_id,
            "cwd": summary.cwd,
            "timestamp": summary.timestamp,
            "path": str(summary.path),
            "message_count": summary.message_count,
        }

    @staticmethod
    def _keyword_variants(value: str, case_sensitive: bool) -> list[str]:
        base = value if case_sensitive else value.lower()
        variants = [base, base.replace("\u2019", "'"), base.replace("\u2019", "'").replace("'", "")]
        result: list[str] = []
        seen: set[str] = set()
        for variant in variants:
            if variant and variant not in seen:
                seen.add(variant)
                result.append(variant)
        return result

    @staticmethod
    def _matcher(
        *,
        query: str | None,
        keywords: list[str] | None,
        regex: str | None,
        mode: MatchMode,
        case_sensitive: bool,
    ) -> Matcher:
        flags = 0 if case_sensitive else re.IGNORECASE
        if regex or mode == "regex":
            pattern_text = regex or query or ""
            if _re2 is not None:
                try:
                    pattern = _re2.compile(pattern_text if case_sensitive else f"(?i){pattern_text}")

                    def match_regex(text: str) -> tuple[int, int] | None:
                        found = pattern.search(text)
                        return found.span() if found else None

                    return match_regex
                except _RE2_ERRORS:
                    pass
            pattern = re.compile(pattern_text, flags)

            def match_regex(text: str) -> tuple[int, int] | None:
                found = pattern.search(text)
                return found.span() if found else None

            return match_regex

        needles = keywords or ([query] if query else [])
        if not needles:
            raise ValueError("Provide query, regex, or keywords")
        prepared = [SessionSearchEngine._keyword_variants(needle, case_sensitive) for needle in needles]

        def match_keywords(text: str) -> tuple[int, int] | None:
            haystacks = SessionSearchEngine._keyword_variants(text, case_sensitive)
            spans = []
            for variants in prepared:
                if not variants:
                    continue
                found = None
                for needle in variants:
                    for haystack in haystacks:
                        idx = haystack.find(needle)
                        if idx >= 0:
                            found = (idx, len(needle))
                            break
                    if found is not None:
                        break
                if found is not None:
                    spans.append(found)
            if mode == "all_keywords" and len(spans) != len(prepared):
                return None
            if mode == "any_keywords" and not spans:
                return None
            if mode == "literal" and not spans:
                return None
            start, length = min(spans, default=(-1, 0))
            return (start, start + length) if start >= 0 else None

        return match_keywords
