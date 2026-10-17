from __future__ import annotations

import unittest
from pathlib import Path

from session_sdk.converters import (
    ClaudeRecordBuilder,
    CommandCodeRecordBuilder,
    CodexRecordBuilder,
    DevinRecordBuilder,
    FactoryRecordBuilder,
    FreebuffRecordBuilder,
    GrokRecordBuilder,
    MessageExtractor,
    OmpRecordBuilder,
    OpenCodeExportBuilder,
    PiRecordBuilder,
    WindsurfRecordBuilder,
)
from session_sdk.models import NativeSession, TextMessage, ToolCall

SESSION = "11111111-2222-4333-8444-555555555555"
CWD = r"C:\work\demo"
TS = "2026-06-01T10:00:00.000Z"
THINKING = "Plan first, then act."
CALLS = (
    ToolCall(id="call-1", name="run_command", arguments={"command": "ls"}, output="a.txt", is_error=False),
    ToolCall(id="call-2", name="read_files", arguments={"path": "b.txt"}, output="permission denied", is_error=True),
)


def conversation() -> list[TextMessage]:
    return [
        TextMessage("user", "List the files and read b.txt", TS),
        TextMessage("assistant", "Here is what I found.", "2026-06-01T10:00:05.000Z", thinking=THINKING, tool_calls=CALLS),
    ]


def windsurf_plaintext(blob: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return AESGCM(b"safeCodeiumworldKeYsecretBalloon").decrypt(blob[:12], blob[12:], None)


# target name -> (build records, extract messages from them)
def targets() -> dict[str, tuple[object, object]]:
    extractor = MessageExtractor()

    def session(provider: str, records: list[object]) -> NativeSession:
        return NativeSession(provider, SESSION, CWD, TS, Path(f"{provider}.bin"), records)  # type: ignore[arg-type]

    def pi():
        records = PiRecordBuilder().build(SESSION, CWD, TS, conversation())
        return extractor.from_pi(session("pi", records))

    def claude():
        records = ClaudeRecordBuilder().build(SESSION, CWD, TS, conversation())
        return extractor.from_claude(session("claude", records))

    def codex():
        records = CodexRecordBuilder().build(SESSION, CWD, TS, conversation())
        return extractor.from_codex(session("codex", records))

    def opencode():
        records = OpenCodeExportBuilder().build(SESSION, CWD, TS, conversation())
        return extractor.from_opencode(session("opencode", records))

    def devin():
        records = DevinRecordBuilder().build(SESSION, CWD, TS, conversation())
        return extractor.from_devin(session("devin", records))

    def factory():
        records = FactoryRecordBuilder().build(SESSION, CWD, TS, conversation())
        return extractor.from_factory(session("factory", records))

    def windsurf():
        blobs = WindsurfRecordBuilder().build(SESSION, CWD, TS, conversation())
        return extractor.from_windsurf(session("windsurf", [{"_plaintext": windsurf_plaintext(blobs[0])}]))

    def omp():
        records = OmpRecordBuilder().build(SESSION, CWD, TS, conversation())
        return extractor.from_omp(session("omp", records))

    def commandcode():
        records = CommandCodeRecordBuilder().build(SESSION, CWD, TS, conversation())
        return extractor.from_commandcode(session("commandcode", records))

    def grok():
        records = GrokRecordBuilder().build(SESSION, CWD, TS, conversation())
        return extractor.from_grok(session("grok", records))

    def freebuff():
        records = FreebuffRecordBuilder().build(SESSION, CWD, TS, conversation())
        return extractor.from_freebuff(session("freebuff", records))

    return {
        "pi": pi,
        "claude": claude,
        "codex": codex,
        "opencode": opencode,
        "devin": devin,
        "factory": factory,
        "windsurf": windsurf,
        "grok": grok,
        "freebuff": freebuff,
        "omp": omp,
        "commandcode": commandcode,
    }


# What each target can hold. Windsurf reads only run_command steps and has no error flag.
# Devin and Codex have no error flag either.
ERROR_FLAGS = {"pi", "claude", "opencode", "factory", "grok", "freebuff", "omp"}


class ThinkingAndToolRoundTripTests(unittest.TestCase):
    def test_every_target_keeps_thinking_and_tool_calls(self) -> None:
        for name, run in targets().items():
            with self.subTest(target=name):
                messages = run()  # type: ignore[operator]
                assistant = next(m for m in messages if m.role == "assistant")
                self.assertEqual(assistant.thinking, THINKING)
                self.assertEqual([call.output for call in assistant.tool_calls], ["a.txt", "permission denied"])
                self.assertEqual([call.name for call in assistant.tool_calls], ["run_command", "read_files"])
                self.assertEqual([call.arguments for call in assistant.tool_calls], [{"command": "ls"}, {"path": "b.txt"}])
                if name in ERROR_FLAGS:
                    self.assertEqual([call.is_error for call in assistant.tool_calls], [False, True])

    def test_text_is_unchanged_by_the_new_fields(self) -> None:
        for name, run in targets().items():
            with self.subTest(target=name):
                messages = run()  # type: ignore[operator]
                texts = [m.text for m in messages if m.text]
                self.assertIn("List the files and read b.txt", texts)
                self.assertIn("Here is what I found.", texts)


if __name__ == "__main__":
    unittest.main()
