from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from session_sdk.converters import (
    CodexRecordBuilder,
    CodexToOpenCodeConverter,
    CodexToPiConverter,
    DevinToPiConverter,
    MessageExtractor,
    OpenCodeToCodexConverter,
    OpenCodeToPiConverter,
    PiToCodexConverter,
    PiToOpenCodeConverter,
)
from session_sdk.jsonl import JsonlFile
from session_sdk.models import NativeSession, TextMessage
from session_sdk.paths import SessionIdFactory, encode_pi_cwd
from session_sdk.search import SessionSearchEngine
from session_sdk.stores import CodexStore, DevinStore, OpenCodeStore, PiDcpStore, PiStore
from session_sdk.traces import OpenAITraceBuilder, ShareGPTTraceBuilder, STSTraceBuilder, build_trace


class PathTests(unittest.TestCase):
    def test_encode_windows_cwd(self) -> None:
        self.assertEqual(encode_pi_cwd(r"C:\home\user"), "--C--home-user--")
        self.assertEqual(encode_pi_cwd(r"C:\projects\repo"), "--C--projects-repo--")


class ConversionTests(unittest.TestCase):
    def _write_codex_fixture(self, codex_home: Path, session_id: str) -> Path:
        source = codex_home / "sessions" / "2026" / "06" / "10" / f"rollout-2026-06-10T23-22-58-{session_id}.jsonl"
        JsonlFile(source).write(
            [
                {
                    "timestamp": "2026-06-10T23:22:58.000Z",
                    "type": "session_meta",
                    "payload": {
                        "id": session_id,
                        "timestamp": "2026-06-10T23:22:58.000Z",
                        "cwd": r"C:\projects\test-app",
                    },
                },
                {
                    "timestamp": "2026-06-10T23:23:00.000Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "hello"}],
                    },
                },
            ]
        )
        return source

    def _write_pi_fixture(self, pi_agent_home: Path, session_id: str) -> Path:
        source = pi_agent_home / "sessions" / "--C--home-user--" / f"2026-06-28T04-20-31-629Z_{session_id}.jsonl"
        JsonlFile(source).write(
            [
                {
                    "type": "session",
                    "version": 3,
                    "id": session_id,
                    "timestamp": "2026-06-28T04:20:31.629Z",
                    "cwd": r"C:\home\user",
                },
                {
                    "type": "message",
                    "id": "abcd1234",
                    "parentId": session_id,
                    "timestamp": "2026-06-28T04:20:32.000Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "hello"}],
                    },
                },
            ]
        )
        return source

    def _write_opencode_fixture(self, opencode_dir: Path, session_id: str) -> Path:
        source = opencode_dir / f"{session_id}.json"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(
            """{
  "info": {
    "id": "ses_9d9ddbe00001aaaaaaaaaaaaaa",
    "slug": "hello",
    "projectID": "global",
    "directory": "C:\\\\Users\\\\win",
    "title": "hello",
    "version": "session-export",
    "time": {"created": 1782601231629, "updated": 1782601232000}
  },
  "messages": [
    {
      "info": {
        "id": "msg_9d9ddbe00001bbbbbbbbbbbbbb",
        "sessionID": "ses_9d9ddbe00001aaaaaaaaaaaaaa",
        "role": "user",
        "time": {"created": 1782601231629},
        "agent": "session-export",
        "model": {"providerID": "session-export", "modelID": "imported"}
      },
      "parts": [
        {
          "id": "prt_9d9ddbe00001cccccccccccccc",
          "sessionID": "ses_9d9ddbe00001aaaaaaaaaaaaaa",
          "messageID": "msg_9d9ddbe00001bbbbbbbbbbbbbb",
          "type": "text",
          "text": "hello"
        }
      ]
    }
  ]
}
""",
            encoding="utf-8",
        )
        return source

    def test_codex_to_pi_plan(self) -> None:
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            codex_home = root / ".codex"
            pi_agent_home = root / ".pi" / "agent"
            pi_dcp_home = root / ".pi-dcp"
            session_id = "01234567-89ab-cdef-0123-456789abcdef"
            self._write_codex_fixture(codex_home, session_id)

            plan = CodexToPiConverter(
                CodexStore(codex_home),
                PiStore(pi_agent_home),
                PiDcpStore(pi_dcp_home),
                SessionIdFactory(),
            ).plan(session_id)

            self.assertEqual(plan.destination.parent.name, "--C--projects-test-app--")
            self.assertEqual(len(plan.records), 2)
            self.assertEqual(plan.records[0]["type"], "session")
            self.assertEqual(plan.records[1]["type"], "message")

    def test_codex_to_pi_assistant_usage_is_estimated(self) -> None:
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            codex_home = root / ".codex"
            pi_agent_home = root / ".pi" / "agent"
            pi_dcp_home = root / ".pi-dcp"
            session_id = "01234567-89ab-cdef-0123-456789abcdef"
            source = codex_home / "sessions" / "2026" / "06" / "10" / f"rollout-2026-06-10T23-22-58-{session_id}.jsonl"
            JsonlFile(source).write(
                [
                    {
                        "timestamp": "2026-06-10T23:22:58.000Z",
                        "type": "session_meta",
                        "payload": {
                            "id": session_id,
                            "timestamp": "2026-06-10T23:22:58.000Z",
                            "cwd": r"C:\home\user",
                        },
                    },
                    {
                        "timestamp": "2026-06-10T23:23:00.000Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "hello"}],
                        },
                    },
                    {
                        "timestamp": "2026-06-10T23:23:01.000Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "hi there"}],
                        },
                    },
                ]
            )

            plan = CodexToPiConverter(
                CodexStore(codex_home),
                PiStore(pi_agent_home),
                PiDcpStore(pi_dcp_home),
                SessionIdFactory(),
            ).plan(session_id)

            usage = plan.records[2]["message"]["usage"]  # type: ignore[index]
            self.assertGreater(usage["input"], 0)  # type: ignore[index]
            self.assertGreater(usage["output"], 0)  # type: ignore[index]
            self.assertEqual(usage["totalTokens"], usage["input"] + usage["output"])  # type: ignore[index,operator]

    def test_pi_to_codex_plan(self) -> None:
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            codex_home = root / ".codex"
            pi_agent_home = root / ".pi" / "agent"
            session_id = "019f0c75-250d-7a48-ad78-92c861c3c49e"
            self._write_pi_fixture(pi_agent_home, session_id)

            plan = PiToCodexConverter(
                PiStore(pi_agent_home),
                CodexStore(codex_home),
                SessionIdFactory(),
            ).plan(session_id)

            self.assertIn("rollout-2026-06-28T04-20-31-629", plan.destination.name)
            self.assertEqual(len(plan.records), 2)
            self.assertEqual(plan.records[0]["type"], "session_meta")
            self.assertEqual(plan.records[1]["type"], "response_item")

    def test_codex_to_opencode_plan_uses_export_shape(self) -> None:
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            codex_home = root / ".codex"
            opencode_home = root / "opencode"
            session_id = "01234567-89ab-cdef-0123-456789abcdef"
            self._write_codex_fixture(codex_home, session_id)

            plan = CodexToOpenCodeConverter(
                CodexStore(codex_home),
                OpenCodeStore(opencode_home),
                SessionIdFactory(),
            ).plan(session_id)

            export = plan.records[0]
            self.assertTrue(str(export["info"]["id"]).startswith("ses_"))  # type: ignore[index]
            self.assertEqual(plan.destination.parent, opencode_home / "session-export")
            self.assertEqual(export["messages"][0]["parts"][0]["text"], "hello")  # type: ignore[index]

    def test_pi_to_opencode_plan_uses_export_shape(self) -> None:
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            pi_agent_home = root / ".pi" / "agent"
            opencode_home = root / "opencode"
            session_id = "019f0c75-250d-7a48-ad78-92c861c3c49e"
            self._write_pi_fixture(pi_agent_home, session_id)

            plan = PiToOpenCodeConverter(
                PiStore(pi_agent_home),
                OpenCodeStore(opencode_home),
                SessionIdFactory(),
            ).plan(session_id)

            export = plan.records[0]
            self.assertTrue(str(export["info"]["id"]).startswith("ses_"))  # type: ignore[index]
            self.assertEqual(export["messages"][0]["info"]["role"], "assistant")  # type: ignore[index]

    def test_opencode_to_codex_plan_generates_uuid(self) -> None:
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            codex_home = root / ".codex"
            opencode_dir = root / "exports"
            session_id = "ses_9d9ddbe00001aaaaaaaaaaaaaa"
            self._write_opencode_fixture(opencode_dir, session_id)

            plan = OpenCodeToCodexConverter(
                OpenCodeStore(root / "opencode", opencode_dir),
                CodexStore(codex_home),
                SessionIdFactory(),
            ).plan(session_id)

            self.assertIn("rollout-2026-06-27", plan.destination.name)
            self.assertEqual(plan.records[0]["type"], "session_meta")
            self.assertEqual(plan.records[1]["payload"]["role"], "user")  # type: ignore[index]

    def test_opencode_to_pi_plan_writes_dcp_service(self) -> None:
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            pi_agent_home = root / ".pi" / "agent"
            pi_dcp_home = root / ".pi-dcp"
            opencode_dir = root / "exports"
            session_id = "ses_9d9ddbe00001aaaaaaaaaaaaaa"
            self._write_opencode_fixture(opencode_dir, session_id)

            plan = OpenCodeToPiConverter(
                OpenCodeStore(root / "opencode", opencode_dir),
                PiStore(pi_agent_home),
                PiDcpStore(pi_dcp_home),
                SessionIdFactory(),
            ).plan(session_id)

            self.assertEqual(plan.records[0]["type"], "session")
            self.assertEqual(len(plan.services), 1)
            self.assertEqual(plan.services[0].parent, pi_dcp_home / "sessions")

    def test_custom_session_directories_are_used(self) -> None:
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            codex_session_dir = root / "codex-sessions"
            pi_session_dir = root / "pi-sessions"
            opencode_session_dir = root / "opencode-exports"
            session_id = "01234567-89ab-cdef-0123-456789abcdef"
            source = codex_session_dir / "2026" / "06" / "10" / f"rollout-2026-06-10T23-22-58-{session_id}.jsonl"
            JsonlFile(source).write(
                [
                    {
                        "timestamp": "2026-06-10T23:22:58.000Z",
                        "type": "session_meta",
                        "payload": {
                            "id": session_id,
                            "timestamp": "2026-06-10T23:22:58.000Z",
                            "cwd": r"C:\home\user",
                        },
                    }
                ]
            )

            codex = CodexStore(root / ".codex", codex_session_dir)
            pi = PiStore(root / ".pi" / "agent", pi_session_dir)
            opencode = OpenCodeStore(root / "opencode", opencode_session_dir)

            self.assertEqual(len(codex.list()), 1)
            self.assertEqual(pi.destination_path(session_id, "2026-06-10T23:22:58.000Z", r"C:\home\user").parents[1], pi_session_dir)
            self.assertEqual(opencode.destination_path("ses_test").parent, opencode_session_dir)


class CompactionTests(unittest.TestCase):
    """Tests for compaction marker extraction and emission across formats."""

    # -- Fixtures with compaction markers --

    _CODEX_COMPACTION_RECORDS = [
        {"timestamp": "2026-01-01T00:00:00.000Z", "type": "session_meta", "payload": {"id": "test-id", "timestamp": "2026-01-01T00:00:00.000Z", "cwd": r"C:\test"}},
        {"timestamp": "2026-01-01T00:00:01.000Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "old message"}]}},
        {"timestamp": "2026-01-01T00:00:02.000Z", "type": "compacted", "payload": {"message": "Compaction summary text", "replacement_history": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "replaced user msg"}]},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "replaced assistant msg"}]},
        ]}},
        {"timestamp": "2026-01-01T00:00:03.000Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "post-compaction msg"}]}},
    ]

    _PI_COMPACTION_RECORDS = [
        {"type": "session", "version": 3, "id": "test-id", "timestamp": "2026-01-01T00:00:00.000Z", "cwd": r"C:\test"},
        {"type": "message", "id": "abc12345", "parentId": None, "timestamp": "2026-01-01T00:00:01.000Z", "message": {"role": "user", "content": [{"type": "text", "text": "old msg"}], "timestamp": 1700000001000}},
        {"type": "compaction", "id": "def67890", "parentId": "abc12345", "timestamp": "2026-01-01T00:00:02.000Z", "summary": "Compaction summary", "firstKeptEntryId": "ghi13579", "tokensBefore": 1000, "details": {}, "fromHook": True},
        {"type": "message", "id": "ghi13579", "parentId": "def67890", "timestamp": "2026-01-01T00:00:03.000Z", "message": {"role": "user", "content": [{"type": "text", "text": "new msg"}], "timestamp": 1700000003000}},
    ]

    # -- Helpers --

    @staticmethod
    def _write_codex_compaction(codex_home: Path) -> str:
        session_id = "test-id"
        source = codex_home / "sessions" / "2026" / "01" / "01" / f"rollout-2026-01-01T00-00-00-{session_id}.jsonl"
        JsonlFile(source).write(CompactionTests._CODEX_COMPACTION_RECORDS)
        return session_id

    @staticmethod
    def _write_pi_compaction(pi_agent_home: Path) -> str:
        session_id = "test-id"
        source = pi_agent_home / "sessions" / "--C--test--" / f"2026-01-01T00-00-00-000Z_{session_id}.jsonl"
        JsonlFile(source).write(CompactionTests._PI_COMPACTION_RECORDS)
        return session_id

    @staticmethod
    def _write_opencode_compaction(opencode_dir: Path) -> str:
        session_id = "ses_testcompaction0000000000000001"
        source = opencode_dir / f"{session_id}.json"
        source.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "info": {
                "id": session_id,
                "slug": "compaction-test",
                "projectID": "global",
                "directory": r"C:\test",
                "title": "compaction test",
                "version": "session-export",
                "time": {"created": 1700000000000, "updated": 1700000003000},
            },
            "messages": [
                {
                    "info": {
                        "id": "msg_pre00000000000000000000000001",
                        "sessionID": session_id,
                        "role": "user",
                        "time": {"created": 1700000000000},
                        "agent": "session-export",
                        "model": {"providerID": "session-export", "modelID": "imported"},
                    },
                    "parts": [
                        {"id": "prt_pre00000000000000000000000001", "sessionID": session_id, "messageID": "msg_pre00000000000000000000000001", "type": "text", "text": "old msg"}
                    ],
                },
                {
                    "info": {
                        "id": "msg_comp0000000000000000000000002",
                        "sessionID": session_id,
                        "role": "user",
                        "time": {"created": 1700000001000},
                        "agent": "session-export",
                        "model": {"providerID": "session-export", "modelID": "imported"},
                    },
                    "parts": [
                        {"id": "prt_comp0000000000000000000000002", "sessionID": session_id, "messageID": "msg_comp0000000000000000000000002", "type": "compaction", "auto": True}
                    ],
                },
                {
                    "info": {
                        "id": "msg_sum00000000000000000000000003",
                        "sessionID": session_id,
                        "role": "assistant",
                        "time": {"created": 1700000001000, "completed": 1700000001000},
                        "parentID": "msg_comp0000000000000000000000002",
                        "modelID": "imported",
                        "providerID": "session-export",
                        "mode": "build",
                        "agent": "session-export",
                        "path": {"cwd": r"C:\test", "root": r"C:\test"},
                        "cost": 0,
                        "summary": True,
                        "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}},
                    },
                    "parts": [
                        {"id": "prt_sum00000000000000000000000003", "sessionID": session_id, "messageID": "msg_sum00000000000000000000000003", "type": "text", "text": "Compaction summary", "time": {"start": 1700000001000, "end": 1700000001000}}
                    ],
                },
                {
                    "info": {
                        "id": "msg_post0000000000000000000000004",
                        "sessionID": session_id,
                        "role": "user",
                        "time": {"created": 1700000002000},
                        "agent": "session-export",
                        "model": {"providerID": "session-export", "modelID": "imported"},
                    },
                    "parts": [
                        {"id": "prt_post0000000000000000000000004", "sessionID": session_id, "messageID": "msg_post0000000000000000000000004", "type": "text", "text": "new msg"}
                    ],
                },
            ],
        }
        source.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return session_id

    # -- Extraction tests --

    def test_codex_compaction_extracted(self) -> None:
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            codex_home = root / ".codex"
            session_id = self._write_codex_compaction(codex_home)
            session = CodexStore(codex_home).load(session_id)
            messages = MessageExtractor().from_codex(session)

            self.assertEqual(len(messages), 5)
            self.assertTrue(messages[1].is_compaction)
            self.assertEqual(messages[1].role, "user")
            self.assertEqual(messages[1].text, "Compaction summary text")
            self.assertEqual(messages[2].text, "replaced user msg")
            self.assertEqual(messages[4].text, "post-compaction msg")

    def test_pi_compaction_extracted(self) -> None:
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            pi_agent_home = root / ".pi" / "agent"
            session_id = self._write_pi_compaction(pi_agent_home)
            session = PiStore(pi_agent_home).load(session_id)
            messages = MessageExtractor().from_pi(session)

            self.assertEqual(len(messages), 3)
            self.assertTrue(messages[1].is_compaction)
            self.assertEqual(messages[1].text, "Compaction summary")
            self.assertEqual(messages[2].text, "new msg")

    def test_opencode_compaction_extracted(self) -> None:
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            opencode_dir = root / "exports"
            session_id = self._write_opencode_compaction(opencode_dir)
            store = OpenCodeStore(root / "opencode", opencode_dir)
            session = store.load(session_id)
            messages = MessageExtractor().from_opencode(session)

            self.assertEqual(len(messages), 3)
            self.assertTrue(messages[1].is_compaction)
            self.assertEqual(messages[1].text, "Compaction summary")
            self.assertEqual(messages[2].text, "new msg")

    # -- Conversion / emission tests --

    def test_codex_to_pi_compaction_emitted(self) -> None:
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            codex_home = root / ".codex"
            pi_agent_home = root / ".pi" / "agent"
            pi_dcp_home = root / ".pi-dcp"
            session_id = self._write_codex_compaction(codex_home)

            plan = CodexToPiConverter(
                CodexStore(codex_home),
                PiStore(pi_agent_home),
                PiDcpStore(pi_dcp_home),
                SessionIdFactory(),
            ).plan(session_id)

            compaction_records = [r for r in plan.records if r.get("type") == "compaction"]
            self.assertEqual(len(compaction_records), 1)
            comp = compaction_records[0]
            self.assertEqual(comp["summary"], "Compaction summary text")
            self.assertTrue(comp["fromHook"])
            first_kept = comp["firstKeptEntryId"]
            all_ids = {r.get("id") for r in plan.records if r.get("id")}
            self.assertIn(first_kept, all_ids)

    def test_codex_to_codex_compaction_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            codex_home = root / ".codex"
            session_id = self._write_codex_compaction(codex_home)
            session = CodexStore(codex_home).load(session_id)
            messages = MessageExtractor().from_codex(session)
            records = CodexRecordBuilder().build(session_id, session.cwd, session.timestamp, messages)

            compacted = [r for r in records if r.get("type") == "compacted"]
            self.assertEqual(len(compacted), 1)
            self.assertEqual(compacted[0]["payload"]["message"], "Compaction summary text")

    def test_pi_to_codex_compaction_emitted(self) -> None:
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            codex_home = root / ".codex"
            pi_agent_home = root / ".pi" / "agent"
            session_id = self._write_pi_compaction(pi_agent_home)

            plan = PiToCodexConverter(
                PiStore(pi_agent_home),
                CodexStore(codex_home),
                SessionIdFactory(),
            ).plan(session_id)

            compacted = [r for r in plan.records if r.get("type") == "compacted"]
            self.assertEqual(len(compacted), 1)
            self.assertEqual(compacted[0]["payload"]["message"], "Compaction summary")


class ClaudeTests(unittest.TestCase):
    def test_claude_session_extracted_and_converted_to_pi(self) -> None:
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            claude_home = root / ".claude"
            projects_dir = claude_home / "projects" / "C--projects-test"
            projects_dir.mkdir(parents=True, exist_ok=True)
            session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            session_file = projects_dir / f"{session_id}.jsonl"
            lines = [
                '{"type":"permission-mode","permissionMode":"bypassPermissions","sessionId":"' + session_id + '"}',
                '{"parentUuid":null,"isSidechain":false,"type":"user","message":{"role":"user","content":"hello world"},"uuid":"aaa-111","timestamp":"2026-05-23T03:12:51.338Z","cwd":"C:\\\\Projects\\\\test","sessionId":"' + session_id + '","version":"2.1.140","userType":"external","entrypoint":"cli"}',
                '{"parentUuid":"aaa-111","isSidechain":false,"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"hi there"}],"model":"claude-opus-4-7","usage":{"input_tokens":100,"output_tokens":50},"stop_reason":"end_turn"},"uuid":"bbb-222","timestamp":"2026-05-23T03:12:52.000Z","cwd":"C:\\\\Projects\\\\test","sessionId":"' + session_id + '","version":"2.1.140","userType":"external","entrypoint":"cli"}',
                '{"parentUuid":null,"logicalParentUuid":"bbb-222","isSidechain":false,"type":"system","subtype":"compact_boundary","content":"Conversation compacted","uuid":"ccc-333","timestamp":"2026-05-23T03:13:00.000Z","level":"info","compactMetadata":{"trigger":"manual","preTokens":500},"cwd":"C:\\\\Projects\\\\test","sessionId":"' + session_id + '","version":"2.1.140","userType":"external","entrypoint":"cli"}',
                '{"parentUuid":"ccc-333","isSidechain":false,"type":"user","isCompactSummary":true,"message":{"role":"user","content":"This session is being continued..."},"uuid":"ddd-444","timestamp":"2026-05-23T03:13:01.000Z","cwd":"C:\\\\Projects\\\\test","sessionId":"' + session_id + '","version":"2.1.140","userType":"external","entrypoint":"cli"}',
            ]
            session_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

            from session_sdk.stores import ClaudeStore
            from session_sdk.converters import ClaudeToPiConverter, MessageExtractor

            claude_store = ClaudeStore(claude_home)
            session = claude_store.load(session_id)
            self.assertEqual(session.session_id, session_id)

            extractor = MessageExtractor()
            messages = extractor.from_claude(session)
            self.assertEqual(len(messages), 4)
            self.assertEqual(messages[0].role, "user")
            self.assertIn("hello world", messages[0].text)
            self.assertEqual(messages[1].role, "assistant")
            self.assertIn("hi there", messages[1].text)
            self.assertEqual(messages[1].model, "claude-opus-4-7")
            self.assertTrue(messages[2].is_compaction)
            self.assertTrue(messages[3].is_compaction)

            pi_store = PiStore(root / ".pi" / "agent")
            dcp_store = PiDcpStore(root / ".pi-dcp")
            factory = SessionIdFactory(preserve_ids=True)
            converter = ClaudeToPiConverter(claude_store, pi_store, dcp_store, factory)
            plan = converter.plan(session_id)
            self.assertTrue(plan.destination.name.endswith(f"{session_id}.jsonl"))
            self.assertGreater(len(plan.records), 4)
            self.assertEqual(plan.records[0].get("type"), "session")


class SearchTests(unittest.TestCase):
    def test_list_chats_filters_by_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            codex_home = root / ".codex"
            session_id = "01234567-89ab-cdef-0123-456789abcdef"
            ConversionTests()._write_codex_fixture(codex_home, session_id)

            engine = SessionSearchEngine(
                CodexStore(codex_home),
                PiStore(root / ".pi" / "agent"),
                OpenCodeStore(root / "opencode"),
            )

            chats = engine.list_chats(provider="codex", cwd="test-app", cwd_match="contains")
            self.assertEqual(len(chats), 1)
            self.assertEqual(chats[0]["session_id"], session_id)

    def test_search_supports_regex_keywords_roles_and_contextual_exclusion(self) -> None:
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            codex_home = root / ".codex"
            session_id = "01234567-89ab-cdef-0123-456789abcdef"
            source = ConversionTests()._write_codex_fixture(codex_home, session_id)
            JsonlFile(source).write(
                [
                    {
                        "timestamp": "2026-06-10T23:22:58.000Z",
                        "type": "session_meta",
                        "payload": {
                            "id": session_id,
                            "timestamp": "2026-06-10T23:22:58.000Z",
                            "cwd": r"C:\projects\test-app",
                        },
                    },
                    {
                        "timestamp": "2026-06-10T23:23:00.000Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "please remember test billing notes"}],
                        },
                    },
                    {
                        "timestamp": "2026-06-10T23:24:00.000Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "Test billing is in context"}],
                        },
                    },
                    {
                        "timestamp": "2026-06-10T23:25:00.000Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "# AGENTS.md instructions for C:/repo\nsecret marker"}],
                        },
                    },
                ],
                overwrite=True,
            )
            engine = SessionSearchEngine(
                CodexStore(codex_home),
                PiStore(root / ".pi" / "agent"),
                OpenCodeStore(root / "opencode"),
            )

            engine.refresh_index(provider="codex")

            user_resp = engine.search(provider="codex", query="test", roles=["user"])
            user_hits = user_resp["results"]
            self.assertEqual(len(user_hits), 1)
            self.assertEqual(user_hits[0]["role"], "user")
            self.assertIn("relevance_score", user_hits[0])
            self.assertIn("rank", user_hits[0])

            kw_resp = engine.search(provider="codex", keywords=["test", "billing"], mode="all_keywords")
            self.assertEqual(len(kw_resp["results"]), 2)

            regex_resp = engine.search(provider="codex", regex="test.*notes")
            self.assertEqual(len(regex_resp["results"]), 1)

            hidden = engine.search(provider="codex", query="secret marker")
            self.assertEqual(hidden["results"], [])
            visible = engine.search(provider="codex", query="secret marker", include_contextual=True)
            self.assertEqual(len(visible["results"]), 1)
            self.assertEqual(visible["results"][0]["message_type"], "contextual")

            excluded = engine.search(provider="codex", keywords=["test"], exclude_keywords=["billing"])
            self.assertTrue(all("billing" not in r["snippet"].lower() for r in excluded["results"]))

            session_results = engine.search_sessions(provider="codex", query="test")
            self.assertGreater(len(session_results), 0)
            self.assertIn("match_count", session_results[0])
            self.assertIn("top_snippets", session_results[0])

    def test_search_dedup_and_per_session_cap_edge_cases(self) -> None:
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            codex_home = root / ".codex"
            session_id_a = "01234567-89ab-cdef-0123-456789abcdef"
            session_id_b = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            for sid in (session_id_a, session_id_b):
                source = codex_home / "sessions" / "2026" / "06" / "10" / f"rollout-2026-06-10T23-22-58-{sid}.jsonl"
                source.parent.mkdir(parents=True, exist_ok=True)
                JsonlFile(source).write(
                    [
                        {"timestamp": "2026-06-10T23:22:58.000Z", "type": "session_meta", "payload": {"id": sid, "timestamp": "2026-06-10T23:22:58.000Z", "cwd": r"C:\projects\test-app"}},
                        {"timestamp": "2026-06-10T23:23:00.000Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "unique message alpha"}]}},
                        {"timestamp": "2026-06-10T23:24:00.000Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "shared duplicate message"}]}},
                    ],
                )

            engine = SessionSearchEngine(
                CodexStore(codex_home),
                PiStore(root / ".pi" / "agent"),
                OpenCodeStore(root / "opencode"),
            )
            engine.refresh_index(provider="codex")

            resp = engine.search(provider="codex", query="shared duplicate", max_per_session=1)
            snippets = [r["snippet"] for r in resp["results"]]
            self.assertEqual(len(snippets), 1)
            self.assertIn("shared duplicate", snippets[0])

            resp_all = engine.search(provider="codex", query="shared duplicate", max_per_session=0)
            self.assertEqual(len(resp_all["results"]), 1)
            self.assertGreaterEqual(resp_all["results"][0]["duplicate_count"], 2)

            sessions = engine.search_sessions(provider="codex", query="shared duplicate")
            for s in sessions:
                self.assertEqual(s["match_count"], 1)

    def test_regex_searches_full_index_not_recent_subset(self) -> None:
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            codex_home = root / ".codex"
            session_id = "01234567-89ab-cdef-0123-456789abcdef"
            source = codex_home / "sessions" / "2026" / "06" / "10" / f"rollout-2026-06-10T23-22-58-{session_id}.jsonl"
            source.parent.mkdir(parents=True, exist_ok=True)
            records = [
                {"timestamp": "2026-06-10T23:22:58.000Z", "type": "session_meta", "payload": {"id": session_id, "timestamp": "2026-06-10T23:22:58.000Z", "cwd": r"C:\projects\test-app"}},
            ]
            for i in range(300):
                records.append({
                    "timestamp": f"2026-06-10T23:{i:02d}:00.000Z",
                    "type": "response_item",
                    "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": f"filler message number {i} to push down older messages"}]},
                })
            records.append({
                "timestamp": "2026-06-10T22:00:00.000Z",
                "type": "response_item",
                "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "I hate this way too much info"}]},
            })
            JsonlFile(source).write(records)

            engine = SessionSearchEngine(
                CodexStore(codex_home),
                PiStore(root / ".pi" / "agent"),
                OpenCodeStore(root / "opencode"),
            )
            engine.refresh_index(provider="codex")

            kw_resp = engine.search(provider="codex", keywords=["hate"], roles=["user"])
            self.assertGreater(len(kw_resp["results"]), 0)

            rx_resp = engine.search(provider="codex", regex="hate", roles=["user"])
            self.assertGreater(len(rx_resp["results"]), 0, "regex mode must search full index, not just recent messages")
            self.assertIn("hate", rx_resp["results"][0]["snippet"].lower())

    def test_keyword_search_handles_contractions(self) -> None:
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            codex_home = root / ".codex"
            session_id = "01234567-89ab-cdef-0123-456789abcdef"
            source = codex_home / "sessions" / "2026" / "06" / "10" / f"rollout-2026-06-10T23-22-58-{session_id}.jsonl"
            source.parent.mkdir(parents=True, exist_ok=True)
            JsonlFile(source).write([
                {"timestamp": "2026-06-10T23:22:58.000Z", "type": "session_meta", "payload": {"id": session_id, "timestamp": "2026-06-10T23:22:58.000Z", "cwd": r"C:\projects\test-app"}},
                {"timestamp": "2026-06-10T23:23:00.000Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Things I don't like in my frontend"}]}},
                {"timestamp": "2026-06-10T23:24:00.000Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Update the frontend and repo readme like new routes"}]}},
            ])
            engine = SessionSearchEngine(
                CodexStore(codex_home),
                PiStore(root / ".pi" / "agent"),
                OpenCodeStore(root / "opencode"),
            )
            engine.refresh_index(provider="codex")

            raw = engine.raw_search_rows(provider="codex", keywords=["frontend", "dont like"], mode="all_keywords", roles=["user"])
            self.assertTrue(any("don't like" in row["text"].lower() for row in raw))

            for phrase in ("dont like", "don't like"):
                resp = engine.search(provider="codex", keywords=["frontend", phrase], mode="all_keywords", roles=["user"])
                self.assertGreater(len(resp["results"]), 0)
                self.assertIn("don't like", resp["results"][0]["snippet"].lower())

    def test_search_index_persists_and_refreshes_by_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            codex_home = root / ".codex"
            session_id = "01234567-89ab-cdef-0123-456789abcdef"
            ConversionTests()._write_codex_fixture(codex_home, session_id)
            index_path = root / "search.sqlite"

            first = SessionSearchEngine(
                CodexStore(codex_home),
                PiStore(root / ".pi" / "agent"),
                OpenCodeStore(root / "opencode"),
                index_path,
            )
            try:
                status = first.index_status(provider="codex")
                self.assertEqual(status["total_refresh_sessions"], 1)
                self.assertEqual(first.search(provider="codex", query="hello", roles=["user"], stale_policy="skip")["results"], [])
                with self.assertRaises(ValueError):
                    first.search(provider="codex", query="hello", roles=["user"], stale_policy="error")
                with self.assertRaises(ValueError):
                    first.refresh_index(provider="codex", max_refresh_sessions=0)
                self.assertEqual(first.refresh_index(provider="codex"), {"codex": 1, "total": 1})
            finally:
                first.close()

            second = SessionSearchEngine(
                CodexStore(codex_home),
                PiStore(root / ".pi" / "agent"),
                OpenCodeStore(root / "opencode"),
                index_path,
            )
            try:
                self.assertEqual(second.refresh_index(provider="codex"), {"codex": 0, "total": 0})
                status = second.index_status(provider="codex")
                self.assertEqual(status["total_refresh_sessions"], 0)
                hits = second.search(provider="codex", query="hello", roles=["user"], stale_policy="skip")
                self.assertEqual(len(hits["results"]), 1)
                self.assertEqual(hits["results"][0]["session_id"], session_id)
            finally:
                second.close()


class DevinTests(unittest.TestCase):
    def test_devin_transcript_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            devin_home = root / "devin"
            transcript_dir = devin_home / "cli" / "transcripts"
            transcript_dir.mkdir(parents=True)
            session_id = "test-session"
            transcript = {
                "schema_version": "ATIF-v1.7",
                "session_id": session_id,
                "agent": {
                    "name": "devin",
                    "version": "2026.8.18",
                    "model_name": "GLM-5.2",
                    "extra": {"backend": "Windsurf", "cwd": "/test/project"},
                },
                "steps": [
                    {"step_id": 1, "timestamp": "2026-06-29T21:13:41Z", "source": "system", "message": "You are Devin, an interactive command line agent from Cognition."},
                    {"step_id": 2, "timestamp": "2026-06-29T21:13:42Z", "source": "user", "message": "Fix the bug in main.py"},
                    {"step_id": 3, "timestamp": "2026-06-29T21:13:43Z", "source": "agent", "message": "I will look at main.py now.", "extra": {"generation_model": "GLM-5.2", "telemetry": {"source": "assistant", "operation": "inference"}}},
                ],
                "final_metrics": {"total_prompt_tokens": 100, "total_completion_tokens": 50, "total_cached_tokens": 0, "total_steps": 3},
            }
            (transcript_dir / f"{session_id}.json").write_text(json.dumps(transcript), encoding="utf-8")

            store = DevinStore(devin_home)
            session = store.load(session_id)
            self.assertEqual(session.provider, "devin")
            self.assertEqual(session.session_id, session_id)
            self.assertEqual(session.cwd, "/test/project")

            messages = MessageExtractor().from_devin(session)
            self.assertEqual(len(messages), 3)
            self.assertEqual(messages[0].role, "user")
            self.assertTrue(messages[0].is_contextual)
            self.assertEqual(messages[1].role, "user")
            self.assertFalse(messages[1].is_contextual)
            self.assertEqual(messages[1].text, "Fix the bug in main.py")
            self.assertEqual(messages[2].role, "assistant")
            self.assertEqual(messages[2].text, "I will look at main.py now.")
            self.assertEqual(messages[2].model, "GLM-5.2")

    def test_devin_to_pi_conversion(self) -> None:
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            devin_home = root / "devin"
            pi_agent_home = root / ".pi" / "agent"
            pi_dcp_home = root / ".pi-dcp"
            transcript_dir = devin_home / "cli" / "transcripts"
            transcript_dir.mkdir(parents=True)
            session_id = "test-session"
            transcript = {
                "schema_version": "ATIF-v1.7",
                "session_id": session_id,
                "agent": {"name": "devin", "version": "2026.8.18", "model_name": "GLM-5.2", "extra": {"cwd": "/test/project"}},
                "steps": [
                    {"step_id": 1, "timestamp": "2026-06-29T21:13:41Z", "source": "user", "message": "Hello world"},
                    {"step_id": 2, "timestamp": "2026-06-29T21:13:42Z", "source": "agent", "message": "Hi there", "extra": {"generation_model": "GLM-5.2"}},
                ],
                "final_metrics": {"total_prompt_tokens": 0, "total_completion_tokens": 0, "total_cached_tokens": 0, "total_steps": 2},
            }
            (transcript_dir / f"{session_id}.json").write_text(json.dumps(transcript), encoding="utf-8")

            plan = DevinToPiConverter(
                DevinStore(devin_home),
                PiStore(pi_agent_home),
                PiDcpStore(pi_dcp_home),
                SessionIdFactory(),
            ).plan(session_id)

            self.assertEqual(plan.source.session_id, session_id)
            self.assertEqual(plan.source.cwd, "/test/project")
            self.assertGreater(len(plan.records), 0)
            self.assertEqual(plan.records[0]["type"], "session")
            messages = [r for r in plan.records if r["type"] == "message"]
            self.assertEqual(len(messages), 2)
            self.assertEqual(messages[0]["message"]["role"], "user")
            self.assertEqual(messages[1]["message"]["role"], "assistant")
            self.assertTrue(plan.services, "Should have DCP service path")

    def test_devin_list_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            devin_home = root / "devin"
            transcript_dir = devin_home / "cli" / "transcripts"
            transcript_dir.mkdir(parents=True)
            for sid in ("alpha", "beta"):
                transcript = {
                    "schema_version": "ATIF-v1.7",
                    "session_id": sid,
                    "agent": {"name": "devin", "version": "2026.8.18", "model_name": "GLM-5.2", "extra": {"cwd": "/test"}},
                    "steps": [{"step_id": 1, "timestamp": "2026-06-29T21:13:41Z", "source": "user", "message": "hello"}],
                    "final_metrics": {"total_prompt_tokens": 0, "total_completion_tokens": 0, "total_cached_tokens": 0, "total_steps": 1},
                }
                (transcript_dir / f"{sid}.json").write_text(json.dumps(transcript), encoding="utf-8")

            store = DevinStore(devin_home)
            summaries = store.list()
            self.assertEqual(len(summaries), 2)
            ids = {s.session_id for s in summaries}
            self.assertEqual(ids, {"alpha", "beta"})
            self.assertTrue(all(s.provider == "devin" for s in summaries))


class TraceTests(unittest.TestCase):
    def _make_messages(self) -> list[TextMessage]:
        return [
            TextMessage(role="system", text="You are a helpful assistant.", timestamp="2026-07-01T15:00:00Z", is_contextual=True),
            TextMessage(role="user", text="Fix the bug", timestamp="2026-07-01T15:00:01Z"),
            TextMessage(role="assistant", text="Looking at it now.", timestamp="2026-07-01T15:00:02Z", model="gpt-4o"),
            TextMessage(role="system", text="Previous context summarized...", timestamp="2026-07-01T15:00:03Z", is_compaction=True),
            TextMessage(role="user", text="Done yet?", timestamp="2026-07-01T15:00:04Z"),
            TextMessage(role="assistant", text="Yes, fixed it.", timestamp="2026-07-01T15:00:05Z", model="gpt-4o"),
        ]

    def _make_session(self, messages: list[TextMessage]) -> NativeSession:
        return NativeSession(
            provider="codex",
            session_id="test-trace-001",
            cwd="/test/project",
            timestamp="2026-07-01T15:00:00Z",
            path=Path("/tmp/test.jsonl"),
            records=[],
        )

    def test_sts_format(self) -> None:
        session = self._make_session(self._make_messages())
        records = STSTraceBuilder().build(session, self._make_messages())
        # Header + 5 non-contextual messages (system contextual is skipped, compaction becomes system)
        self.assertEqual(records[0]["type"], "session")
        self.assertEqual(records[0]["harness"], "codex")
        self.assertEqual(records[0]["id"], "test-trace-001")
        self.assertEqual(records[0]["cwd"], "/test/project")
        # First message line should be user (contextual system is skipped)
        self.assertEqual(records[1]["type"], "message")
        self.assertEqual(records[1]["message"]["role"], "user")
        self.assertEqual(records[1]["message"]["content"], "Fix the bug")
        # Assistant message should have model and timestamp
        self.assertEqual(records[2]["message"]["role"], "assistant")
        self.assertEqual(records[2]["message"]["model"], "gpt-4o")
        # Compaction becomes a system message
        self.assertEqual(records[3]["message"]["role"], "system")
        self.assertIn("[compaction summary]", records[3]["message"]["content"])
        # Total: header + 5 message lines (contextual skipped, compaction included)
        self.assertEqual(len(records), 6)

    def test_openai_format(self) -> None:
        session = self._make_session(self._make_messages())
        records = OpenAITraceBuilder().build(session, self._make_messages())
        # Single object with messages array
        self.assertEqual(len(records), 1)
        msgs = records[0]["messages"]
        # Contextual system is skipped, compaction becomes system
        # user, assistant, system(compaction), user, assistant = 5
        self.assertEqual(len(msgs), 5)
        self.assertEqual(msgs[0]["role"], "user")
        self.assertEqual(msgs[0]["content"], "Fix the bug")
        self.assertEqual(msgs[1]["role"], "assistant")
        self.assertEqual(msgs[2]["role"], "system")
        self.assertIn("[compaction summary]", msgs[2]["content"])
        self.assertEqual(msgs[3]["role"], "user")
        self.assertEqual(msgs[4]["role"], "assistant")

    def test_sharegpt_format(self) -> None:
        session = self._make_session(self._make_messages())
        records = ShareGPTTraceBuilder().build(session, self._make_messages())
        self.assertEqual(len(records), 1)
        convs = records[0]["conversations"]
        self.assertEqual(len(convs), 5)
        self.assertEqual(convs[0]["from"], "human")
        self.assertEqual(convs[0]["value"], "Fix the bug")
        self.assertEqual(convs[1]["from"], "gpt")
        self.assertEqual(convs[2]["from"], "system")
        self.assertIn("[compaction summary]", convs[2]["value"])
        self.assertEqual(convs[3]["from"], "human")
        self.assertEqual(convs[4]["from"], "gpt")

    def test_build_trace_dispatch(self) -> None:
        session = self._make_session(self._make_messages())
        messages = self._make_messages()
        for fmt in ("sts", "openai", "sharegpt"):
            records = build_trace(fmt, session, messages)
            self.assertGreater(len(records), 0)
        with self.assertRaises(ValueError):
            build_trace("unknown", session, messages)


if __name__ == "__main__":
    unittest.main()
