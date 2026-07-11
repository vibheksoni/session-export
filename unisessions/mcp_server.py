from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Literal

from fastmcp import FastMCP

from session_sdk.paths import WindowsDefaults
from session_sdk.search import CwdMatch, MatchMode, Provider, SessionSearchEngine, StalePolicy
from session_sdk.stores import ClaudeStore, CodexStore, DevinStore, FactoryStore, OpenCodeStore, PiStore

Transport = Literal["stdio", "http", "sse", "streamable-http"]

mcp = FastMCP("UniSessions")


def _index_path() -> Path:
    configured = os.environ.get("UNISESSIONS_SEARCH_INDEX")
    if configured:
        return Path(configured)
    root = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME")
    base = Path(root) if root else Path.home() / ".cache"
    return base / "unisessions" / "search.sqlite"


def _engine() -> SessionSearchEngine:
    defaults = WindowsDefaults()
    return SessionSearchEngine(
        CodexStore(defaults.codex_home),
        PiStore(defaults.pi_agent_home),
        OpenCodeStore(defaults.opencode_data_home),
        _index_path(),
        ClaudeStore(defaults.claude_home),
        DevinStore(defaults.devin_home),
        FactoryStore(defaults.factory_home),
    )


@mcp.tool
def list_chats(
    cwd: str | None = None,
    provider: Provider = "all",
    cwd_match: CwdMatch = "exact",
    limit: int = 100,
    workers: int = 1,
) -> list[dict[str, object]]:
    """List known chat sessions, optionally filtered to a project path."""
    engine = _engine()
    try:
        return engine.list_chats(
            provider=provider,
            cwd=cwd,
            cwd_match=cwd_match,
            limit=limit,
            workers=workers,
        )
    finally:
        engine.close()


@mcp.tool
def index_status(
    cwd: str | None = None,
    provider: Provider = "all",
    cwd_match: CwdMatch = "exact",
    workers: int = 1,
) -> dict[str, dict[str, int] | int]:
    """Report indexed, missing, stale, and deleted chat index state without parsing sessions."""
    engine = _engine()
    try:
        return engine.index_status(provider=provider, cwd=cwd, cwd_match=cwd_match, workers=workers)
    finally:
        engine.close()


@mcp.tool
def refresh_chats_index(
    cwd: str | None = None,
    provider: Provider = "all",
    cwd_match: CwdMatch = "exact",
    workers: int = 1,
    max_refresh_sessions: int | None = None,
    max_refresh_bytes: int | None = None,
) -> dict[str, int]:
    """Parse sessions into the local FTS index so later recall is fast."""
    engine = _engine()
    try:
        return engine.refresh_index(
            provider=provider,
            cwd=cwd,
            cwd_match=cwd_match,
            workers=workers,
            max_refresh_sessions=max_refresh_sessions,
            max_refresh_bytes=max_refresh_bytes,
        )
    finally:
        engine.close()


@mcp.tool
def search_chats(
    query: str | None = None,
    keywords: list[str] | None = None,
    regex: str | None = None,
    exclude_keywords: list[str] | None = None,
    provider: Provider = "all",
    cwd: str | None = None,
    cwd_match: CwdMatch = "exact",
    session_ids: list[str] | None = None,
    roles: list[Literal["user", "assistant"]] | None = None,
    message_types: list[Literal["message", "compaction", "contextual"]] | None = None,
    mode: MatchMode = "literal",
    case_sensitive: bool = False,
    include_contextual: bool = False,
    include_compactions: bool = False,
    max_results: int = 50,
    context_chars: int = 300,
    max_per_session: int = 5,
    after: str | None = None,
    before: str | None = None,
    workers: int = 1,
    stale_policy: StalePolicy = "skip",
    max_refresh_sessions: int | None = None,
    max_refresh_bytes: int | None = None,
) -> dict[str, object]:
    """Search chat text across one, many, or all sessions.

    Returns a structured response with search_metadata (total_matches, deduplicated,
    sessions_searched, messages_searched, truncated) and a results array ranked by
    relevance score. Duplicate messages across compaction cycles are collapsed to
    a single hit with a duplicate_count field.

    Recommended workflow for first use:
    1. Call index_status to check if sessions are indexed.
    2. Call refresh_chats_index scoped to your provider if sessions are stale.
    3. Call search_chats with stale_policy='skip' (default) for fast cached recall.

    If the index is not built and stale_policy='skip', results will be empty.
    Use stale_policy='refresh' to parse missing sessions before searching (slow
    on first call with large corpora). Use stale_policy='error' to fail fast.

    Parameters:
    - query: literal search string (default mode).
    - keywords: list of keywords; use mode='all_keywords' for AND, 'any_keywords' for OR.
    - regex: Python regex pattern; takes priority over query/keywords.
      Example: regex='(?i)(?:i (?:don\\'t|do not|dont) like|i hate)'
    - exclude_keywords: list of keywords to exclude; messages containing any are skipped.
      Example: exclude_keywords=['update', 'readme', 'laravel'] removes false positives.
    - roles: filter to 'user' or 'assistant' messages. When roles=['user'] only,
      compaction summaries are auto-excluded since they are system-generated.
    - message_types: filter to 'message', 'compaction', or 'contextual'.
      Defaults to real messages only (compaction excluded by default).
    - max_per_session: cap results per session ID (default 5). Use 0 for unlimited.
    - after/before: ISO timestamp filters (e.g. after='2026-07-01').
    - context_chars: snippet context window (default 300).
    - include_compactions: set True to include compaction summaries in results.
    """
    engine = _engine()
    try:
        return engine.search(
            query=query,
            keywords=keywords,
            regex=regex,
            exclude_keywords=exclude_keywords,
            provider=provider,
            cwd=cwd,
            cwd_match=cwd_match,
            session_ids=session_ids,
            roles=list(roles) if roles else None,
            message_types=list(message_types) if message_types else None,
            mode=mode,
            case_sensitive=case_sensitive,
            include_contextual=include_contextual,
            include_compactions=include_compactions,
            max_results=max_results,
            context_chars=context_chars,
            max_per_session=max_per_session,
            after=after,
            before=before,
            workers=workers,
            stale_policy=stale_policy,
            max_refresh_sessions=max_refresh_sessions,
            max_refresh_bytes=max_refresh_bytes,
        )
    finally:
        engine.close()


@mcp.tool
def search_sessions(
    query: str | None = None,
    keywords: list[str] | None = None,
    regex: str | None = None,
    exclude_keywords: list[str] | None = None,
    provider: Provider = "all",
    cwd: str | None = None,
    cwd_match: CwdMatch = "exact",
    session_ids: list[str] | None = None,
    roles: list[Literal["user", "assistant"]] | None = None,
    message_types: list[Literal["message", "compaction", "contextual"]] | None = None,
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
    """Find which sessions are relevant to a topic, not individual messages.

    Returns a list of sessions sorted by match_count (most matches first),
    each with top_snippets showing the first few matching text excerpts.
    Useful for discovering which sessions to dive into with search_chats.

    Same parameters as search_chats except no max_results/context_chars/max_per_session
    since this operates at session granularity.
    """
    engine = _engine()
    try:
        return engine.search_sessions(
            query=query,
            keywords=keywords,
            regex=regex,
            exclude_keywords=exclude_keywords,
            provider=provider,
            cwd=cwd,
            cwd_match=cwd_match,
            session_ids=session_ids,
            roles=list(roles) if roles else None,
            message_types=list(message_types) if message_types else None,
            mode=mode,
            case_sensitive=case_sensitive,
            include_contextual=include_contextual,
            include_compactions=include_compactions,
            after=after,
            before=before,
            workers=workers,
            stale_policy=stale_policy,
            max_refresh_sessions=max_refresh_sessions,
            max_refresh_bytes=max_refresh_bytes,
        )
    finally:
        engine.close()


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the UniSessions MCP server.")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http", "sse", "streamable-http"],
        default=os.environ.get("UNISESSIONS_MCP_TRANSPORT", "stdio"),
        help="MCP transport to use. Default: stdio.",
    )
    parser.add_argument("--host", default=os.environ.get("UNISESSIONS_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("UNISESSIONS_MCP_PORT", "8765")))
    parser.add_argument("--path", default=os.environ.get("UNISESSIONS_MCP_PATH"))
    parser.add_argument("--log-level", default=os.environ.get("UNISESSIONS_MCP_LOG_LEVEL", "ERROR"))
    parser.add_argument(
        "--show-banner",
        action="store_true",
        default=_env_bool("UNISESSIONS_MCP_SHOW_BANNER", False),
        help="Show FastMCP startup banner. Keep off for stdio clients.",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    kwargs: dict[str, object] = {
        "transport": args.transport,
        "show_banner": args.show_banner,
        "log_level": args.log_level,
    }
    if args.transport != "stdio":
        kwargs.update({"host": args.host, "port": args.port})
        if args.path:
            kwargs["path"] = args.path
    mcp.run(**kwargs)


if __name__ == "__main__":
    main()
