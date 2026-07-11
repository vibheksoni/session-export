"""Search across sessions using SessionSearchEngine.

Usage:
    python examples/search_sessions.py [query] [--provider PROVIDER] [--role ROLE] [--cwd PATH]
    python examples/search_sessions.py "error handling"
    python examples/search_sessions.py "function" --provider codex --role user
    python examples/search_sessions.py --provider pi --cwd C:\\Projects\\myproject
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from session_sdk.paths import WindowsDefaults
from session_sdk.search import SessionSearchEngine
from session_sdk.stores import ClaudeStore, CodexStore, OpenCodeStore, PiStore


def default_index_path() -> Path:
    configured = os.environ.get("UNISESSIONS_SEARCH_INDEX")
    if configured:
        return Path(configured)
    root = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME")
    base = Path(root) if root else Path.home() / ".cache"
    return base / "unisessions" / "search.sqlite"


def main() -> int:
    parser = argparse.ArgumentParser(description="Search across sessions using the SDK search engine.")
    parser.add_argument("query", nargs="?", default=None, help="Search query text")
    parser.add_argument("--provider", default="all", choices=("all", "codex", "pi", "opencode", "claude"))
    parser.add_argument("--role", default=None, choices=("user", "assistant"))
    parser.add_argument("--cwd", default=None, help="Filter by project cwd")
    parser.add_argument("--max-results", type=int, default=20, help="Maximum results (default: 20)")
    parser.add_argument("--max-per-session", type=int, default=5, help="Max results per session (default: 5)")
    parser.add_argument("--exclude", nargs="*", default=None, help="Keywords to exclude")
    parser.add_argument("--after", default=None, help="ISO timestamp lower bound")
    parser.add_argument("--before", default=None, help="ISO timestamp upper bound")
    parser.add_argument("--stale-policy", default="skip", choices=("refresh", "skip", "error"))
    parser.add_argument("--max-refresh", type=int, default=10, help="Max sessions to refresh (default: 10)")
    args = parser.parse_args()

    defaults = WindowsDefaults()
    engine = SessionSearchEngine(
        CodexStore(defaults.codex_home),
        PiStore(defaults.pi_agent_home),
        OpenCodeStore(defaults.opencode_data_home),
        default_index_path(),
        ClaudeStore(defaults.claude_home),
    )

    try:
        print("=== Index Status ===")
        status = engine.index_status(provider=args.provider)
        for name, stats in status.items():
            if isinstance(stats, dict):
                print(f"  {name}: indexed={stats['indexed_sessions']}, "
                      f"missing={stats['missing_sessions']}, "
                      f"stale={stats['stale_sessions']}, "
                      f"refresh={stats['refresh_sessions']}")
            else:
                print(f"  {name}: {stats}")

        if args.stale_policy != "skip":
            print("\n=== Refreshing Index ===")
            refreshed = engine.refresh_index(
                provider=args.provider, workers=1, max_refresh_sessions=args.max_refresh,
            )
            for name, count in refreshed.items():
                print(f"  {name}: {count} sessions refreshed")
        else:
            print("\n(skipping refresh -- stale_policy=skip)")

        if args.query is None:
            print("\nNo query provided. Use --help for usage.")
            print("Example: python examples/search_sessions.py \"error handling\"")
            return 0

        print(f"\n=== Search: '{args.query}' ===")
        response = engine.search(
            query=args.query,
            provider=args.provider,
            cwd=args.cwd,
            roles=[args.role] if args.role else None,
            exclude_keywords=args.exclude,
            max_results=args.max_results,
            max_per_session=args.max_per_session,
            after=args.after,
            before=args.before,
            stale_policy=args.stale_policy,
            max_refresh_sessions=args.max_refresh,
        )

        meta = response["search_metadata"]
        print(f"\n  total_matches={meta['total_matches']}, returned={meta['returned']}, "
              f"deduplicated={meta['deduplicated']}, truncated={meta['truncated']}")

        results = response["results"]
        if not results:
            print("  No results found.")
            return 0

        for r in results:
            print(f"\n  #{r['rank']} [{r['provider']}] {r['session_id'][:12]}... "
                  f"score={r['relevance_score']} dups={r['duplicate_count']}")
            print(f"    role:    {r['role']}")
            print(f"    type:    {r['message_type']}")
            print(f"    snippet: {r['snippet']}")

        print(f"\n=== Session-Level Search ===")
        sessions = engine.search_sessions(
            query=args.query,
            provider=args.provider,
            cwd=args.cwd,
            roles=[args.role] if args.role else None,
            stale_policy=args.stale_policy,
        )
        for s in sessions:
            print(f"  [{s['provider']}] {s['session_id'][:12]}... matches={s['match_count']}")
            for snip in s["top_snippets"][:2]:
                print(f"    > {snip[:80]}")
    finally:
        engine.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
