"""Fork a session: convert with a new session ID while preserving the original.

Usage:
    python examples/fork_session.py <source> <target> <session_id> [--write]
    python examples/fork_session.py codex pi <session-id>
    python examples/fork_session.py codex pi <session-id> --write
"""

from __future__ import annotations

import argparse
import sys
from uuid import uuid4

from session_sdk.converters import (
    ClaudeToCodexConverter,
    ClaudeToOpenCodeConverter,
    ClaudeToPiConverter,
    CodexToClaudeConverter,
    CodexToOpenCodeConverter,
    CodexToPiConverter,
    OpenCodeToClaudeConverter,
    OpenCodeToCodexConverter,
    OpenCodeToPiConverter,
    PiToClaudeConverter,
    PiToCodexConverter,
    PiToOpenCodeConverter,
)
from session_sdk.paths import SessionIdFactory, WindowsDefaults
from session_sdk.stores import ClaudeStore, CodexStore, OpenCodeStore, PiDcpStore, PiStore

VALID_PROVIDERS = ("codex", "pi", "opencode", "claude")


def build_converter(
    source: str,
    target: str,
    codex: CodexStore,
    pi: PiStore,
    dcp: PiDcpStore,
    opencode: OpenCodeStore,
    claude: ClaudeStore,
    id_factory: SessionIdFactory,
):
    key = f"{source}-to-{target}"
    table = {
        "codex-to-pi": lambda: CodexToPiConverter(codex, pi, dcp, id_factory),
        "pi-to-codex": lambda: PiToCodexConverter(pi, codex, id_factory),
        "codex-to-opencode": lambda: CodexToOpenCodeConverter(codex, opencode, id_factory),
        "pi-to-opencode": lambda: PiToOpenCodeConverter(pi, opencode, id_factory),
        "opencode-to-codex": lambda: OpenCodeToCodexConverter(opencode, codex, id_factory),
        "opencode-to-pi": lambda: OpenCodeToPiConverter(opencode, pi, dcp, id_factory),
        "claude-to-pi": lambda: ClaudeToPiConverter(claude, pi, dcp, id_factory),
        "pi-to-claude": lambda: PiToClaudeConverter(pi, claude, id_factory),
        "claude-to-codex": lambda: ClaudeToCodexConverter(claude, codex, id_factory),
        "codex-to-claude": lambda: CodexToClaudeConverter(codex, claude, id_factory),
        "claude-to-opencode": lambda: ClaudeToOpenCodeConverter(claude, opencode, id_factory),
        "opencode-to-claude": lambda: OpenCodeToClaudeConverter(opencode, claude, id_factory),
    }
    if key not in table:
        print(f"Unsupported conversion: {source} -> {target}", file=sys.stderr)
        print(f"Valid pairs: {', '.join(table.keys())}", file=sys.stderr)
        sys.exit(2)
    return table[key]()


def main() -> int:
    parser = argparse.ArgumentParser(description="Fork a session with a new ID while preserving the original.")
    parser.add_argument("source", choices=VALID_PROVIDERS, help="Source provider")
    parser.add_argument("target", choices=VALID_PROVIDERS, help="Target provider")
    parser.add_argument("session_id", help="Session ID to fork")
    parser.add_argument("--write", action="store_true", help="Write the forked session to disk")
    args = parser.parse_args()

    defaults = WindowsDefaults()
    codex = CodexStore(defaults.codex_home)
    pi = PiStore(defaults.pi_agent_home)
    dcp = PiDcpStore(defaults.pi_dcp_home)
    opencode = OpenCodeStore(defaults.opencode_data_home)
    claude = ClaudeStore(defaults.claude_home)
    id_factory = SessionIdFactory(preserve_ids=True)

    converter = build_converter(
        args.source, args.target, codex, pi, dcp, opencode, claude, id_factory
    )

    # Generate a new UUID for the forked session
    new_id = str(uuid4())
    print(f"Original session ID: {args.session_id}")
    print(f"Forked session ID:   {new_id}")

    # Plan with target_id to fork into a new session
    plan = converter.plan(args.session_id, target_id=new_id)
    print(f"\nsource:      {plan.source.path}")
    print(f"source id:   {plan.source.session_id}")
    print(f"source cwd:  {plan.source.cwd}")
    print(f"destination: {plan.destination}")
    print(f"records:     {len(plan.records)}")
    for service in plan.services:
        print(f"service:     {service}")

    if args.write:
        converter.write(plan, overwrite=True)
        print(f"\nForked session written to: {plan.destination}")
    else:
        print("\nDry run -- use --write to write the forked session.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
