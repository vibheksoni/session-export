from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from uuid import uuid4

from session_sdk.converters import (
    ClaudeToCodexConverter,
    ClaudeToDevinConverter,
    ClaudeToFactoryConverter,
    ClaudeToOpenCodeConverter,
    ClaudeToPiConverter,
    ClaudeToWindsurfConverter,
    CodexToClaudeConverter,
    CodexToDevinConverter,
    CodexToFactoryConverter,
    CodexToOpenCodeConverter,
    CodexToPiConverter,
    CodexToWindsurfConverter,
    DevinToClaudeConverter,
    DevinToCodexConverter,
    DevinToFactoryConverter,
    DevinToOpenCodeConverter,
    DevinToPiConverter,
    DevinToWindsurfConverter,
    FactoryToClaudeConverter,
    FactoryToCodexConverter,
    FactoryToDevinConverter,
    FactoryToOpenCodeConverter,
    FactoryToPiConverter,
    FactoryToWindsurfConverter,
    OpenCodeToClaudeConverter,
    OpenCodeToCodexConverter,
    OpenCodeToDevinConverter,
    OpenCodeToFactoryConverter,
    OpenCodeToPiConverter,
    OpenCodeToWindsurfConverter,
    PiToClaudeConverter,
    PiToCodexConverter,
    PiToDevinConverter,
    PiToFactoryConverter,
    PiToOpenCodeConverter,
    PiToWindsurfConverter,
    WindsurfToClaudeConverter,
    WindsurfToCodexConverter,
    WindsurfToDevinConverter,
    WindsurfToFactoryConverter,
    WindsurfToOpenCodeConverter,
    WindsurfToPiConverter,
)
from session_sdk.models import ConversionPlan, SessionSummary
from session_sdk.paths import SessionIdFactory, WindowsDefaults
from session_sdk.stores import ClaudeStore, CodexStore, DevinStore, FactoryStore, OpenCodeStore, PiDcpStore, PiStore, WindsurfStore
from session_sdk.traces import TRACE_FORMATS, build_trace
from session_sdk.converters import MessageExtractor


class CliApp:
    def __init__(self, argv: list[str] | None = None) -> None:
        self._argv = argv

    def run(self) -> int:
        args = self._parser().parse_args(self._argv)
        defaults = WindowsDefaults()
        codex = CodexStore(
            Path(args.codex_home or defaults.codex_home),
            self._optional_path(args.codex_session_dir),
        )
        pi = PiStore(
            Path(args.pi_agent_home or defaults.pi_agent_home),
            self._optional_path(args.pi_session_dir),
        )
        dcp = PiDcpStore(Path(args.pi_dcp_home or defaults.pi_dcp_home))
        opencode = OpenCodeStore(
            Path(args.opencode_data_home or defaults.opencode_data_home),
            self._optional_path(args.opencode_session_dir),
        )
        claude = ClaudeStore(
            Path(args.claude_home or defaults.claude_home),
            self._optional_path(args.claude_session_dir),
        )
        devin = DevinStore(
            Path(args.devin_home or defaults.devin_home),
            self._optional_path(args.devin_session_dir),
        )
        factory = FactoryStore(
            Path(args.factory_home or defaults.factory_home),
            self._optional_path(args.factory_session_dir),
        )
        windsurf = WindsurfStore(
            Path(args.windsurf_home or defaults.windsurf_home),
            self._optional_path(args.windsurf_session_dir),
        )

        if args.command == "list":
            store = {"codex": codex, "pi": pi, "opencode": opencode, "claude": claude, "devin": devin, "factory": factory, "windsurf": windsurf}[args.provider]
            summaries = store.list(workers=args.workers or 1)
            self._print_summaries(summaries)
            return 0

        if args.command == "to-trace":
            return self._to_trace(args, codex, pi, opencode, claude, devin, factory, windsurf)

        id_factory = SessionIdFactory(preserve_ids=not args.new_id)
        if args.command in ("codex-to-pi", "pi-to-codex", "codex-to-opencode",
                            "pi-to-opencode", "opencode-to-codex", "opencode-to-pi",
                            "claude-to-pi", "pi-to-claude", "claude-to-codex",
                            "codex-to-claude", "claude-to-opencode", "opencode-to-claude",
                            "devin-to-pi", "devin-to-codex", "devin-to-opencode",
                            "devin-to-claude", "pi-to-devin", "codex-to-devin",
                            "opencode-to-devin", "claude-to-devin",
                            "factory-to-pi", "factory-to-codex", "factory-to-opencode",
                            "factory-to-claude", "factory-to-devin", "pi-to-factory",
                            "codex-to-factory", "opencode-to-factory", "claude-to-factory",
                            "devin-to-factory",
                            "windsurf-to-pi", "windsurf-to-codex", "windsurf-to-opencode",
                            "windsurf-to-claude", "windsurf-to-devin", "windsurf-to-factory",
                            "pi-to-windsurf", "codex-to-windsurf", "opencode-to-windsurf",
                            "claude-to-windsurf", "devin-to-windsurf", "factory-to-windsurf"):
            return self._single_convert(args, codex, pi, dcp, opencode, claude, devin, factory, windsurf, id_factory)

        if args.command == "codex-to-pi-all":
            return self._bulk_export(codex, pi, dcp, opencode, id_factory, args, targets=["pi"])

        if args.command == "export-all":
            return self._bulk_export(codex, pi, dcp, opencode, id_factory, args, targets=args.targets)

        raise ValueError(f"Unsupported command: {args.command}")

    @staticmethod
    def _parser() -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(prog="unisessions")
        parser.add_argument("--codex-home", default=None)
        parser.add_argument("--codex-session-dir", default=None)
        parser.add_argument("--pi-agent-home", default=None)
        parser.add_argument("--pi-session-dir", default=None)
        parser.add_argument("--pi-dcp-home", default=None)
        parser.add_argument("--opencode-data-home", default=None)
        parser.add_argument("--opencode-session-dir", default=None)
        parser.add_argument("--claude-home", default=None)
        parser.add_argument("--claude-session-dir", default=None)
        parser.add_argument("--devin-home", default=None)
        parser.add_argument("--devin-session-dir", default=None)
        parser.add_argument("--factory-home", default=None)
        parser.add_argument("--factory-session-dir", default=None)
        parser.add_argument("--windsurf-home", default=None)
        parser.add_argument("--windsurf-session-dir", default=None)
        subparsers = parser.add_subparsers(dest="command", required=True)

        list_parser = subparsers.add_parser("list")
        list_parser.add_argument("provider", choices=("codex", "pi", "opencode", "claude", "devin", "factory", "windsurf"))
        list_parser.add_argument("--workers", type=int, default=None, help="Number of parallel workers for listing (default: 1).")

        codex_to_pi = subparsers.add_parser("codex-to-pi")
        CliApp._add_convert_args(codex_to_pi)

        pi_to_codex = subparsers.add_parser("pi-to-codex")
        CliApp._add_convert_args(pi_to_codex)

        codex_to_opencode = subparsers.add_parser("codex-to-opencode")
        CliApp._add_convert_args(codex_to_opencode)

        pi_to_opencode = subparsers.add_parser("pi-to-opencode")
        CliApp._add_convert_args(pi_to_opencode)

        opencode_to_codex = subparsers.add_parser("opencode-to-codex")
        CliApp._add_convert_args(opencode_to_codex)

        opencode_to_pi = subparsers.add_parser("opencode-to-pi")
        CliApp._add_convert_args(opencode_to_pi)

        claude_to_pi = subparsers.add_parser("claude-to-pi")
        CliApp._add_convert_args(claude_to_pi)

        pi_to_claude = subparsers.add_parser("pi-to-claude")
        CliApp._add_convert_args(pi_to_claude)

        claude_to_codex = subparsers.add_parser("claude-to-codex")
        CliApp._add_convert_args(claude_to_codex)

        codex_to_claude = subparsers.add_parser("codex-to-claude")
        CliApp._add_convert_args(codex_to_claude)

        claude_to_opencode = subparsers.add_parser("claude-to-opencode")
        CliApp._add_convert_args(claude_to_opencode)

        opencode_to_claude = subparsers.add_parser("opencode-to-claude")
        CliApp._add_convert_args(opencode_to_claude)

        devin_to_pi = subparsers.add_parser("devin-to-pi")
        CliApp._add_convert_args(devin_to_pi)

        pi_to_devin = subparsers.add_parser("pi-to-devin")
        CliApp._add_convert_args(pi_to_devin)

        devin_to_codex = subparsers.add_parser("devin-to-codex")
        CliApp._add_convert_args(devin_to_codex)

        codex_to_devin = subparsers.add_parser("codex-to-devin")
        CliApp._add_convert_args(codex_to_devin)

        devin_to_opencode = subparsers.add_parser("devin-to-opencode")
        CliApp._add_convert_args(devin_to_opencode)

        opencode_to_devin = subparsers.add_parser("opencode-to-devin")
        CliApp._add_convert_args(opencode_to_devin)

        devin_to_claude = subparsers.add_parser("devin-to-claude")
        CliApp._add_convert_args(devin_to_claude)

        claude_to_devin = subparsers.add_parser("claude-to-devin")
        CliApp._add_convert_args(claude_to_devin)

        factory_to_pi = subparsers.add_parser("factory-to-pi")
        CliApp._add_convert_args(factory_to_pi)

        pi_to_factory = subparsers.add_parser("pi-to-factory")
        CliApp._add_convert_args(pi_to_factory)

        factory_to_codex = subparsers.add_parser("factory-to-codex")
        CliApp._add_convert_args(factory_to_codex)

        codex_to_factory = subparsers.add_parser("codex-to-factory")
        CliApp._add_convert_args(codex_to_factory)

        factory_to_opencode = subparsers.add_parser("factory-to-opencode")
        CliApp._add_convert_args(factory_to_opencode)

        opencode_to_factory = subparsers.add_parser("opencode-to-factory")
        CliApp._add_convert_args(opencode_to_factory)

        factory_to_claude = subparsers.add_parser("factory-to-claude")
        CliApp._add_convert_args(factory_to_claude)

        claude_to_factory = subparsers.add_parser("claude-to-factory")
        CliApp._add_convert_args(claude_to_factory)

        factory_to_devin = subparsers.add_parser("factory-to-devin")
        CliApp._add_convert_args(factory_to_devin)

        devin_to_factory = subparsers.add_parser("devin-to-factory")
        CliApp._add_convert_args(devin_to_factory)

        windsurf_to_pi = subparsers.add_parser("windsurf-to-pi")
        CliApp._add_convert_args(windsurf_to_pi)

        pi_to_windsurf = subparsers.add_parser("pi-to-windsurf")
        CliApp._add_convert_args(pi_to_windsurf)

        windsurf_to_codex = subparsers.add_parser("windsurf-to-codex")
        CliApp._add_convert_args(windsurf_to_codex)

        codex_to_windsurf = subparsers.add_parser("codex-to-windsurf")
        CliApp._add_convert_args(codex_to_windsurf)

        windsurf_to_opencode = subparsers.add_parser("windsurf-to-opencode")
        CliApp._add_convert_args(windsurf_to_opencode)

        opencode_to_windsurf = subparsers.add_parser("opencode-to-windsurf")
        CliApp._add_convert_args(opencode_to_windsurf)

        windsurf_to_claude = subparsers.add_parser("windsurf-to-claude")
        CliApp._add_convert_args(windsurf_to_claude)

        claude_to_windsurf = subparsers.add_parser("claude-to-windsurf")
        CliApp._add_convert_args(claude_to_windsurf)

        windsurf_to_devin = subparsers.add_parser("windsurf-to-devin")
        CliApp._add_convert_args(windsurf_to_devin)

        devin_to_windsurf = subparsers.add_parser("devin-to-windsurf")
        CliApp._add_convert_args(devin_to_windsurf)

        windsurf_to_factory = subparsers.add_parser("windsurf-to-factory")
        CliApp._add_convert_args(windsurf_to_factory)

        factory_to_windsurf = subparsers.add_parser("factory-to-windsurf")
        CliApp._add_convert_args(factory_to_windsurf)

        codex_to_pi_all = subparsers.add_parser("codex-to-pi-all")
        CliApp._add_bulk_args(codex_to_pi_all)

        export_all = subparsers.add_parser("export-all")
        CliApp._add_bulk_args(export_all)
        export_all.add_argument("--targets", nargs="+", default=["pi"], choices=("pi", "opencode", "claude", "devin", "factory", "windsurf"))

        to_trace = subparsers.add_parser("to-trace")
        to_trace.add_argument("provider", choices=("codex", "pi", "opencode", "claude", "devin", "factory", "windsurf"))
        to_trace.add_argument("session_id")
        to_trace.add_argument("--format", choices=TRACE_FORMATS, default="sts",
                              help="Trace format: sts (HuggingFace), openai (fine-tuning), or sharegpt.")
        to_trace.add_argument("--output", "-o", default=None,
                              help="Output file path. If omitted, prints to stdout.")
        to_trace.add_argument("--write", action="store_true", help="Write to output file instead of stdout.")
        return parser

    @staticmethod
    def _optional_path(value: str | None) -> Path | None:
        if value is None:
            return None
        return Path(value)

    @staticmethod
    def _add_bulk_args(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--write", action="store_true", help="Write converted sessions.")
        parser.add_argument("--overwrite", action="store_true", help="Allow replacing existing destinations.")
        parser.add_argument("--new-id", action="store_true", help="Generate new target session IDs.")
        parser.add_argument("--workers", type=int, default=None, help="Number of parallel workers (default: 8).")
        parser.add_argument("--on-conflict", choices=("skip", "overwrite", "fork", "update"), default="skip",
                            help="Conflict resolution: skip, overwrite, fork (new ID), or update (overwrite if changed).")

    def _bulk_export(
        self,
        codex: CodexStore,
        pi: PiStore,
        dcp: PiDcpStore,
        opencode: OpenCodeStore,
        id_factory: SessionIdFactory,
        args: argparse.Namespace,
        targets: list[str],
    ) -> int:
        workers = args.workers or 8
        summaries = codex.list(workers=1)
        total = len(summaries)
        print(f"Found {total} Codex sessions. Workers: {workers}. Targets: {', '.join(targets)}")
        if not args.write:
            print("Dry run -- use --write to export.")
            return 0

        pi_converter = CodexToPiConverter(codex, pi, dcp, id_factory)
        opencode_converter = CodexToOpenCodeConverter(codex, opencode, id_factory)

        exported = 0
        skipped = 0
        failed = 0

        def convert_one(summary: SessionSummary) -> tuple[str, str, str]:
            sid = summary.session_id
            try:
                results = []
                for target in targets:
                    if target == "pi":
                        converter = pi_converter
                        if args.on_conflict == "update":
                            try:
                                if not converter.has_changes(sid):
                                    results.append("pi:skipped")
                                    continue
                            except Exception:
                                pass
                        plan = converter.plan(sid)
                        if plan.destination.exists():
                            action = self._resolve_conflict(args, converter, sid)
                            if action == "skip":
                                results.append("pi:skipped")
                                continue
                            elif action == "fork":
                                new_id = str(uuid4())
                                plan = converter.plan(sid, target_id=new_id)
                        try:
                            converter.write(plan, overwrite=True)
                        except FileExistsError:
                            converter.write(plan, overwrite=True)
                        results.append("pi:ok")
                    elif target == "opencode":
                        converter = opencode_converter
                        if args.on_conflict == "update":
                            try:
                                if not converter.has_changes(sid):
                                    results.append("opencode:skipped")
                                    continue
                            except Exception:
                                pass
                        plan = converter.plan(sid)
                        if plan.destination.exists():
                            action = self._resolve_conflict(args, converter, sid)
                            if action == "skip":
                                results.append("opencode:skipped")
                                continue
                            elif action == "fork":
                                new_id = str(uuid4())
                                plan = converter.plan(sid, target_id=new_id)
                        try:
                            converter.write(plan, overwrite=True)
                        except FileExistsError:
                            converter.write(plan, overwrite=True)
                        results.append("opencode:ok")
                return sid, ",".join(results), ""
            except Exception as exc:
                return sid, "failed", str(exc)

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="export") as executor:
            future_to_summary = {executor.submit(convert_one, s): s for s in summaries}
            for i, future in enumerate(as_completed(future_to_summary), start=1):
                sid, status, detail = future.result()
                if "failed" in status:
                    failed += 1
                    print(f"[{i}/{total}] {sid} ... FAILED: {detail}", file=sys.stderr)
                elif "skipped" in status and "ok" not in status:
                    skipped += 1
                    print(f"[{i}/{total}] {sid} ... SKIPPED ({status})")
                else:
                    exported += 1
                    print(f"[{i}/{total}] {sid} ... OK ({status})")

        print(f"\nExported: {exported}, skipped: {skipped}, failed: {failed}")
        return 0 if failed == 0 else 1

    @staticmethod
    def _add_convert_args(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("session_id")
        parser.add_argument("--write", action="store_true", help="Write the converted session.")
        parser.add_argument("--overwrite", action="store_true", help="Allow replacing an existing destination.")
        parser.add_argument("--new-id", action="store_true", help="Generate a new target session ID.")
        parser.add_argument("--on-conflict", choices=("skip", "overwrite", "fork", "update"), default="skip",
                            help="Conflict resolution: skip, overwrite, fork (new ID), or update (overwrite if changed).")

    def _single_convert(
        self,
        args: argparse.Namespace,
        codex: CodexStore,
        pi: PiStore,
        dcp: PiDcpStore,
        opencode: OpenCodeStore,
        claude: ClaudeStore,
        devin: DevinStore,
        factory: FactoryStore,
        windsurf: WindsurfStore,
        id_factory: SessionIdFactory,
    ) -> int:
        converters = {
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
            "devin-to-pi": lambda: DevinToPiConverter(devin, pi, dcp, id_factory),
            "pi-to-devin": lambda: PiToDevinConverter(pi, devin, id_factory),
            "devin-to-codex": lambda: DevinToCodexConverter(devin, codex, id_factory),
            "codex-to-devin": lambda: CodexToDevinConverter(codex, devin, id_factory),
            "devin-to-opencode": lambda: DevinToOpenCodeConverter(devin, opencode, id_factory),
            "opencode-to-devin": lambda: OpenCodeToDevinConverter(opencode, devin, id_factory),
            "devin-to-claude": lambda: DevinToClaudeConverter(devin, claude, id_factory),
            "claude-to-devin": lambda: ClaudeToDevinConverter(claude, devin, id_factory),
            "factory-to-pi": lambda: FactoryToPiConverter(factory, pi, dcp, id_factory),
            "pi-to-factory": lambda: PiToFactoryConverter(pi, factory, id_factory),
            "factory-to-codex": lambda: FactoryToCodexConverter(factory, codex, id_factory),
            "codex-to-factory": lambda: CodexToFactoryConverter(codex, factory, id_factory),
            "factory-to-opencode": lambda: FactoryToOpenCodeConverter(factory, opencode, id_factory),
            "opencode-to-factory": lambda: OpenCodeToFactoryConverter(opencode, factory, id_factory),
            "factory-to-claude": lambda: FactoryToClaudeConverter(factory, claude, id_factory),
            "claude-to-factory": lambda: ClaudeToFactoryConverter(claude, factory, id_factory),
            "factory-to-devin": lambda: FactoryToDevinConverter(factory, devin, id_factory),
            "devin-to-factory": lambda: DevinToFactoryConverter(devin, factory, id_factory),
            "windsurf-to-pi": lambda: WindsurfToPiConverter(windsurf, pi, dcp, id_factory),
            "pi-to-windsurf": lambda: PiToWindsurfConverter(pi, windsurf, id_factory),
            "windsurf-to-codex": lambda: WindsurfToCodexConverter(windsurf, codex, id_factory),
            "codex-to-windsurf": lambda: CodexToWindsurfConverter(codex, windsurf, id_factory),
            "windsurf-to-opencode": lambda: WindsurfToOpenCodeConverter(windsurf, opencode, id_factory),
            "opencode-to-windsurf": lambda: OpenCodeToWindsurfConverter(opencode, windsurf, id_factory),
            "windsurf-to-claude": lambda: WindsurfToClaudeConverter(windsurf, claude, id_factory),
            "claude-to-windsurf": lambda: ClaudeToWindsurfConverter(claude, windsurf, id_factory),
            "windsurf-to-devin": lambda: WindsurfToDevinConverter(windsurf, devin, id_factory),
            "devin-to-windsurf": lambda: DevinToWindsurfConverter(devin, windsurf, id_factory),
            "windsurf-to-factory": lambda: WindsurfToFactoryConverter(windsurf, factory, id_factory),
            "factory-to-windsurf": lambda: FactoryToWindsurfConverter(factory, windsurf, id_factory),
        }
        converter = converters[args.command]()
        sid = args.session_id

        # For update mode, check changes before planning to avoid double parse.
        # has_changes only counts source records vs destination records -- fast.
        if args.write and args.on_conflict == "update":
            try:
                if not converter.has_changes(sid):
                    print(f"No changes detected, skipping.")
                    return 0
            except Exception:
                pass  # If check fails, proceed to plan

        target_id: str | None = None
        if args.write and args.on_conflict == "fork":
            # Check if destination exists before forking
            plan = converter.plan(sid)
            if plan.destination.exists():
                target_id = str(uuid4())
                plan = converter.plan(sid, target_id=target_id)
                print(f"Forked to new session ID: {target_id}")
            self._print_plan(plan)
            converter.write(plan, overwrite=True)
            return 0

        plan = converter.plan(sid, target_id=target_id)
        self._print_plan(plan)
        if args.write:
            if plan.destination.exists():
                action = self._resolve_conflict(args, converter, sid)
                if action == "skip":
                    print(f"Destination exists, skipping: {plan.destination}")
                    return 0
            converter.write(plan, overwrite=True)
        return 0

    def _to_trace(
        self,
        args: argparse.Namespace,
        codex: CodexStore,
        pi: PiStore,
        opencode: OpenCodeStore,
        claude: ClaudeStore,
        devin: DevinStore,
        factory: FactoryStore,
        windsurf: WindsurfStore,
    ) -> int:
        stores = {"codex": codex, "pi": pi, "opencode": opencode, "claude": claude, "devin": devin, "factory": factory, "windsurf": windsurf}
        store = stores[args.provider]
        session = store.load(args.session_id)
        extractor = MessageExtractor()
        extractors = {
            "codex": extractor.from_codex,
            "pi": extractor.from_pi,
            "opencode": extractor.from_opencode,
            "claude": extractor.from_claude,
            "devin": extractor.from_devin,
            "factory": extractor.from_factory,
            "windsurf": extractor.from_windsurf,
        }
        messages = extractors[args.provider](session)
        records = build_trace(args.format, session, messages)

        from session_sdk.jsonl import _dumps

        lines = [_dumps(r).decode("utf-8") for r in records]
        output = "\n".join(lines) + "\n"

        if args.write and args.output:
            Path(args.output).write_text(output, encoding="utf-8")
            print(f"Wrote {len(records)} records to {args.output}")
        else:
            sys.stdout.write(output)
        return 0

    def _resolve_conflict(self, args: argparse.Namespace, converter, session_id: str) -> str:
        """Returns 'skip', 'overwrite', or 'fork'."""
        if args.overwrite:
            return "overwrite"
        mode = args.on_conflict
        if mode == "overwrite":
            return "overwrite"
        if mode == "fork":
            return "fork"
        if mode == "update":
            try:
                if converter.has_changes(session_id):
                    return "overwrite"
                return "skip"
            except Exception:
                return "skip"
        return "skip"

    @staticmethod
    def _print_summaries(summaries: list[SessionSummary]) -> None:
        for summary in summaries:
            message_count = "unknown" if summary.message_count < 0 else str(summary.message_count)
            print(
                f"{summary.provider}\t{summary.session_id}\t{summary.timestamp}\t"
                f"{message_count} messages\t{summary.cwd}\t{summary.path}"
            )

    @staticmethod
    def _print_plan(plan: ConversionPlan) -> None:
        print(f"source:      {plan.source.path}")
        print(f"source id:   {plan.source.session_id}")
        print(f"source cwd:  {plan.source.cwd}")
        print(f"destination: {plan.destination}")
        print(f"records:     {len(plan.records)}")
        if plan.services:
            for service in plan.services:
                print(f"service:     {service}")


def main(argv: list[str] | None = None) -> int:
    return CliApp(argv).run()
