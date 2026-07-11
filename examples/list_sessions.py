"""List sessions from all providers using the UniSessions SDK."""

from __future__ import annotations

import sys

from session_sdk import WindowsDefaults
from session_sdk.stores import ClaudeStore, CodexStore, OpenCodeStore, PiStore


def main() -> int:
    defaults = WindowsDefaults()
    stores: list[tuple[str, CodexStore | PiStore | OpenCodeStore | ClaudeStore]] = [
        ("codex", CodexStore(defaults.codex_home)),
        ("pi", PiStore(defaults.pi_agent_home)),
        ("opencode", OpenCodeStore(defaults.opencode_data_home)),
        ("claude", ClaudeStore(defaults.claude_home)),
    ]

    for name, store in stores:
        summaries = store.list(workers=1)
        print(f"\n=== {name.upper()} ({len(summaries)} sessions) ===")
        if not summaries:
            print("  (no sessions found)")
            continue
        for s in summaries:
            count = "unknown" if s.message_count < 0 else str(s.message_count)
            print(f"  {s.session_id}")
            print(f"    cwd:       {s.cwd}")
            print(f"    timestamp: {s.timestamp}")
            print(f"    messages:  {count}")
            print(f"    path:      {s.path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
