from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from uuid import uuid4

from session_sdk.converters import (
    CommandCodeToPiConverter,
    CommandCodeToCodexConverter,
    CommandCodeToOpenCodeConverter,
    CommandCodeToClaudeConverter,
    CommandCodeToDevinConverter,
    CommandCodeToFactoryConverter,
    CommandCodeToWindsurfConverter,
    CommandCodeToGrokConverter,
    CommandCodeToFreebuffConverter,
    ClaudeToCodexConverter,
    ClaudeToDevinConverter,
    ClaudeToFactoryConverter,
    ClaudeToFreebuffConverter,
    ClaudeToGrokConverter,
    ClaudeToOpenCodeConverter,
    ClaudeToPiConverter,
    ClaudeToWindsurfConverter,
    CodexToClaudeConverter,
    CodexToDevinConverter,
    CodexToFactoryConverter,
    CodexToFreebuffConverter,
    CodexToGrokConverter,
    CodexToOpenCodeConverter,
    CodexToPiConverter,
    CodexToWindsurfConverter,
    DevinToClaudeConverter,
    DevinToCodexConverter,
    DevinToFactoryConverter,
    DevinToFreebuffConverter,
    DevinToGrokConverter,
    DevinToOpenCodeConverter,
    DevinToPiConverter,
    DevinToWindsurfConverter,
    FactoryToClaudeConverter,
    FactoryToCodexConverter,
    FactoryToDevinConverter,
    FactoryToOpenCodeConverter,
    FactoryToPiConverter,
    FactoryToFreebuffConverter,
    FactoryToGrokConverter,
    FactoryToWindsurfConverter,
    OmpToClaudeConverter,
    OmpToCodexConverter,
    OmpToDevinConverter,
    OmpToFactoryConverter,
    OmpToFreebuffConverter,
    OmpToGrokConverter,
    OmpToOpenCodeConverter,
    OmpToPiConverter,
    OmpToWindsurfConverter,
    OpenCodeToClaudeConverter,
    OpenCodeToCodexConverter,
    OpenCodeToDevinConverter,
    FreebuffToClaudeConverter,
    FreebuffToCodexConverter,
    FreebuffToDevinConverter,
    FreebuffToFactoryConverter,
    FreebuffToGrokConverter,
    FreebuffToOpenCodeConverter,
    FreebuffToPiConverter,
    FreebuffToWindsurfConverter,
    GrokToClaudeConverter,
    GrokToCodexConverter,
    GrokToDevinConverter,
    GrokToFactoryConverter,
    GrokToOpenCodeConverter,
    GrokToPiConverter,
    GrokToWindsurfConverter,
    OpenCodeToFactoryConverter,
    OpenCodeToFreebuffConverter,
    OpenCodeToGrokConverter,
    OpenCodeToPiConverter,
    OpenCodeToWindsurfConverter,
    PiToClaudeConverter,
    PiToCodexConverter,
    PiToDevinConverter,
    PiToFactoryConverter,
    PiToFreebuffConverter,
    PiToGrokConverter,
    PiToOpenCodeConverter,
    PiToWindsurfConverter,
    T3ToClaudeConverter,
    T3ToCodexConverter,
    T3ToDevinConverter,
    T3ToFactoryConverter,
    T3ToFreebuffConverter,
    T3ToGrokConverter,
    T3ToOpenCodeConverter,
    T3ToPiConverter,
    T3ToWindsurfConverter,
    ToCommandCodeConverter,
    ToOmpConverter,
    ToT3Converter,
    WindsurfToClaudeConverter,
    WindsurfToCodexConverter,
    WindsurfToDevinConverter,
    WindsurfToFactoryConverter,
    WindsurfToFreebuffConverter,
    WindsurfToGrokConverter,
    WindsurfToOpenCodeConverter,
    WindsurfToPiConverter,
)
from session_sdk.models import ConversionPlan, SessionSummary
from session_sdk.paths import SessionIdFactory, WindowsDefaults
from session_sdk.stores import T3_PROVIDER_DRIVERS, T3WriteError, destination_exists, ClaudeStore, CodexStore, CommandCodeStore, DevinStore, FactoryStore, FreebuffStore, GrokStore, OmpStore, OpenCodeStore, PiDcpStore, PiStore, T3Store, WindsurfStore
from session_sdk.options import ConversionOptions, conversion_options
from session_sdk.traces import TRACE_FORMATS, build_trace
from session_sdk.converters import MessageExtractor


class CliApp:
    def __init__(self, argv: list[str] | None = None) -> None:
        self._argv = argv

    def run(self) -> int:
        args = self._parser().parse_args(self._argv)
        options = self._options(args)
        with conversion_options(options):
            return self._run_parsed(args)

    @staticmethod
    def _options(args: argparse.Namespace) -> ConversionOptions:
        """Everything is carried unless a switch turns it off (flag, or environment variable)."""
        base = ConversionOptions.from_env()
        return ConversionOptions(
            include_thinking=base.include_thinking and not getattr(args, "no_thinking", False),
            include_tool_calls=base.include_tool_calls and not getattr(args, "no_tool_calls", False),
        )

    def _run_parsed(self, args: argparse.Namespace) -> int:
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
        grok = GrokStore(
            Path(args.grok_home or defaults.grok_home),
            self._optional_path(args.grok_session_dir),
        )
        freebuff = FreebuffStore(
            Path(args.freebuff_home or defaults.freebuff_home),
            self._optional_path(args.freebuff_session_dir),
        )
        t3 = T3Store(Path(args.t3_home or defaults.t3_home))
        omp = OmpStore(
            Path(args.omp_agent_home or defaults.omp_agent_home),
            self._optional_path(args.omp_session_dir),
        )
        commandcode = CommandCodeStore(
            Path(args.commandcode_home or defaults.commandcode_home),
            self._optional_path(args.commandcode_session_dir),
        )

        if args.command == "list":
            store = {"codex": codex, "pi": pi, "opencode": opencode, "claude": claude, "devin": devin, "factory": factory, "windsurf": windsurf, "grok": grok, "freebuff": freebuff, "t3": t3, "omp": omp, "commandcode": commandcode}[args.provider]
            summaries = store.list(workers=args.workers or 1)
            self._print_summaries(summaries)
            return 0

        if args.command == "to-trace":
            return self._to_trace(args, codex, pi, opencode, claude, devin, factory, windsurf, grok, freebuff, t3, omp, commandcode)

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
                            "claude-to-windsurf", "devin-to-windsurf", "factory-to-windsurf",
                            "grok-to-pi", "grok-to-codex", "grok-to-opencode",
                            "grok-to-claude", "grok-to-devin", "grok-to-factory", "grok-to-windsurf",
                            "pi-to-grok", "codex-to-grok", "opencode-to-grok",
                            "claude-to-grok", "devin-to-grok", "factory-to-grok", "windsurf-to-grok",
                            "freebuff-to-pi", "freebuff-to-codex", "freebuff-to-opencode",
                            "freebuff-to-claude", "freebuff-to-devin", "freebuff-to-factory",
                            "freebuff-to-windsurf", "freebuff-to-grok",
                            "pi-to-freebuff", "codex-to-freebuff", "opencode-to-freebuff",
                            "claude-to-freebuff", "devin-to-freebuff", "factory-to-freebuff",
                            "windsurf-to-freebuff", "grok-to-freebuff",
                            "codex-to-t3", "pi-to-t3", "opencode-to-t3", "claude-to-t3", "devin-to-t3", "factory-to-t3", "windsurf-to-t3", "grok-to-t3", "freebuff-to-t3",
                            "t3-to-pi", "t3-to-codex", "t3-to-opencode", "t3-to-claude", "t3-to-devin", "t3-to-factory", "t3-to-windsurf", "t3-to-grok", "t3-to-freebuff",
                            "omp-to-pi", "omp-to-codex", "omp-to-opencode", "omp-to-claude", "omp-to-devin", "omp-to-factory", "omp-to-windsurf", "omp-to-grok", "omp-to-freebuff", "omp-to-t3",
                            "pi-to-omp", "codex-to-omp", "opencode-to-omp", "claude-to-omp", "devin-to-omp", "factory-to-omp", "windsurf-to-omp", "grok-to-omp", "freebuff-to-omp", "t3-to-omp",
                            "commandcode-to-omp", "omp-to-commandcode",
                            "commandcode-to-pi",
                            "pi-to-commandcode",
                            "commandcode-to-codex",
                            "codex-to-commandcode",
                            "commandcode-to-opencode",
                            "opencode-to-commandcode",
                            "commandcode-to-claude",
                            "claude-to-commandcode",
                            "commandcode-to-devin",
                            "devin-to-commandcode",
                            "commandcode-to-factory",
                            "factory-to-commandcode",
                            "commandcode-to-windsurf",
                            "windsurf-to-commandcode",
                            "commandcode-to-grok",
                            "grok-to-commandcode",
                            "commandcode-to-freebuff",
                            "freebuff-to-commandcode",
                            "commandcode-to-t3",
                            "t3-to-commandcode",
                            ):
            return self._single_convert(args, codex, pi, dcp, opencode, claude, devin, factory, windsurf, grok, freebuff, t3, omp, commandcode, id_factory)

        if args.command == "codex-to-pi-all":
            return self._bulk_export({"codex": codex, "pi": pi, "pi-dcp": dcp, "opencode": opencode, "claude": claude, "devin": devin, "factory": factory, "windsurf": windsurf, "grok": grok, "freebuff": freebuff, "omp": omp, "commandcode": commandcode}, id_factory, args, targets=["pi"])

        if args.command == "export-all":
            return self._bulk_export({"codex": codex, "pi": pi, "pi-dcp": dcp, "opencode": opencode, "claude": claude, "devin": devin, "factory": factory, "windsurf": windsurf, "grok": grok, "freebuff": freebuff, "omp": omp, "commandcode": commandcode}, id_factory, args, targets=args.targets)

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
        parser.add_argument("--grok-home", default=None)
        parser.add_argument("--grok-session-dir", default=None)
        parser.add_argument("--freebuff-home", default=None)
        parser.add_argument("--freebuff-session-dir", default=None)
        parser.add_argument("--t3-home", default=None, help="T3 Code home (default: T3CODE_HOME or ~/.t3). Threads are read from <home>/userdata, read-only.")
        parser.add_argument("--omp-agent-home", default=None, help="OMP agent directory holding sessions/ (default: resolved like omp, usually ~/.omp/agent).")
        parser.add_argument("--omp-session-dir", default=None, help="OMP sessions root holding the per-project folders (default: <omp agent home>/sessions).")
        parser.add_argument("--commandcode-home", default=None, help="Command Code data directory holding projects/ (default: ~/.commandcode).")
        parser.add_argument("--commandcode-session-dir", default=None, help="Command Code projects root holding the per-project folders (default: <commandcode home>/projects).")
        subparsers = parser.add_subparsers(dest="command", required=True)

        list_parser = subparsers.add_parser("list")
        list_parser.add_argument("provider", choices=("codex", "pi", "opencode", "claude", "devin", "factory", "windsurf", "grok", "freebuff", "t3", "omp", "commandcode"))
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

        grok_to_pi = subparsers.add_parser("grok-to-pi")
        CliApp._add_convert_args(grok_to_pi)

        pi_to_grok = subparsers.add_parser("pi-to-grok")
        CliApp._add_convert_args(pi_to_grok)

        grok_to_codex = subparsers.add_parser("grok-to-codex")
        CliApp._add_convert_args(grok_to_codex)

        codex_to_grok = subparsers.add_parser("codex-to-grok")
        CliApp._add_convert_args(codex_to_grok)

        grok_to_opencode = subparsers.add_parser("grok-to-opencode")
        CliApp._add_convert_args(grok_to_opencode)

        opencode_to_grok = subparsers.add_parser("opencode-to-grok")
        CliApp._add_convert_args(opencode_to_grok)

        grok_to_claude = subparsers.add_parser("grok-to-claude")
        CliApp._add_convert_args(grok_to_claude)

        claude_to_grok = subparsers.add_parser("claude-to-grok")
        CliApp._add_convert_args(claude_to_grok)

        grok_to_devin = subparsers.add_parser("grok-to-devin")
        CliApp._add_convert_args(grok_to_devin)

        devin_to_grok = subparsers.add_parser("devin-to-grok")
        CliApp._add_convert_args(devin_to_grok)

        grok_to_factory = subparsers.add_parser("grok-to-factory")
        CliApp._add_convert_args(grok_to_factory)

        factory_to_grok = subparsers.add_parser("factory-to-grok")
        CliApp._add_convert_args(factory_to_grok)

        grok_to_windsurf = subparsers.add_parser("grok-to-windsurf")
        CliApp._add_convert_args(grok_to_windsurf)

        windsurf_to_grok = subparsers.add_parser("windsurf-to-grok")
        CliApp._add_convert_args(windsurf_to_grok)

        freebuff_to_pi = subparsers.add_parser("freebuff-to-pi")
        CliApp._add_convert_args(freebuff_to_pi)

        pi_to_freebuff = subparsers.add_parser("pi-to-freebuff")
        CliApp._add_convert_args(pi_to_freebuff)

        freebuff_to_codex = subparsers.add_parser("freebuff-to-codex")
        CliApp._add_convert_args(freebuff_to_codex)

        codex_to_freebuff = subparsers.add_parser("codex-to-freebuff")
        CliApp._add_convert_args(codex_to_freebuff)

        freebuff_to_opencode = subparsers.add_parser("freebuff-to-opencode")
        CliApp._add_convert_args(freebuff_to_opencode)

        opencode_to_freebuff = subparsers.add_parser("opencode-to-freebuff")
        CliApp._add_convert_args(opencode_to_freebuff)

        freebuff_to_claude = subparsers.add_parser("freebuff-to-claude")
        CliApp._add_convert_args(freebuff_to_claude)

        claude_to_freebuff = subparsers.add_parser("claude-to-freebuff")
        CliApp._add_convert_args(claude_to_freebuff)

        freebuff_to_devin = subparsers.add_parser("freebuff-to-devin")
        CliApp._add_convert_args(freebuff_to_devin)

        devin_to_freebuff = subparsers.add_parser("devin-to-freebuff")
        CliApp._add_convert_args(devin_to_freebuff)

        freebuff_to_factory = subparsers.add_parser("freebuff-to-factory")
        CliApp._add_convert_args(freebuff_to_factory)

        factory_to_freebuff = subparsers.add_parser("factory-to-freebuff")
        CliApp._add_convert_args(factory_to_freebuff)

        freebuff_to_windsurf = subparsers.add_parser("freebuff-to-windsurf")
        CliApp._add_convert_args(freebuff_to_windsurf)

        windsurf_to_freebuff = subparsers.add_parser("windsurf-to-freebuff")
        CliApp._add_convert_args(windsurf_to_freebuff)

        freebuff_to_grok = subparsers.add_parser("freebuff-to-grok")
        CliApp._add_convert_args(freebuff_to_grok)

        grok_to_freebuff = subparsers.add_parser("grok-to-freebuff")
        CliApp._add_convert_args(grok_to_freebuff)

        t3_to_pi = subparsers.add_parser("t3-to-pi")
        CliApp._add_convert_args(t3_to_pi)

        t3_to_codex = subparsers.add_parser("t3-to-codex")
        CliApp._add_convert_args(t3_to_codex)

        t3_to_opencode = subparsers.add_parser("t3-to-opencode")
        CliApp._add_convert_args(t3_to_opencode)

        t3_to_claude = subparsers.add_parser("t3-to-claude")
        CliApp._add_convert_args(t3_to_claude)

        t3_to_devin = subparsers.add_parser("t3-to-devin")
        CliApp._add_convert_args(t3_to_devin)

        t3_to_factory = subparsers.add_parser("t3-to-factory")
        CliApp._add_convert_args(t3_to_factory)

        t3_to_windsurf = subparsers.add_parser("t3-to-windsurf")
        CliApp._add_convert_args(t3_to_windsurf)

        t3_to_grok = subparsers.add_parser("t3-to-grok")
        CliApp._add_convert_args(t3_to_grok)

        t3_to_freebuff = subparsers.add_parser("t3-to-freebuff")
        CliApp._add_convert_args(t3_to_freebuff)

        codex_to_t3 = subparsers.add_parser("codex-to-t3")
        CliApp._add_convert_args(codex_to_t3)
        CliApp._add_t3_target_args(codex_to_t3)

        pi_to_t3 = subparsers.add_parser("pi-to-t3")
        CliApp._add_convert_args(pi_to_t3)
        CliApp._add_t3_target_args(pi_to_t3)

        opencode_to_t3 = subparsers.add_parser("opencode-to-t3")
        CliApp._add_convert_args(opencode_to_t3)
        CliApp._add_t3_target_args(opencode_to_t3)

        claude_to_t3 = subparsers.add_parser("claude-to-t3")
        CliApp._add_convert_args(claude_to_t3)
        CliApp._add_t3_target_args(claude_to_t3)

        devin_to_t3 = subparsers.add_parser("devin-to-t3")
        CliApp._add_convert_args(devin_to_t3)
        CliApp._add_t3_target_args(devin_to_t3)

        factory_to_t3 = subparsers.add_parser("factory-to-t3")
        CliApp._add_convert_args(factory_to_t3)
        CliApp._add_t3_target_args(factory_to_t3)

        windsurf_to_t3 = subparsers.add_parser("windsurf-to-t3")
        CliApp._add_convert_args(windsurf_to_t3)
        CliApp._add_t3_target_args(windsurf_to_t3)

        grok_to_t3 = subparsers.add_parser("grok-to-t3")
        CliApp._add_convert_args(grok_to_t3)
        CliApp._add_t3_target_args(grok_to_t3)

        freebuff_to_t3 = subparsers.add_parser("freebuff-to-t3")
        CliApp._add_convert_args(freebuff_to_t3)
        CliApp._add_t3_target_args(freebuff_to_t3)

        omp_to_pi = subparsers.add_parser("omp-to-pi")
        CliApp._add_convert_args(omp_to_pi)

        omp_to_codex = subparsers.add_parser("omp-to-codex")
        CliApp._add_convert_args(omp_to_codex)

        omp_to_opencode = subparsers.add_parser("omp-to-opencode")
        CliApp._add_convert_args(omp_to_opencode)

        omp_to_claude = subparsers.add_parser("omp-to-claude")
        CliApp._add_convert_args(omp_to_claude)

        omp_to_devin = subparsers.add_parser("omp-to-devin")
        CliApp._add_convert_args(omp_to_devin)

        omp_to_factory = subparsers.add_parser("omp-to-factory")
        CliApp._add_convert_args(omp_to_factory)

        omp_to_windsurf = subparsers.add_parser("omp-to-windsurf")
        CliApp._add_convert_args(omp_to_windsurf)

        omp_to_grok = subparsers.add_parser("omp-to-grok")
        CliApp._add_convert_args(omp_to_grok)

        omp_to_freebuff = subparsers.add_parser("omp-to-freebuff")
        CliApp._add_convert_args(omp_to_freebuff)

        omp_to_t3 = subparsers.add_parser("omp-to-t3")
        CliApp._add_convert_args(omp_to_t3)
        CliApp._add_t3_target_args(omp_to_t3)

        pi_to_omp = subparsers.add_parser("pi-to-omp")
        CliApp._add_convert_args(pi_to_omp)

        codex_to_omp = subparsers.add_parser("codex-to-omp")
        CliApp._add_convert_args(codex_to_omp)

        opencode_to_omp = subparsers.add_parser("opencode-to-omp")
        CliApp._add_convert_args(opencode_to_omp)

        claude_to_omp = subparsers.add_parser("claude-to-omp")
        CliApp._add_convert_args(claude_to_omp)

        devin_to_omp = subparsers.add_parser("devin-to-omp")
        CliApp._add_convert_args(devin_to_omp)

        factory_to_omp = subparsers.add_parser("factory-to-omp")
        CliApp._add_convert_args(factory_to_omp)

        windsurf_to_omp = subparsers.add_parser("windsurf-to-omp")
        CliApp._add_convert_args(windsurf_to_omp)

        grok_to_omp = subparsers.add_parser("grok-to-omp")
        CliApp._add_convert_args(grok_to_omp)

        freebuff_to_omp = subparsers.add_parser("freebuff-to-omp")
        CliApp._add_convert_args(freebuff_to_omp)

        t3_to_omp = subparsers.add_parser("t3-to-omp")
        CliApp._add_convert_args(t3_to_omp)

        commandcode_to_pi = subparsers.add_parser("commandcode-to-pi")
        CliApp._add_convert_args(commandcode_to_pi)
        pi_to_commandcode = subparsers.add_parser("pi-to-commandcode")
        CliApp._add_convert_args(pi_to_commandcode)
        commandcode_to_codex = subparsers.add_parser("commandcode-to-codex")
        CliApp._add_convert_args(commandcode_to_codex)
        codex_to_commandcode = subparsers.add_parser("codex-to-commandcode")
        CliApp._add_convert_args(codex_to_commandcode)
        commandcode_to_opencode = subparsers.add_parser("commandcode-to-opencode")
        CliApp._add_convert_args(commandcode_to_opencode)
        opencode_to_commandcode = subparsers.add_parser("opencode-to-commandcode")
        CliApp._add_convert_args(opencode_to_commandcode)
        commandcode_to_claude = subparsers.add_parser("commandcode-to-claude")
        CliApp._add_convert_args(commandcode_to_claude)
        claude_to_commandcode = subparsers.add_parser("claude-to-commandcode")
        CliApp._add_convert_args(claude_to_commandcode)
        commandcode_to_devin = subparsers.add_parser("commandcode-to-devin")
        CliApp._add_convert_args(commandcode_to_devin)
        devin_to_commandcode = subparsers.add_parser("devin-to-commandcode")
        CliApp._add_convert_args(devin_to_commandcode)
        commandcode_to_factory = subparsers.add_parser("commandcode-to-factory")
        CliApp._add_convert_args(commandcode_to_factory)
        factory_to_commandcode = subparsers.add_parser("factory-to-commandcode")
        CliApp._add_convert_args(factory_to_commandcode)
        commandcode_to_windsurf = subparsers.add_parser("commandcode-to-windsurf")
        CliApp._add_convert_args(commandcode_to_windsurf)
        windsurf_to_commandcode = subparsers.add_parser("windsurf-to-commandcode")
        CliApp._add_convert_args(windsurf_to_commandcode)
        commandcode_to_grok = subparsers.add_parser("commandcode-to-grok")
        CliApp._add_convert_args(commandcode_to_grok)
        grok_to_commandcode = subparsers.add_parser("grok-to-commandcode")
        CliApp._add_convert_args(grok_to_commandcode)
        commandcode_to_freebuff = subparsers.add_parser("commandcode-to-freebuff")
        CliApp._add_convert_args(commandcode_to_freebuff)
        freebuff_to_commandcode = subparsers.add_parser("freebuff-to-commandcode")
        CliApp._add_convert_args(freebuff_to_commandcode)
        commandcode_to_omp = subparsers.add_parser("commandcode-to-omp")
        CliApp._add_convert_args(commandcode_to_omp)
        omp_to_commandcode = subparsers.add_parser("omp-to-commandcode")
        CliApp._add_convert_args(omp_to_commandcode)
        commandcode_to_t3 = subparsers.add_parser("commandcode-to-t3")
        CliApp._add_convert_args(commandcode_to_t3)
        CliApp._add_t3_target_args(commandcode_to_t3)
        t3_to_commandcode = subparsers.add_parser("t3-to-commandcode")
        CliApp._add_convert_args(t3_to_commandcode)

        codex_to_pi_all = subparsers.add_parser("codex-to-pi-all")
        CliApp._add_bulk_args(codex_to_pi_all)

        export_all = subparsers.add_parser("export-all")
        CliApp._add_bulk_args(export_all)
        export_all.add_argument("--targets", nargs="+", default=["pi"], choices=("pi", "opencode", "claude", "devin", "factory", "windsurf", "grok", "freebuff", "omp", "commandcode"))

        to_trace = subparsers.add_parser("to-trace")
        to_trace.add_argument("provider", choices=("codex", "pi", "opencode", "claude", "devin", "factory", "windsurf", "grok", "freebuff", "t3", "omp", "commandcode"))
        to_trace.add_argument("session_id")
        to_trace.add_argument("--no-thinking", action="store_true", help="Leave reasoning out of the output (default: carried over).")
        to_trace.add_argument("--no-tool-calls", action="store_true", help="Leave tool calls and their outputs out of the output (default: carried over).")
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
        parser.add_argument("--no-thinking", action="store_true", help="Leave reasoning out of the output (default: carried over).")
        parser.add_argument("--no-tool-calls", action="store_true", help="Leave tool calls and their outputs out of the output (default: carried over).")
        parser.add_argument("--overwrite", action="store_true", help="Allow replacing existing destinations.")
        parser.add_argument("--new-id", action="store_true", help="Generate new target session IDs.")
        parser.add_argument("--workers", type=int, default=None, help="Number of parallel workers (default: 8).")
        parser.add_argument("--on-conflict", choices=("skip", "overwrite", "fork", "update"), default="skip",
                            help="Conflict resolution: skip, overwrite, fork (new ID), or update (overwrite if changed).")

    @staticmethod
    def _codex_converter(target: str, stores: dict[str, object], id_factory: SessionIdFactory):
        """Converter from Codex to one export target. Every target is supported, as in the single commands."""
        codex = stores["codex"]
        if target == "pi":
            return CodexToPiConverter(codex, stores["pi"], stores["pi-dcp"], id_factory)  # type: ignore[arg-type]
        builders = {
            "opencode": CodexToOpenCodeConverter,
            "claude": CodexToClaudeConverter,
            "devin": CodexToDevinConverter,
            "factory": CodexToFactoryConverter,
            "windsurf": CodexToWindsurfConverter,
            "grok": CodexToGrokConverter,
            "freebuff": CodexToFreebuffConverter,
            "omp": ToOmpConverter,
            "commandcode": ToCommandCodeConverter,
        }
        return builders[target](codex, stores[target], id_factory)  # type: ignore[call-arg, arg-type]

    def _bulk_export(
        self,
        stores: dict[str, object],
        id_factory: SessionIdFactory,
        args: argparse.Namespace,
        targets: list[str],
    ) -> int:
        workers = args.workers or 8
        codex = stores["codex"]
        summaries = codex.list(workers=1)  # type: ignore[attr-defined]
        total = len(summaries)
        print(f"Found {total} Codex sessions. Workers: {workers}. Targets: {', '.join(targets)}")
        if not args.write:
            print("Dry run -- use --write to export.")
            return 0

        converters = {target: self._codex_converter(target, stores, id_factory) for target in targets}

        exported = 0
        skipped = 0
        failed = 0

        def convert_one(summary: SessionSummary) -> tuple[str, str, str]:
            sid = summary.session_id
            try:
                results = []
                for target in targets:
                    converter = converters[target]
                    plan = converter.plan(sid)
                    if self._destination_taken(converter, plan):
                        action = self._resolve_conflict(args, plan)
                        if action == "skip":
                            results.append(f"{target}:skipped")
                            continue
                        if action == "fork":
                            plan = converter.plan(sid, target_id=str(uuid4()))
                    converter.write(plan, overwrite=True)
                    results.append(f"{target}:ok")
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
    def _add_t3_target_args(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--t3-provider",
            choices=sorted(T3_PROVIDER_DRIVERS),
            default="codex",
            help="T3 provider the imported thread runs on (experimental, default: codex).",
        )

    @staticmethod
    def _add_convert_args(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("session_id")
        parser.add_argument("--write", action="store_true", help="Write the converted session.")
        parser.add_argument("--no-thinking", action="store_true", help="Leave reasoning out of the output (default: carried over).")
        parser.add_argument("--no-tool-calls", action="store_true", help="Leave tool calls and their outputs out of the output (default: carried over).")
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
        grok: GrokStore,
        freebuff: FreebuffStore,
        t3: T3Store,
        omp: OmpStore,
        commandcode: CommandCodeStore,
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
            "grok-to-pi": lambda: GrokToPiConverter(grok, pi, dcp, id_factory),
            "pi-to-grok": lambda: PiToGrokConverter(pi, grok, id_factory),
            "grok-to-codex": lambda: GrokToCodexConverter(grok, codex, id_factory),
            "codex-to-grok": lambda: CodexToGrokConverter(codex, grok, id_factory),
            "grok-to-opencode": lambda: GrokToOpenCodeConverter(grok, opencode, id_factory),
            "opencode-to-grok": lambda: OpenCodeToGrokConverter(opencode, grok, id_factory),
            "grok-to-claude": lambda: GrokToClaudeConverter(grok, claude, id_factory),
            "claude-to-grok": lambda: ClaudeToGrokConverter(claude, grok, id_factory),
            "grok-to-devin": lambda: GrokToDevinConverter(grok, devin, id_factory),
            "devin-to-grok": lambda: DevinToGrokConverter(devin, grok, id_factory),
            "grok-to-factory": lambda: GrokToFactoryConverter(grok, factory, id_factory),
            "factory-to-grok": lambda: FactoryToGrokConverter(factory, grok, id_factory),
            "grok-to-windsurf": lambda: GrokToWindsurfConverter(grok, windsurf, id_factory),
            "windsurf-to-grok": lambda: WindsurfToGrokConverter(windsurf, grok, id_factory),
            "freebuff-to-pi": lambda: FreebuffToPiConverter(freebuff, pi, dcp, id_factory),
            "pi-to-freebuff": lambda: PiToFreebuffConverter(pi, freebuff, id_factory),
            "freebuff-to-codex": lambda: FreebuffToCodexConverter(freebuff, codex, id_factory),
            "codex-to-freebuff": lambda: CodexToFreebuffConverter(codex, freebuff, id_factory),
            "freebuff-to-opencode": lambda: FreebuffToOpenCodeConverter(freebuff, opencode, id_factory),
            "opencode-to-freebuff": lambda: OpenCodeToFreebuffConverter(opencode, freebuff, id_factory),
            "freebuff-to-claude": lambda: FreebuffToClaudeConverter(freebuff, claude, id_factory),
            "claude-to-freebuff": lambda: ClaudeToFreebuffConverter(claude, freebuff, id_factory),
            "freebuff-to-devin": lambda: FreebuffToDevinConverter(freebuff, devin, id_factory),
            "devin-to-freebuff": lambda: DevinToFreebuffConverter(devin, freebuff, id_factory),
            "freebuff-to-factory": lambda: FreebuffToFactoryConverter(freebuff, factory, id_factory),
            "factory-to-freebuff": lambda: FactoryToFreebuffConverter(factory, freebuff, id_factory),
            "freebuff-to-windsurf": lambda: FreebuffToWindsurfConverter(freebuff, windsurf, id_factory),
            "windsurf-to-freebuff": lambda: WindsurfToFreebuffConverter(windsurf, freebuff, id_factory),
            "freebuff-to-grok": lambda: FreebuffToGrokConverter(freebuff, grok, id_factory),
            "grok-to-freebuff": lambda: GrokToFreebuffConverter(grok, freebuff, id_factory),
            "t3-to-pi": lambda: T3ToPiConverter(t3, pi, dcp, id_factory),
            "t3-to-codex": lambda: T3ToCodexConverter(t3, codex, id_factory),
            "t3-to-opencode": lambda: T3ToOpenCodeConverter(t3, opencode, id_factory),
            "t3-to-claude": lambda: T3ToClaudeConverter(t3, claude, id_factory),
            "t3-to-devin": lambda: T3ToDevinConverter(t3, devin, id_factory),
            "t3-to-factory": lambda: T3ToFactoryConverter(t3, factory, id_factory),
            "t3-to-windsurf": lambda: T3ToWindsurfConverter(t3, windsurf, id_factory),
            "t3-to-grok": lambda: T3ToGrokConverter(t3, grok, id_factory),
            "t3-to-freebuff": lambda: T3ToFreebuffConverter(t3, freebuff, id_factory),
            "codex-to-t3": lambda: ToT3Converter(codex, t3, id_factory, args.t3_provider),
            "pi-to-t3": lambda: ToT3Converter(pi, t3, id_factory, args.t3_provider),
            "opencode-to-t3": lambda: ToT3Converter(opencode, t3, id_factory, args.t3_provider),
            "claude-to-t3": lambda: ToT3Converter(claude, t3, id_factory, args.t3_provider),
            "devin-to-t3": lambda: ToT3Converter(devin, t3, id_factory, args.t3_provider),
            "factory-to-t3": lambda: ToT3Converter(factory, t3, id_factory, args.t3_provider),
            "windsurf-to-t3": lambda: ToT3Converter(windsurf, t3, id_factory, args.t3_provider),
            "grok-to-t3": lambda: ToT3Converter(grok, t3, id_factory, args.t3_provider),
            "freebuff-to-t3": lambda: ToT3Converter(freebuff, t3, id_factory, args.t3_provider),
            "omp-to-pi": lambda: OmpToPiConverter(omp, pi, dcp, id_factory),
            "omp-to-codex": lambda: OmpToCodexConverter(omp, codex, id_factory),
            "omp-to-opencode": lambda: OmpToOpenCodeConverter(omp, opencode, id_factory),
            "omp-to-claude": lambda: OmpToClaudeConverter(omp, claude, id_factory),
            "omp-to-devin": lambda: OmpToDevinConverter(omp, devin, id_factory),
            "omp-to-factory": lambda: OmpToFactoryConverter(omp, factory, id_factory),
            "omp-to-windsurf": lambda: OmpToWindsurfConverter(omp, windsurf, id_factory),
            "omp-to-grok": lambda: OmpToGrokConverter(omp, grok, id_factory),
            "omp-to-freebuff": lambda: OmpToFreebuffConverter(omp, freebuff, id_factory),
            "omp-to-t3": lambda: ToT3Converter(omp, t3, id_factory, args.t3_provider),
            "pi-to-omp": lambda: ToOmpConverter(pi, omp, id_factory),
            "codex-to-omp": lambda: ToOmpConverter(codex, omp, id_factory),
            "opencode-to-omp": lambda: ToOmpConverter(opencode, omp, id_factory),
            "claude-to-omp": lambda: ToOmpConverter(claude, omp, id_factory),
            "devin-to-omp": lambda: ToOmpConverter(devin, omp, id_factory),
            "factory-to-omp": lambda: ToOmpConverter(factory, omp, id_factory),
            "windsurf-to-omp": lambda: ToOmpConverter(windsurf, omp, id_factory),
            "grok-to-omp": lambda: ToOmpConverter(grok, omp, id_factory),
            "freebuff-to-omp": lambda: ToOmpConverter(freebuff, omp, id_factory),
            "t3-to-omp": lambda: ToOmpConverter(t3, omp, id_factory),
            "commandcode-to-pi": lambda: CommandCodeToPiConverter(commandcode, pi, dcp, id_factory),
            "pi-to-commandcode": lambda: ToCommandCodeConverter(pi, commandcode, id_factory),
            "commandcode-to-codex": lambda: CommandCodeToCodexConverter(commandcode, codex, id_factory),
            "codex-to-commandcode": lambda: ToCommandCodeConverter(codex, commandcode, id_factory),
            "commandcode-to-opencode": lambda: CommandCodeToOpenCodeConverter(commandcode, opencode, id_factory),
            "opencode-to-commandcode": lambda: ToCommandCodeConverter(opencode, commandcode, id_factory),
            "commandcode-to-claude": lambda: CommandCodeToClaudeConverter(commandcode, claude, id_factory),
            "claude-to-commandcode": lambda: ToCommandCodeConverter(claude, commandcode, id_factory),
            "commandcode-to-devin": lambda: CommandCodeToDevinConverter(commandcode, devin, id_factory),
            "devin-to-commandcode": lambda: ToCommandCodeConverter(devin, commandcode, id_factory),
            "commandcode-to-factory": lambda: CommandCodeToFactoryConverter(commandcode, factory, id_factory),
            "factory-to-commandcode": lambda: ToCommandCodeConverter(factory, commandcode, id_factory),
            "commandcode-to-windsurf": lambda: CommandCodeToWindsurfConverter(commandcode, windsurf, id_factory),
            "windsurf-to-commandcode": lambda: ToCommandCodeConverter(windsurf, commandcode, id_factory),
            "commandcode-to-grok": lambda: CommandCodeToGrokConverter(commandcode, grok, id_factory),
            "grok-to-commandcode": lambda: ToCommandCodeConverter(grok, commandcode, id_factory),
            "commandcode-to-freebuff": lambda: CommandCodeToFreebuffConverter(commandcode, freebuff, id_factory),
            "freebuff-to-commandcode": lambda: ToCommandCodeConverter(freebuff, commandcode, id_factory),
            "commandcode-to-omp": lambda: ToOmpConverter(commandcode, omp, id_factory),
            "omp-to-commandcode": lambda: ToCommandCodeConverter(omp, commandcode, id_factory),
            "commandcode-to-t3": lambda: ToT3Converter(commandcode, t3, id_factory, args.t3_provider),
            "t3-to-commandcode": lambda: ToCommandCodeConverter(t3, commandcode, id_factory),
        }
        converter = converters[args.command]()
        sid = args.session_id

        if args.command.endswith("-to-t3"):
            print("EXPERIMENTAL: T3 Code writes may not match every T3 release. Open the result in a new, empty T3 home.", file=sys.stderr)
            if not args.t3_home:
                print("Pass --t3-home <new empty folder> to choose where the thread goes. Without it, the destination shown is the default T3 home. The live T3 data directory is never written.", file=sys.stderr)
                if args.write:
                    return 2

        target_id: str | None = None
        if args.write and args.on_conflict == "fork":
            # Check if destination exists before forking
            plan = converter.plan(sid)
            if self._destination_taken(converter, plan):
                target_id = str(uuid4())
                plan = converter.plan(sid, target_id=target_id)
                print(f"Forked to new session ID: {target_id}")
            self._print_plan(plan)
            converter.write(plan, overwrite=True)
            return 0

        plan = converter.plan(sid, target_id=target_id)
        self._print_plan(plan)
        if args.write:
            if self._destination_taken(converter, plan):
                action = self._resolve_conflict(args, plan)
                if action == "skip":
                    print(f"Destination exists, skipping: {plan.destination}")
                    return 0
            try:
                converter.write(plan, overwrite=True)
            except T3WriteError as error:
                print(f"error: {error}", file=sys.stderr)
                return 2
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
        grok: GrokStore,
        freebuff: FreebuffStore,
        t3: T3Store,
        omp: OmpStore,
        commandcode: CommandCodeStore,
    ) -> int:
        stores = {"codex": codex, "pi": pi, "opencode": opencode, "claude": claude, "devin": devin, "factory": factory, "windsurf": windsurf, "grok": grok, "freebuff": freebuff, "t3": t3, "omp": omp, "commandcode": commandcode}
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
            "grok": extractor.from_grok,
            "freebuff": extractor.from_freebuff,
            "t3": extractor.from_t3,
            "omp": extractor.from_omp,
            "commandcode": extractor.from_commandcode,
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

    @staticmethod
    def _destination_taken(converter, plan: ConversionPlan) -> bool:
        """Whether the session already exists at its destination. Most targets keep one file per
        session. T3 keeps every thread in one database, so its converter answers per thread."""
        taken = getattr(converter, "destination_taken", None)
        if taken is not None:
            return bool(taken(plan))
        return destination_exists(plan.destination)

    def _resolve_conflict(self, args: argparse.Namespace, plan: ConversionPlan) -> str:
        """Returns 'skip', 'overwrite', or 'fork'."""
        if args.overwrite:
            return "overwrite"
        mode = args.on_conflict
        if mode == "overwrite":
            return "overwrite"
        if mode == "fork":
            return "fork"
        if mode == "update":
            return "overwrite" if self._source_changed(plan) else "skip"
        return "skip"

    @classmethod
    def _source_changed(cls, plan: ConversionPlan) -> bool:
        """``update`` rewrites a session only when its source was modified after the destination was written.
        Both times come from the file system. A session inside a database uses the database file's time."""
        source = cls._mtime_ns(plan.source.path)
        destination = cls._mtime_ns(plan.destination)
        if source is None or destination is None:
            return True
        return source > destination

    @staticmethod
    def _mtime_ns(path: Path) -> int | None:
        for candidate in (path, path.parent):
            try:
                return candidate.stat().st_mtime_ns
            except OSError:
                continue
        return None

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
