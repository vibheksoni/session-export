"""Use custom session directories instead of system defaults.

Usage:
    python examples/custom_paths.py [--codex-dir DIR] [--pi-dir DIR] [--opencode-dir DIR] [--claude-dir DIR]
    python examples/custom_paths.py
    python examples/custom_paths.py --codex-dir C:\\path\\to\\codex\\sessions
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from session_sdk.stores import ClaudeStore, CodexStore, OpenCodeStore, PiStore


def main() -> int:
    parser = argparse.ArgumentParser(description="List sessions from custom directories.")
    parser.add_argument("--codex-dir", default=None, help="Root containing Codex rollout JSONL files")
    parser.add_argument("--pi-dir", default=None, help="Root containing Pi session JSONL files")
    parser.add_argument("--opencode-dir", default=None, help="Root containing OpenCode export JSON files")
    parser.add_argument("--claude-dir", default=None, help="Root containing Claude session JSONL files")
    args = parser.parse_args()

    stores: list[tuple[str, CodexStore | PiStore | OpenCodeStore | ClaudeStore]] = []

    if args.codex_dir:
        stores.append(("codex (custom)", CodexStore(Path(args.codex_dir), Path(args.codex_dir))))
    if args.pi_dir:
        stores.append(("pi (custom)", PiStore(Path(args.pi_dir), Path(args.pi_dir))))
    if args.opencode_dir:
        stores.append(("opencode (custom)", OpenCodeStore(Path(args.opencode_dir), Path(args.opencode_dir))))
    if args.claude_dir:
        stores.append(("claude (custom)", ClaudeStore(Path(args.claude_dir), Path(args.claude_dir))))

    if not stores:
        print("No custom directories specified. Use --help for usage.")
        print()
        print("Examples:")
        print("  python examples/custom_paths.py --codex-dir C:\\path\\to\\codex\\sessions")
        print("  python examples/custom_paths.py --pi-dir C:\\path\\to\\pi\\sessions --claude-dir C:\\path\\to\\claude")
        return 0

    for name, store in stores:
        summaries = store.list(workers=1)
        print(f"\n=== {name} ({len(summaries)} sessions) ===")
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
