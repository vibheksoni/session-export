from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from session_sdk.commandcode_format import CommandCodeFormatError, detect_version, migrate
from session_sdk.converters import (
    CommandCodeRecordBuilder,
    CommandCodeToPiConverter,
    MessageExtractor,
    ToCommandCodeConverter,
)
from session_sdk.models import NativeSession, TextMessage, ToolCall
from session_sdk.paths import SessionIdFactory, encode_commandcode_cwd
from session_sdk.stores import CommandCodeStore, DevinStore, PiDcpStore, PiStore
from unisessions.cli import main

SESSION = "3f2c1bfd-cebd-4d28-96ca-b165187b55cb"
CWD = r"C:\work\demo"
TS = "2026-06-02T09:00:00.000Z"


def lines(*records: object) -> list[str]:
    return [json.dumps(record) for record in records]


def v3_fixture() -> list[str]:
    """A version 3 file with a branch that was abandoned after its first prompt. The leaf is the last entry."""
    return lines(
        {"type": "session", "version": 3, "id": SESSION, "timestamp": TS, "cwd": CWD},
        {"type": "message", "id": "00000001", "parentId": None, "timestamp": TS, "message": {"role": "user", "content": [{"type": "text", "text": "Plan the app"}]}},
        {"type": "message", "id": "00000002", "parentId": "00000001", "timestamp": TS, "message": {"role": "assistant", "content": [{"type": "text", "text": "abandoned reply"}]}},
        {"type": "message", "id": "00000003", "parentId": "00000001", "timestamp": TS, "message": {"role": "user", "content": [{"type": "text", "text": "Use Python"}]}},
        {"type": "message", "id": "00000004", "parentId": "00000003", "timestamp": TS, "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "Plan first.", "signature": ""},
            {"type": "text", "text": "Python it is."},
            {"type": "tool_use", "id": "t9", "name": "bash", "input": {"command": "pwd"}},
        ]}},
        {"type": "message", "id": "00000005", "parentId": "00000004", "timestamp": TS, "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t9", "content": [{"type": "text", "text": r"C:\work"}]},
        ]}},
    )


def v2_fixture() -> list[str]:
    """Version 2: one flat line per message, AI SDK style parts, and a tool result in a ``tool`` message."""
    meta = {"version": 2}
    return lines(
        {"id": "a0000001", "timestamp": "2026-06-01T10:00:00.000Z", "sessionId": "v2", "parentId": None, "role": "user",
         "content": [{"type": "text", "text": "Read b.txt"}], "gitBranch": "-", "metadata": meta},
        {"id": "a0000002", "timestamp": "2026-06-01T10:00:01.000Z", "sessionId": "v2", "parentId": "a0000001", "role": "assistant",
         "content": [{"type": "reasoning", "text": "Open it."}, {"type": "text", "text": "Reading."},
                     {"type": "tool-call", "toolCallId": "c2", "toolName": "read_file", "input": {"path": "b.txt"}}],
         "gitBranch": "-", "metadata": meta},
        {"id": "a0000003", "timestamp": "2026-06-01T10:00:02.000Z", "sessionId": "v2", "parentId": "a0000002", "role": "tool",
         "content": [{"type": "tool-result", "toolCallId": "c2", "toolName": "read_file", "output": {"type": "text", "value": "bytes"}}],
         "gitBranch": "-", "metadata": meta},
    )


def v1_fixture() -> list[str]:
    """Version 1: plain Anthropic-style lines with no ids and no metadata."""
    return lines(
        {"role": "user", "content": "List the files"},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "Check first."},
            {"type": "text", "text": "Listing."},
            {"type": "tool_use", "id": "tc-1", "name": "bash", "input": {"command": "ls"}},
        ]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tc-1", "content": "a.txt"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "Done."}]},
    )


def write(path: Path, content: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(content) + "\n", encoding="utf-8")
    return path


class CommandCodeFormatTests(unittest.TestCase):
    def test_detects_each_version(self) -> None:
        self.assertEqual(detect_version(v3_fixture()), 3)
        self.assertEqual(detect_version(v2_fixture()), 2)
        self.assertEqual(detect_version(v1_fixture()), 1)

    def test_refuses_a_newer_format(self) -> None:
        newer = lines({"type": "session", "version": 4, "id": SESSION, "timestamp": TS, "cwd": CWD})
        with self.assertRaisesRegex(CommandCodeFormatError, "format v4"):
            migrate(newer, session_id=SESSION, cwd=CWD, fallback_timestamp=TS)

    def test_version_three_keeps_the_branch_records_as_written(self) -> None:
        migrated = migrate(v3_fixture(), session_id=SESSION, cwd=CWD, fallback_timestamp=TS)
        self.assertEqual(migrated.records[0]["cwd"], CWD)
        self.assertEqual([r["id"] for r in migrated.records[1:]], ["00000001", "00000002", "00000003", "00000004", "00000005"])
        self.assertEqual(migrated.corrupted, 0)

    def test_version_two_becomes_version_three_messages(self) -> None:
        migrated = migrate(v2_fixture(), session_id="v2", cwd=CWD, fallback_timestamp=TS)
        header, *entries = migrated.records
        self.assertEqual((header["version"], header["id"], header["cwd"]), (3, "v2", CWD))
        assistant = entries[1]["message"]
        self.assertEqual([b["type"] for b in assistant["content"]], ["thinking", "text", "tool_use"])
        self.assertEqual(assistant["content"][0]["thinking"], "Open it.")
        self.assertEqual(assistant["content"][2]["input"], {"path": "b.txt"})
        self.assertEqual(entries[2]["message"]["content"][0]["tool_use_id"], "c2")
        self.assertEqual(entries[2]["parentId"], entries[1]["id"])

    def test_version_one_pairs_tool_results_and_keeps_thinking(self) -> None:
        migrated = migrate(v1_fixture(), session_id="v1", cwd=CWD, fallback_timestamp=TS)
        messages = [entry["message"] for entry in migrated.records[1:]]
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "user", "assistant"])
        self.assertEqual([b["type"] for b in messages[1]["content"]], ["thinking", "text", "tool_use"])
        self.assertEqual(messages[2]["content"][0]["tool_use_id"], "tc-1")

    def test_the_model_goes_on_the_last_reply_of_an_old_file(self) -> None:
        migrated = migrate(v1_fixture(), session_id="v1", cwd=CWD, fallback_timestamp=TS, model="poolside/laguna-s-2.1-free")
        self.assertEqual(migrated.records[-1]["model"], "poolside/laguna-s-2.1-free")
        self.assertNotIn("model", migrated.records[-2])

    def test_unreadable_lines_are_counted_not_fatal(self) -> None:
        content = v3_fixture() + ["{not json", "[1, 2]"]
        migrated = migrate(content, session_id=SESSION, cwd=CWD, fallback_timestamp=TS)
        self.assertEqual(migrated.corrupted, 2)
        self.assertEqual(len(migrated.records), 6)

    def test_the_v1_timestamp_falls_back_to_the_file_time(self) -> None:
        migrated = migrate(v1_fixture(), session_id="v1", cwd=CWD, fallback_timestamp=TS)
        self.assertTrue(all(entry["timestamp"] == TS for entry in migrated.records[1:]))


class CommandCodeStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / ".commandcode"
        self.projects = self.home / "projects"
        self.store = CommandCodeStore(self.home)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_lists_every_version_and_skips_sidecars_and_newer_files(self) -> None:
        write(self.projects / "c-work-demo" / f"{SESSION}.jsonl", v3_fixture())
        write(self.projects / "c-work-demo" / "a0000000-0000-4000-8000-000000000002.jsonl", v2_fixture())
        write(self.projects / "c-work-demo" / f"{SESSION}.checkpoints.jsonl", ['{"type": "checkpoint"}'])
        write(self.projects / "c-work-demo" / f"{SESSION}.prompts.jsonl", ['{"prompt": "x"}'])
        write(self.projects / "root" / "b0000000-0000-4000-8000-000000000003.jsonl", v1_fixture())
        write(self.projects / "root" / "c0000000-0000-4000-8000-000000000004.jsonl", lines({"type": "session", "version": 9, "id": "x", "timestamp": TS, "cwd": CWD}))

        summaries = {s.session_id: s for s in self.store.list()}
        self.assertEqual(set(summaries), {SESSION, "a0000000-0000-4000-8000-000000000002", "b0000000-0000-4000-8000-000000000003"})
        self.assertEqual(summaries[SESSION].cwd, CWD)
        self.assertEqual(summaries[SESSION].message_count, 5)

    def test_an_old_file_takes_the_cwd_of_its_folder(self) -> None:
        write(self.projects / "c-work-demo" / f"{SESSION}.jsonl", v3_fixture())
        write(self.projects / "c-work-demo" / "a0000000-0000-4000-8000-000000000002.jsonl", v2_fixture())
        session = self.store.load("a0000000-0000-4000-8000-000000000002")
        self.assertEqual(session.cwd, CWD)
        self.assertEqual(session.provider, "commandcode")
        self.assertEqual(session.records[0]["version"], 3)

    def test_an_old_file_in_a_folder_without_a_version_three_file_has_no_cwd(self) -> None:
        write(self.projects / "root" / "b0000000-0000-4000-8000-000000000003.jsonl", v1_fixture())
        self.assertEqual(self.store.load("b0000000-0000-4000-8000-000000000003").cwd, "")

    def test_a_unique_prefix_loads_and_an_ambiguous_one_does_not(self) -> None:
        write(self.projects / "c-work-demo" / f"{SESSION}.jsonl", v3_fixture())
        write(self.projects / "c-work-demo" / "3f2c0000-0000-4000-8000-000000000009.jsonl", v2_fixture())
        self.assertEqual(self.store.load("3f2c1bfd").session_id, SESSION)
        with self.assertRaises(FileNotFoundError):
            self.store.load("3f2c")

    def test_the_sidecar_meta_model_reaches_the_last_reply(self) -> None:
        write(self.projects / "root" / "b0000000-0000-4000-8000-000000000003.jsonl", v1_fixture())
        (self.projects / "root" / "b0000000-0000-4000-8000-000000000003.meta.json").write_text(json.dumps({"model": "deepseek/x"}), encoding="utf-8")
        session = self.store.load("b0000000-0000-4000-8000-000000000003")
        self.assertEqual(session.records[-1]["model"], "deepseek/x")


class CommandCodeExtractionTests(unittest.TestCase):
    def session(self, content: list[str]) -> NativeSession:
        migrated = migrate(content, session_id=SESSION, cwd=CWD, fallback_timestamp=TS)
        return NativeSession("commandcode", SESSION, CWD, TS, Path("s.jsonl"), migrated.records)

    def test_reads_only_the_branch_to_the_leaf(self) -> None:
        messages = MessageExtractor().from_commandcode(self.session(v3_fixture()))
        self.assertEqual([(m.role, m.text) for m in messages], [("user", "Plan the app"), ("user", "Use Python"), ("assistant", "Python it is.")])
        self.assertNotIn("abandoned reply", " ".join(m.text for m in messages))

    def test_thinking_and_tool_calls_come_with_their_results(self) -> None:
        reply = MessageExtractor().from_commandcode(self.session(v3_fixture()))[-1]
        self.assertEqual(reply.thinking, "Plan first.")
        self.assertEqual([(c.name, c.arguments, c.output) for c in reply.tool_calls], [("bash", {"command": "pwd"}, r"C:\work")])

    def test_the_v1_and_v2_files_extract_the_same_conversation(self) -> None:
        extractor = MessageExtractor()
        v2 = extractor.from_commandcode(self.session(v2_fixture()))
        self.assertEqual([(m.role, m.text) for m in v2], [("user", "Read b.txt"), ("assistant", "Reading.")])
        self.assertEqual(v2[1].thinking, "Open it.")
        self.assertEqual((v2[1].tool_calls[0].name, v2[1].tool_calls[0].output), ("read_file", "bytes"))
        v1 = extractor.from_commandcode(self.session(v1_fixture()))
        self.assertEqual([(m.role, m.text) for m in v1], [("user", "List the files"), ("assistant", "Listing."), ("assistant", "Done.")])
        self.assertEqual((v1[1].tool_calls[0].name, v1[1].tool_calls[0].output), ("bash", "a.txt"))

    def test_meta_flags_make_contextual_and_summary_messages(self) -> None:
        content = lines(
            {"type": "session", "version": 3, "id": SESSION, "timestamp": TS, "cwd": CWD},
            {"type": "message", "id": "00000001", "parentId": None, "timestamp": TS, "message": {"role": "user", "content": [{"type": "text", "text": "Injected rules"}], "meta": {"isMeta": True}}},
            {"type": "message", "id": "00000002", "parentId": "00000001", "timestamp": TS, "message": {"role": "user", "content": [{"type": "text", "text": "Summary of the earlier work"}], "meta": {"isSummary": True}}},
            {"type": "custom_message", "id": "00000003", "parentId": "00000002", "timestamp": TS, "customType": "mod", "content": "from a mod", "display": True},
        )
        messages = MessageExtractor().from_commandcode(self.session(content))
        self.assertTrue(messages[0].is_contextual)
        self.assertTrue(messages[1].is_compaction)
        self.assertTrue(messages[2].is_contextual)

    def test_a_compaction_entry_is_a_compaction_message(self) -> None:
        content = lines(
            {"type": "session", "version": 3, "id": SESSION, "timestamp": TS, "cwd": CWD},
            {"type": "compaction", "id": "00000001", "parentId": None, "timestamp": TS, "summary": "Earlier: built the store.", "firstKeptEntryId": "00000001", "tokensBefore": 900},
            {"type": "message", "id": "00000002", "parentId": "00000001", "timestamp": TS, "message": {"role": "user", "content": [{"type": "text", "text": "Next step"}]}},
        )
        messages = MessageExtractor().from_commandcode(self.session(content))
        self.assertEqual([(m.text, m.is_compaction) for m in messages], [("Earlier: built the store.", True), ("Next step", False)])


class CommandCodeWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / ".commandcode"
        self.store = CommandCodeStore(self.home)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def messages(self) -> list[TextMessage]:
        call = ToolCall(id="call-1", name="bash", arguments={"command": "ls"}, output="a.txt", is_error=False)
        return [
            TextMessage("user", "List the files", TS),
            TextMessage("assistant", "Listing.", "2026-06-02T09:00:05.000Z", thinking="Check first.", tool_calls=(call,), model="deepseek/x"),
            TextMessage("user", "Summary so far", "2026-06-02T09:00:06.000Z", is_compaction=True),
            TextMessage("user", "Injected", "2026-06-02T09:00:07.000Z", is_contextual=True),
            TextMessage("assistant", "Done.", "2026-06-02T09:00:08.000Z"),
        ]

    def test_builds_version_three_records_in_the_cli_shape(self) -> None:
        records = CommandCodeRecordBuilder().build(SESSION, CWD, TS, self.messages())
        self.assertEqual(records[0], {"type": "session", "version": 3, "id": SESSION, "timestamp": TS, "cwd": CWD})
        kinds = [r["type"] for r in records[1:]]
        self.assertEqual(kinds, ["session_info", "model_change", "message", "message", "message", "compaction", "message"])
        reply = records[4]
        self.assertEqual([b["type"] for b in reply["message"]["content"]], ["thinking", "text", "tool_use"])
        self.assertEqual(records[5]["message"]["content"][0], {"type": "tool_result", "tool_use_id": "call-1", "content": [{"type": "text", "text": "a.txt"}]})
        self.assertEqual(records[1]["parentId"], None)
        self.assertEqual(records[2]["parentId"], records[1]["id"])
        self.assertEqual(records[3]["parentId"], records[2]["id"])
        self.assertEqual(records[5]["parentId"], reply["id"])

    def test_written_session_reads_back_with_the_same_conversation(self) -> None:
        records = CommandCodeRecordBuilder().build(SESSION, CWD, TS, self.messages())
        destination = self.store.destination_path(SESSION, TS, CWD)
        self.assertEqual(destination.parent.name, encode_commandcode_cwd(CWD))
        self.store.write(destination, records)
        messages = MessageExtractor().from_commandcode(self.store.load(SESSION))
        self.assertEqual([(m.role, m.text) for m in messages if not m.is_contextual], [
            ("user", "List the files"), ("assistant", "Listing."), ("user", "Summary so far"), ("assistant", "Done."),
        ])
        self.assertEqual(messages[1].tool_calls[0].output, "a.txt")
        self.assertEqual([s.session_id for s in self.store.list()], [SESSION])

    def test_a_title_comes_from_the_first_user_line(self) -> None:
        records = CommandCodeRecordBuilder().build(SESSION, CWD, TS, self.messages())
        self.assertEqual(next(r for r in records if r["type"] == "session_info")["name"], "List the files")


class CommandCodeConversionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = CommandCodeStore(self.root / ".commandcode")
        write(self.root / ".commandcode" / "projects" / "c-work-demo" / f"{SESSION}.jsonl", v3_fixture())
        self.ids = SessionIdFactory(preserve_ids=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_commandcode_to_pi_round_trip_keeps_the_conversation(self) -> None:
        pi = PiStore(self.root / ".pi" / "agent")
        converter = CommandCodeToPiConverter(self.store, pi, PiDcpStore(self.root / ".pi-dcp"), self.ids)
        plan = converter.plan(SESSION)
        self.assertEqual(plan.source.provider, "commandcode")
        converter.write(plan)
        back = MessageExtractor().from_session(pi.load(SESSION))
        self.assertEqual([(m.role, m.text) for m in back if not m.is_contextual][:3], [("user", "Plan the app"), ("user", "Use Python"), ("assistant", "Python it is.")])
        self.assertEqual(back[-1].tool_calls[0].output, r"C:\work")

    def test_other_providers_convert_into_commandcode_and_back(self) -> None:
        pi = PiStore(self.root / ".pi" / "agent")
        to_pi = CommandCodeToPiConverter(self.store, pi, PiDcpStore(self.root / ".pi-dcp"), self.ids)
        to_pi.write(to_pi.plan(SESSION))
        back_store = CommandCodeStore(self.root / "back" / ".commandcode")
        converter = ToCommandCodeConverter(pi, back_store, self.ids)
        plan = converter.plan(SESSION)
        self.assertFalse(plan.destination.exists())
        converter.write(plan)
        self.assertTrue(plan.destination.exists())
        self.assertFalse(converter.has_changes(SESSION))
        reread = MessageExtractor().from_commandcode(back_store.load(SESSION))
        self.assertEqual(reread[-1].tool_calls[0].output, r"C:\work")


class CommandCodeCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home = self.root / ".commandcode"
        write(self.home / "projects" / "c-work-demo" / f"{SESSION}.jsonl", v3_fixture())
        self.base = ["--commandcode-home", str(self.home), "--pi-agent-home", str(self.root / ".pi" / "agent"), "--pi-dcp-home", str(self.root / ".pi-dcp")]

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_cli(self, *args: str) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            status = main([*self.base, *args])
        return status, output.getvalue()

    def test_list_shows_the_session(self) -> None:
        status, out = self.run_cli("list", "commandcode")
        self.assertEqual(status, 0)
        self.assertIn(SESSION, out)

    def test_a_dry_run_writes_nothing_and_write_creates_the_pi_session(self) -> None:
        status, out = self.run_cli("commandcode-to-pi", SESSION)
        self.assertEqual(status, 0)
        self.assertIn("destination:", out)
        self.assertFalse((self.root / ".pi").exists())
        self.assertEqual(self.run_cli("commandcode-to-pi", SESSION, "--write")[0], 0)
        self.assertTrue(PiStore(self.root / ".pi" / "agent").load(SESSION))

    def test_every_commandcode_direction_is_a_command(self) -> None:
        from unisessions.cli import CliApp
        parser = CliApp._parser()
        for name in ("commandcode-to-pi", "pi-to-commandcode", "commandcode-to-omp", "omp-to-commandcode", "commandcode-to-t3", "t3-to-commandcode"):
            with self.subTest(command=name):
                self.assertEqual(parser.parse_args([name, SESSION]).command, name)


class CommandCodePathTests(unittest.TestCase):
    def test_project_folder_is_the_lowercase_cwd_with_hyphens(self) -> None:
        self.assertEqual(encode_commandcode_cwd(r"C:\Users\win"), "c-users-win")
        self.assertEqual(encode_commandcode_cwd(r"C:\Users\win\Desktop\automate-sdk"), "c-users-win-desktop-automate-sdk")
        self.assertEqual(encode_commandcode_cwd(r"C:\\a//b"), "c-a-b")
        self.assertEqual(encode_commandcode_cwd(""), "root")


if __name__ == "__main__":
    unittest.main()
