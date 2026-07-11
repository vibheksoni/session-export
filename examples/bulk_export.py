"""Bulk export all sessions from one provider to multiple targets.

Usage:
    python examples/bulk_export.py <source> <target> [<target> ...] [--write] [--on-conflict MODE] [--workers N]
    python examples/bulk_export.py codex pi
    python examples/bulk_export.py codex pi opencode --write --workers 8
"""

from __future__ import annotations

import argparse
import sys
from uuid import uuid4

from session_sdk.converters import (
    CodexToClaudeConverter,
    CodexToOpenCodeConverter,
    CodexToPiConverter,
)
from session_sdk.models import SessionSummary
from session_sdk.paths import SessionIdFactory, WindowsDefaults
from session_sdk.stores import ClaudeStore, CodexStore, OpenCodeStore, PiDcpStore, PiStore

VALID_SOURCES = ("codex",)
VALID_TARGETS = ("pi", "opencode", "claude")


def main() -> int:
    parser = argparse.ArgumentParser(description="Bulk export all sessions from a source provider to targets.")
    parser.add_argument("source", choices=VALID_SOURCES, help="Source provider")
    parser.add_argument("targets", nargs="+", choices=VALID_TARGETS, help="Target provider(s)")
    parser.add_argument("--write", action="store_true", help="Write converted sessions to disk")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing destinations")
    parser.add_argument("--new-id", action="store_true", help="Generate new target session IDs")
    parser.add_argument("--workers", type=int, default=8, help="Number of parallel workers (default: 8)")
    parser.add_argument(
        "--on-conflict",
        choices=("skip", "overwrite", "fork", "update"),
        default="skip",
        help="Conflict resolution mode",
    )
    args = parser.parse_args()

    defaults = WindowsDefaults()
    codex = CodexStore(defaults.codex_home)
    pi = PiStore(defaults.pi_agent_home)
    dcp = PiDcpStore(defaults.pi_dcp_home)
    opencode = OpenCodeStore(defaults.opencode_data_home)
    claude = ClaudeStore(defaults.claude_home)
    id_factory = SessionIdFactory(preserve_ids=not args.new_id)

    # Build converters for each target
    converters = {}
    if "pi" in args.targets:
        converters["pi"] = CodexToPiConverter(codex, pi, dcp, id_factory)
    if "opencode" in args.targets:
        converters["opencode"] = CodexToOpenCodeConverter(codex, opencode, id_factory)
    if "claude" in args.targets:
        converters["claude"] = CodexToClaudeConverter(codex, claude, id_factory)

    summaries = codex.list(workers=1)
    total = len(summaries)
    print(f"Found {total} {args.source} sessions. Targets: {', '.join(args.targets)}")

    if not args.write:
        print("Dry run -- use --write to export.")
        return 0

    exported = 0
    skipped = 0
    failed = 0

    for i, summary in enumerate(summaries, start=1):
        sid = summary.session_id
        results = []
        try:
            for target_name, converter in converters.items():
                # Check for changes in update mode
                if args.on_conflict == "update":
                    try:
                        if not converter.has_changes(sid):
                            results.append(f"{target_name}:skipped")
                            continue
                    except Exception:
                        pass

                plan = converter.plan(sid)

                # Handle conflicts
                if plan.destination.exists():
                    if args.on_conflict == "skip" and not args.overwrite:
                        results.append(f"{target_name}:skipped")
                        continue
                    if args.on_conflict == "fork":
                        new_id = str(uuid4())
                        plan = converter.plan(sid, target_id=new_id)

                converter.write(plan, overwrite=True)
                results.append(f"{target_name}:ok")
        except Exception as exc:
            failed += 1
            print(f"[{i}/{total}] {sid} ... FAILED: {exc}", file=sys.stderr)
            continue

        if "skipped" in ",".join(results) and "ok" not in ",".join(results):
            skipped += 1
            print(f"[{i}/{total}] {sid} ... SKIPPED ({','.join(results)})")
        else:
            exported += 1
            print(f"[{i}/{total}] {sid} ... OK ({','.join(results)})")

    print(f"\nExported: {exported}, skipped: {skipped}, failed: {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
