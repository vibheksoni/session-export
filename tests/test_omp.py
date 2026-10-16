from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from session_sdk.converters import (
    MessageExtractor,
    OmpRecordBuilder,
    OmpToClaudeConverter,
    OmpToCodexConverter,
    OmpToDevinConverter,
    OmpToFactoryConverter,
    OmpToFreebuffConverter,
    OmpToGrokConverter,
    OmpToOpenCodeConverter,
    OmpToPiConverter,
    OmpToWindsurfConverter,
    ToOmpConverter,
)
from session_sdk.jsonl import JsonlFile
from session_sdk.models import NativeSession, TextMessage
from session_sdk.paths import SessionIdFactory, WindowsDefaults, encode_omp_cwd
from session_sdk.search import SessionSearchEngine
from session_sdk.stores import (
    OMP_TITLE_SLOT_BYTES,
    ClaudeStore,
    CodexStore,
    DevinStore,
    FactoryStore,
    FreebuffStore,
    GrokStore,
    OmpStore,
    OpenCodeStore,
    PiDcpStore,
    PiStore,
    WindsurfStore,
    destination_exists,
    omp_title_slot,
)
from session_sdk.traces import build_trace
from unisessions.cli import main

OMP_SESSION = "019e0000-0000-7000-8000-000000000001"
OMP_CWD = "/work/demo-app"
# omp resolves /work/demo-app against the drive on Windows (C:\work\demo-app), as it does for any cwd.
OMP_BUCKET = "--C--work-demo-app--" if sys.platform == "win32" else "--work-demo-app--"


def omp_entry(entry_type: str, entry_id: str, parent: str | None, **fields: object) -> dict[str, object]:
    return {"type": entry_type, "id": entry_id, "parentId": parent, "timestamp": f"2026-10-01T10:00:{int(entry_id[-2:], 16):02d}.000Z", **fields}


def user(entry_id: str, parent: str | None, text: str, **extra: object) -> dict[str, object]:
    return omp_entry("message", entry_id, parent, message={"role": "user", "content": [{"type": "text", "text": text}], "attribution": "user", "timestamp": 1790000000000, **extra})


def assistant(entry_id: str, parent: str | None, *blocks: dict[str, object], **extra: object) -> dict[str, object]:
    message = {"role": "assistant", "content": list(blocks), "api": "anthropic-messages", "provider": "anthropic", "model": "claude-opus-4-6",
               "usage": {"input": 1, "output": 1, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 2, "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}},
               "stopReason": "stop", "timestamp": 1790000000000}
    message.update(extra)
    return omp_entry("message", entry_id, parent, message=message)


def text(value: str) -> dict[str, object]:
    return {"type": "text", "text": value}


def write_omp_session(sessions_root: Path, entries: list[dict[str, object]], *, session_id: str = OMP_SESSION, cwd: str = OMP_CWD,
                      bucket: str = OMP_BUCKET, title: str = "Plan the app", header_title: str | None = None, slot: bool = True) -> Path:
    """An OMP session file as omp 18 writes it: title slot, header, entries."""
    timestamp = "2026-10-01T10:00:00.000Z"
    header: dict[str, object] = {"type": "session", "version": 3, "id": session_id, "timestamp": timestamp, "cwd": cwd}
    if header_title is not None:
        header["title"] = header_title
        header["titleSource"] = "auto"
    records: list[dict[str, object]] = [omp_title_slot(title, timestamp)] if slot else []
    records += [header, *entries]
    path = sessions_root / bucket / f"2026-10-01T10-00-00-000Z_{session_id}.jsonl"
    JsonlFile(path).write(records)
    return path


def rich_session() -> list[dict[str, object]]:
    """A session with the entry kinds omp writes most, plus an abandoned branch that must not be read."""
    return [
        omp_entry("model_change", "00000001", None, model="anthropic/claude-opus-4-6", resolvedModelIsFallback=False),
        omp_entry("thinking_level_change", "00000002", "00000001", thinkingLevel="high", configured=None),
        user("00000003", "00000002", "Plan the app\nwith a second line"),
        omp_entry("custom", "00000004", "00000003", customType="system-prompt-digest", data="abc"),
        assistant("00000005", "00000004", {"type": "thinking", "thinking": "secret reasoning", "thinkingSignature": "sig"},
                  text("Let me read it."), {"type": "toolCall", "id": "call_1", "name": "read", "arguments": {"path": "README.md"}}, stopReason="toolUse"),
        omp_entry("message", "00000006", "00000005", message={"role": "toolResult", "toolCallId": "call_1", "toolName": "read", "content": [text("tool output text")], "isError": False, "timestamp": 1790000000000}),
        omp_entry("message", "00000007", "00000006", message={"role": "developer", "content": [text("<system-reminder>be terse</system-reminder>")], "timestamp": 1790000000000}),
        assistant("00000008", "00000007", text("Here is the plan.")),
        omp_entry("message", "00000009", "00000008", message={"role": "bashExecution", "command": "ls", "output": "bash output", "exitCode": 0, "timestamp": 1790000000000}),
        user("0000000a", "00000009", "abandoned question"),
        assistant("0000000b", "0000000a", text("abandoned answer")),
        omp_entry("branch_summary", "0000000c", "00000009", fromId="0000000b", summary="Tried a dead end."),
        omp_entry("custom_message", "0000000d", "0000000c", customType="irc", content="peer note", display=True),
        omp_entry("compaction", "0000000e", "0000000d", summary="Summary of the plan so far.", firstKeptEntryId="0000000e", tokensBefore=1000),
        omp_entry("model_change", "0000000f", "0000000e", model="openai/gpt-5.5", role="smol"),
        user("00000010", "0000000f", "continue please", synthetic=True),
        user("00000011", "00000010", "Now write the changelog."),
        omp_entry("message", "00000012", "00000011", message={"role": "assistant", "content": [text("Changelog written.")], "timestamp": 1790000000000}),
        omp_entry("credential_pin", "00000013", "00000012", provider="anthropic", hash="h"),
    ]


def claude_fixture(claude_home: Path, session_id: str, cwd: str = "/work/demo-app") -> Path:
    path = claude_home / "projects" / "-work-demo-app" / f"{session_id}.jsonl"
    JsonlFile(path).write([
        {"type": "user", "uuid": "u1", "parentUuid": None, "sessionId": session_id, "cwd": cwd, "timestamp": "2026-06-04T09:15:00.250Z",
         "message": {"role": "user", "content": "Can you summarize the README?"}},
        {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "sessionId": session_id, "cwd": cwd, "timestamp": "2026-06-04T11:16:00.000+02:00",
         "message": {"role": "assistant", "model": "claude-opus-5-5", "content": [{"type": "thinking", "thinking": "hmm", "signature": "s"}, text("Sure: it explains the setup.")]}},
        {"type": "system", "subtype": "compact_boundary", "content": "Earlier: the README was summarized.", "timestamp": "2026-06-04T09:17:00.000Z"},
        {"type": "user", "uuid": "u2", "parentUuid": "a1", "sessionId": session_id, "cwd": cwd, "timestamp": "2026-06-04T09:18:00.000Z",
         "message": {"role": "user", "content": [text("What about the tests?")]}},
    ])
    return path


def texts(messages: list[TextMessage]) -> list[tuple[str, str]]:
    return [(m.role, m.text) for m in messages if not m.is_contextual]


class OmpPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(os.path.realpath(self._tmp.name))
        self.home = root / "home" / "me"
        self.temp_root = root / "tmp"
        self.home.mkdir(parents=True)
        self.temp_root.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def encode(self, cwd: Path | str) -> str:
        return encode_omp_cwd(str(cwd), home=self.home, temp_root=self.temp_root)

    def test_buckets_match_omp_scopes(self) -> None:
        self.assertEqual(self.encode(self.home / "projects" / "demo-app"), "-projects-demo-app")
        self.assertEqual(self.encode(self.home / ".omp" / "wt" / "wt-1"), "-.omp-wt-wt-1")
        self.assertEqual(self.encode(self.home), "-")
        self.assertEqual(self.encode(self.temp_root / "scratch" / "x"), "-tmp-scratch-x")
        self.assertEqual(self.encode(self.temp_root), "-tmp")
        outside = self.home.parent.parent / "srv" / "app"
        self.assertEqual(self.encode(outside), "--" + re.sub(r"[/\\:]", "-", str(outside).lstrip("/\\")) + "--")

    def test_temp_root_inside_home_wins(self) -> None:
        nested_temp = self.home / "AppData" / "Temp"
        nested_temp.mkdir(parents=True)
        self.assertEqual(encode_omp_cwd(str(nested_temp / "job"), home=self.home, temp_root=nested_temp), "-tmp-job")

    def test_paths_are_normalized_and_symlinks_resolved(self) -> None:
        self.assertEqual(self.encode(f"{self.home}/projects/../projects/app/"), "-projects-app")
        target = self.home / "real"
        target.mkdir()
        link = self.temp_root / "link"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("symlinks unavailable")
        self.assertEqual(self.encode(link), "-real")
        # A missing path cannot be canonicalized, so it is encoded as written, like omp does.
        self.assertEqual(self.encode(link / "missing"), "-tmp-link-missing")

    def test_child_named_like_dotdot_is_outside_home_like_omp(self) -> None:
        # omp's containment test treats a relative path starting with ".." as outside.
        odd = self.home / "..cache"
        self.assertEqual(self.encode(odd), "--" + re.sub(r"[/\\:]", "-", str(odd).lstrip("/\\")) + "--")

    def test_foreign_platform_path_is_encoded_as_written(self) -> None:
        if sys.platform == "win32":
            self.skipTest("Windows paths are absolute here")
        self.assertEqual(encode_omp_cwd(r"C:\Users\me\app"), "--C--Users-me-app--")


class OmpAgentHomeTests(unittest.TestCase):
    def agent_home(self, **env: str) -> Path:
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(os.environ, env, clear=True):
            resolved = WindowsDefaults(Path(root)).omp_agent_home
            return resolved.relative_to(root) if resolved.is_relative_to(root) else resolved

    def test_default_and_config_dir(self) -> None:
        self.assertEqual(self.agent_home(), Path(".omp/agent"))
        self.assertEqual(self.agent_home(PI_CONFIG_DIR=".omp-dev"), Path(".omp-dev/agent"))

    def test_profiles(self) -> None:
        self.assertEqual(self.agent_home(OMP_PROFILE="work"), Path(".omp/profiles/work/agent"))
        self.assertEqual(self.agent_home(PI_PROFILE="work"), Path(".omp/profiles/work/agent"))
        # An explicitly empty OMP_PROFILE selects the default profile over PI_PROFILE.
        self.assertEqual(self.agent_home(OMP_PROFILE="", PI_PROFILE="work"), Path(".omp/agent"))
        self.assertEqual(self.agent_home(OMP_PROFILE="default"), Path(".omp/agent"))
        self.assertEqual(self.agent_home(OMP_PROFILE="Bad Name"), Path(".omp/agent"))

    def test_agent_dir_override_applies_outside_profiles(self) -> None:
        override = os.path.abspath("custom-agent")
        self.assertEqual(self.agent_home(PI_CODING_AGENT_DIR=override), Path(override))
        self.assertEqual(self.agent_home(PI_CODING_AGENT_DIR=override, OMP_PROFILE="work"), Path(".omp/profiles/work/agent"))

    @unittest.skipUnless(sys.platform in ("linux", "darwin"), "XDG layout is Linux/macOS only")
    def test_xdg_data_home_is_used_once_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            home, xdg = Path(root) / "home", Path(root) / "xdg"
            with mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(xdg)}, clear=True):
                self.assertEqual(WindowsDefaults(home).omp_agent_home, home / ".omp" / "agent")
                (xdg / "omp").mkdir(parents=True)
                self.assertEqual(WindowsDefaults(home).omp_agent_home, xdg / "omp")


class OmpTitleSlotTests(unittest.TestCase):
    def line(self, record: dict[str, object]) -> bytes:
        return json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"

    def test_slot_is_fixed_width_for_any_title(self) -> None:
        for title in ("", "Plan the app", 'quote " back\\slash\ttab', "émoji 📥 and 漢字", "x" * 400, "📥" * 200):
            with self.subTest(title=title[:20]):
                record = omp_title_slot(title, "2026-10-01T10:00:00.000Z")
                self.assertEqual(len(self.line(record)), OMP_TITLE_SLOT_BYTES)
                self.assertEqual(list(record), ["type", "v", "title", "source", "updatedAt", "pad"])
                self.assertTrue(title.startswith(str(record["title"])))

    def test_long_title_keeps_as_many_code_points_as_fit(self) -> None:
        record = omp_title_slot("📥" * 200, "2026-10-01T10:00:00.000Z")
        kept = str(record["title"])
        self.assertEqual(record["pad"], " " * (OMP_TITLE_SLOT_BYTES - len(self.line({**record, "pad": ""}))))
        self.assertGreater(len(self.line({**record, "title": kept + "📥", "pad": ""})), OMP_TITLE_SLOT_BYTES)

    def test_untitled_slot_has_no_source(self) -> None:
        self.assertNotIn("source", omp_title_slot("", "2026-10-01T10:00:00.000Z", None))

    def test_store_refuses_a_slot_of_the_wrong_width(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = OmpStore(Path(root))
            bad = {"type": "title", "v": 1, "title": "x", "updatedAt": "2026-10-01T10:00:00.000Z", "pad": ""}
            with self.assertRaises(ValueError):
                store.write(Path(root) / "s.jsonl", [bad])


class OmpStoreTests(unittest.TestCase):
    def test_lists_and_loads_top_level_sessions_only(self) -> None:
        with tempfile.TemporaryDirectory() as root_name:
            sessions = Path(root_name) / "agent" / "sessions"
            path = write_omp_session(sessions, rich_session())
            subagent = path.with_suffix("") / "Scout.jsonl"
            JsonlFile(subagent).write([{"type": "session", "version": 3, "id": "sub", "timestamp": "2026-10-01T10:00:00.000Z", "cwd": OMP_CWD}])
            store = OmpStore(Path(root_name) / "agent")

            summaries = store.list()
            self.assertEqual([s.session_id for s in summaries], [OMP_SESSION])
            self.assertEqual(summaries[0].provider, "omp")
            self.assertEqual(summaries[0].cwd, OMP_CWD)
            self.assertEqual(summaries[0].message_count, sum(1 for e in rich_session() if e["type"] == "message"))
            self.assertEqual(store.list_metadata()[0].message_count, -1)

            session = store.load(OMP_SESSION)
            self.assertEqual(session.path, path)
            self.assertEqual(session.records[0]["type"], "session")
            self.assertEqual(store.load(OMP_SESSION[:13]).session_id, OMP_SESSION)
            with self.assertRaises(FileNotFoundError):
                store.load("nope")

    def test_slot_title_wins_over_stale_header_title(self) -> None:
        with tempfile.TemporaryDirectory() as root_name:
            agent = Path(root_name) / "agent"
            write_omp_session(agent / "sessions", [], title="Renamed in place", header_title="First title")
            header = OmpStore(agent).load(OMP_SESSION).records[0]
            self.assertEqual(header["title"], "Renamed in place")
            self.assertEqual(header["titleSource"], "auto")

    def test_reads_files_without_a_title_slot(self) -> None:
        with tempfile.TemporaryDirectory() as root_name:
            agent = Path(root_name) / "agent"
            write_omp_session(agent / "sessions", [user("00000001", None, "hello")], slot=False)
            store = OmpStore(agent)
            self.assertEqual(store.list()[0].message_count, 1)
            self.assertEqual(texts(MessageExtractor().from_omp(store.load(OMP_SESSION))), [("user", "hello")])

    def test_custom_session_dir_replaces_the_sessions_root(self) -> None:
        with tempfile.TemporaryDirectory() as root_name:
            custom = Path(root_name) / "elsewhere"
            write_omp_session(custom, [])
            store = OmpStore(Path(root_name) / "agent", custom)
            self.assertEqual(len(store.list()), 1)
            self.assertEqual(store.destination_path("id", "2026-10-01T10:00:00.000Z", "/srv/app").parent.parent, custom)


class OmpExtractionTests(unittest.TestCase):
    def extract(self, entries: list[dict[str, object]]) -> list[TextMessage]:
        session = NativeSession("omp", OMP_SESSION, OMP_CWD, "2026-10-01T10:00:00.000Z", Path("s.jsonl"), [{"type": "session", "id": OMP_SESSION}, *entries])
        return MessageExtractor().from_omp(session)

    def test_reads_text_on_the_active_branch(self) -> None:
        messages = self.extract(rich_session())
        self.assertEqual(texts(messages), [
            ("user", "Plan the app\nwith a second line"),
            ("assistant", "Let me read it."),
            ("assistant", "Here is the plan."),
            ("user", "Summary of the plan so far."),
            ("user", "Now write the changelog."),
            ("assistant", "Changelog written."),
        ])
        joined = " ".join(m.text for m in messages)
        for hidden in ("abandoned", "secret reasoning", "tool output", "bash output"):
            self.assertNotIn(hidden, joined)
        self.assertTrue(next(m for m in messages if m.text.startswith("Summary")).is_compaction)
        contextual = {m.text for m in messages if m.is_contextual}
        self.assertEqual(contextual, {"<system-reminder>be terse</system-reminder>", "Tried a dead end.", "peer note", "continue please"})

    def test_assistant_model_comes_from_message_or_default_model_change(self) -> None:
        messages = [m for m in self.extract(rich_session()) if m.role == "assistant"]
        self.assertEqual((messages[0].provider, messages[0].model, messages[0].api), ("anthropic", "claude-opus-4-6", "anthropic-messages"))
        # The last reply names no model; a role="smol" change must not apply to it.
        self.assertEqual((messages[-1].provider, messages[-1].model), ("anthropic", "claude-opus-4-6"))
        legacy = self.extract([omp_entry("model_change", "00000001", None, provider="openai", modelId="gpt-4o"),
                               omp_entry("message", "00000002", "00000001", message={"role": "assistant", "content": [text("hi")]})])
        self.assertEqual((legacy[0].provider, legacy[0].model), ("openai", "gpt-4o"))

    def test_model_usage_can_point_the_leaf_back(self) -> None:
        entries = [user("00000001", None, "kept"), assistant("00000002", "00000001", text("rewound away")),
                   omp_entry("model_usage", "00000003", "00000002", activeLeafId="00000001")]
        self.assertEqual(texts(self.extract(entries)), [("user", "kept")])
        cleared = [*entries[:2], omp_entry("model_usage", "00000003", "00000002", activeLeafId=None)]
        self.assertEqual(self.extract(cleared), [])
        dangling = [*entries[:2], omp_entry("model_usage", "00000003", "00000002", activeLeafId="gone")]
        self.assertEqual(texts(self.extract(dangling)), [("user", "kept"), ("assistant", "rewound away")])

    def test_user_invoked_prompts_are_user_turns(self) -> None:
        entries = [
            omp_entry("custom_message", "00000001", None, customType="skill-prompt", attribution="user", display=True, content="[User invoked the skill] build it"),
            omp_entry("custom_message", "00000002", "00000001", customType="collab-prompt", attribution="user", display=True, content=[text("from a peer")]),
            omp_entry("custom_message", "00000003", "00000002", customType="skill-prompt", attribution="agent", display=True, content="agent-run skill"),
            omp_entry("custom_message", "00000004", "00000003", customType="async-result", attribution="agent", display=True, content="job finished"),
        ]
        self.assertEqual(texts(self.extract(entries)), [("user", "[User invoked the skill] build it"), ("user", "from a peer")])

    def test_clear_takes_earlier_turns_out_of_the_conversation(self) -> None:
        entries = [user("00000001", None, "before clear"), assistant("00000002", "00000001", text("old answer")),
                   omp_entry("reset_boundary", "00000003", "00000002"), user("00000004", "00000003", "after clear")]
        messages = self.extract(entries)
        self.assertEqual(texts(messages), [("user", "after clear")])
        self.assertEqual({m.text for m in messages if m.is_contextual}, {"before clear", "old answer"})

    def test_entries_without_ids_are_read_in_order_and_cycles_stop(self) -> None:
        legacy = [{"type": "message", "message": {"role": "user", "content": "one"}}, {"type": "message", "message": {"role": "assistant", "content": [text("two")]}}]
        self.assertEqual(texts(self.extract(legacy)), [("user", "one"), ("assistant", "two")])
        cycle = [user("00000001", "00000002", "a"), user("00000002", "00000001", "b")]
        self.assertEqual(texts(self.extract(cycle)), [("user", "a"), ("user", "b")])


class OmpBuilderTests(unittest.TestCase):
    MESSAGES = [
        TextMessage("user", "<environment_context>ctx</environment_context>", "2026-06-04T09:00:00Z", is_contextual=True),
        TextMessage("user", "\n  Is the   plan clear?\nsecond line", "2026-06-04T11:15:00.250+02:00"),
        TextMessage("assistant", "Yes.", "2001-09-09T01:46:40.123Z", model="claude-opus-5-5"),
        TextMessage("user", "Summary so far.", "2026-06-04T09:17:00.000Z", is_compaction=True),
        TextMessage("assistant", "Carry on.", "not a timestamp", provider="anthropic", model="claude-opus-5-5", api="anthropic-messages"),
    ]

    def build(self, **kwargs: object) -> list[dict[str, object]]:
        return OmpRecordBuilder().build(OMP_SESSION, OMP_CWD, "2026-06-04T09:15:00.250Z", list(self.MESSAGES), **kwargs)  # type: ignore[arg-type]

    def test_records_form_one_valid_omp_branch(self) -> None:
        records = self.build()
        slot, header, *entries = records
        self.assertEqual(len(json.dumps(slot, ensure_ascii=False, separators=(",", ":")).encode()) + 1, OMP_TITLE_SLOT_BYTES)
        self.assertEqual(slot["title"], "Is the plan clear?")
        self.assertEqual(header, {"type": "session", "version": 3, "id": OMP_SESSION, "timestamp": "2026-06-04T09:15:00.250Z", "cwd": OMP_CWD,
                                  "title": "Is the plan clear?", "titleSource": "auto"})
        self.assertEqual([e["type"] for e in entries], ["message", "message", "compaction", "message"])
        ids = [e["id"] for e in entries]
        self.assertEqual(len(set(ids)), len(ids))
        self.assertTrue(all(isinstance(i, str) and len(i) == 8 and int(i, 16) >= 0 for i in ids))
        self.assertEqual([e["parentId"] for e in entries], [None, *ids[:-1]])
        compaction = entries[2]
        self.assertEqual(compaction["firstKeptEntryId"], compaction["id"])
        self.assertGreater(compaction["tokensBefore"], 0)

    def test_messages_use_omp_shapes_and_normalized_times(self) -> None:
        _, _, first, reply, _, last = self.build()
        self.assertEqual(first["timestamp"], "2026-06-04T09:15:00.250Z")
        self.assertEqual(first["message"], {"role": "user", "content": [text("\n  Is the   plan clear?\nsecond line")], "attribution": "user", "timestamp": 1780564500250})
        self.assertEqual(reply["message"]["timestamp"], 1000000000123)
        self.assertEqual((reply["message"]["provider"], reply["message"]["model"], reply["message"]["api"]), ("session-export", "claude-opus-5-5", "openai-completions"))
        self.assertEqual(reply["message"]["usage"]["totalTokens"], 0)
        self.assertEqual(reply["message"]["stopReason"], "stop")
        # An unparseable timestamp falls back to the session time.
        self.assertEqual(last["timestamp"], "2026-06-04T09:15:00.250Z")
        self.assertEqual((last["message"]["provider"], last["message"]["api"]), ("anthropic", "anthropic-messages"))

    def test_source_provenance_and_determinism(self) -> None:
        source = NativeSession("claude", "abc", "/src", "", Path("/src/abc.jsonl"), [])
        records = self.build(source=source)
        self.assertEqual(records, self.build(source=source))
        custom = records[-1]
        self.assertEqual(custom["type"], "custom")
        self.assertEqual(custom["customType"], "foreign_session_import")
        self.assertEqual(custom["data"], {"source": "claude", "sourceId": "abc", "sourcePath": str(Path("/src/abc.jsonl")), "sourceCwd": "/src"})
        self.assertEqual(custom["parentId"], records[-2]["id"])

    def test_session_without_user_text_is_untitled(self) -> None:
        slot, header = OmpRecordBuilder().build(OMP_SESSION, OMP_CWD, "2026-06-04T09:15:00.250Z", [])
        self.assertEqual(slot["title"], "")
        self.assertNotIn("source", slot)
        self.assertNotIn("title", header)


class OmpConversionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.agent = self.root / ".omp" / "agent"
        self.omp = OmpStore(self.agent)
        self.ids = SessionIdFactory(preserve_ids=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_claude_to_omp_writes_a_loadable_session_and_tracks_changes(self) -> None:
        session_id = "0f6c2a1e-5b7d-4c3a-9e8f-1a2b3c4d5e6f"
        source = claude_fixture(self.root / ".claude", session_id)
        converter = ToOmpConverter(ClaudeStore(self.root / ".claude"), self.omp, self.ids)
        plan = converter.plan(session_id)
        self.assertEqual(plan.destination, self.agent / "sessions" / OMP_BUCKET / f"2026-06-04T09-15-00-250Z_{session_id}.jsonl")
        self.assertTrue(converter.has_changes(session_id))
        converter.write(plan)

        written = self.omp.load(session_id)
        self.assertEqual(written.cwd, "/work/demo-app")
        self.assertEqual(texts(MessageExtractor().from_omp(written)), [
            ("user", "Can you summarize the README?"),
            ("assistant", "Sure: it explains the setup."),
            ("user", "Earlier: the README was summarized."),
            ("user", "What about the tests?"),
        ])
        self.assertEqual(written.records[-1]["data"]["sourcePath"], str(source))
        # omp resumes on the model the last reply names; Claude replies must name a real one.
        reply = next(r["message"] for r in written.records if r.get("type") == "message" and r["message"]["role"] == "assistant")
        self.assertEqual((reply["provider"], reply["model"], reply["api"]), ("anthropic", "claude-opus-5-5", "anthropic-messages"))
        self.assertFalse(converter.has_changes(session_id))

        lines = source.read_text(encoding="utf-8").splitlines()
        lines.append(json.dumps({"type": "assistant", "timestamp": "2026-06-04T09:19:00.000Z", "message": {"role": "assistant", "content": [text("Tests answer.")]}}))
        source.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.assertTrue(converter.has_changes(session_id))
        self.assertTrue(ToOmpConverter(ClaudeStore(self.root / ".claude"), self.omp, self.ids).has_changes("missing"))

    def test_non_uuid_ids_and_missing_cwd(self) -> None:
        pi_home = self.root / ".pi" / "agent"
        JsonlFile(pi_home / "sessions" / "--x--" / "2026-06-28T04-20-31-629Z_my_session.jsonl").write([
            {"type": "session", "version": 3, "id": "my_session", "timestamp": "2026-06-28T04:20:31.629Z", "cwd": ""},
            {"type": "message", "id": "a", "parentId": None, "timestamp": "2026-06-28T04:20:32.000Z", "message": {"role": "user", "content": [text("hi")]}},
        ])
        home = self.root / "home"
        home.mkdir()
        # The test tree sits in the temp root, which omp checks before home; point it elsewhere.
        elsewhere = self.root / "tmpdir"
        elsewhere.mkdir()
        env = {"HOME": str(home), "USERPROFILE": str(home), "TMPDIR": str(elsewhere), "TEMP": str(elsewhere), "TMP": str(elsewhere)}
        with mock.patch.dict(os.environ, env):
            plan = ToOmpConverter(PiStore(pi_home), self.omp, self.ids).plan("my_session")
        new_id = plan.records[1]["id"]
        self.assertNotIn("_", str(new_id))
        self.assertEqual(plan.records[1]["cwd"], str(home))
        self.assertEqual(plan.destination.parent.name, "-")
        self.assertTrue(plan.destination.name.endswith(f"_{new_id}.jsonl"))

    TARGETS_ALWAYS_CHANGED = {"devin", "windsurf", "freebuff"}

    def test_omp_to_every_target_plans_writes_and_detects_changes(self) -> None:
        path = write_omp_session(self.agent / "sessions", rich_session())
        out = self.root / "out"
        targets = {
            "pi": OmpToPiConverter(self.omp, PiStore(out / "pi"), PiDcpStore(out / "pi-dcp"), self.ids),
            "codex": OmpToCodexConverter(self.omp, CodexStore(out / "codex"), self.ids),
            "opencode": OmpToOpenCodeConverter(self.omp, OpenCodeStore(out / "opencode"), self.ids),
            "claude": OmpToClaudeConverter(self.omp, ClaudeStore(out / "claude"), self.ids),
            "devin": OmpToDevinConverter(self.omp, DevinStore(out / "devin"), self.ids),
            "factory": OmpToFactoryConverter(self.omp, FactoryStore(out / "factory"), self.ids),
            "windsurf": OmpToWindsurfConverter(self.omp, WindsurfStore(out / "windsurf"), self.ids),
            "grok": OmpToGrokConverter(self.omp, GrokStore(out / "grok"), self.ids),
            "freebuff": OmpToFreebuffConverter(self.omp, FreebuffStore(out / "freebuff"), self.ids),
        }
        for name, converter in targets.items():
            with self.subTest(target=name):
                plan = converter.plan(OMP_SESSION)
                self.assertEqual(plan.source.provider, "omp")
                self.assertTrue(converter.has_changes(OMP_SESSION))
                converter.write(plan, overwrite=True)
                self.assertTrue(destination_exists(plan.destination))
                if name not in self.TARGETS_ALWAYS_CHANGED:
                    self.assertFalse(converter.has_changes(OMP_SESSION), "an unchanged session should not be rewritten")
        self.assertIn("omp-import", (out / "codex").rglob("*.jsonl").__next__().read_text(encoding="utf-8"))

        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(user("00000014", "00000013", "one more thing")) + "\n")
        self.omp.invalidate_paths()
        for name, converter in targets.items():
            with self.subTest(target=name, after="growth"):
                self.assertTrue(converter.has_changes(OMP_SESSION), "a new message must be detected")

    def test_round_trip_through_pi_keeps_the_conversation(self) -> None:
        write_omp_session(self.agent / "sessions", rich_session())
        pi = PiStore(self.root / ".pi" / "agent")
        to_pi = OmpToPiConverter(self.omp, pi, PiDcpStore(self.root / ".pi-dcp"), self.ids)
        to_pi.write(to_pi.plan(OMP_SESSION))
        back_store = OmpStore(self.root / "back")
        back = ToOmpConverter(pi, back_store, self.ids)
        back.write(back.plan(OMP_SESSION))

        extractor = MessageExtractor()
        original = [(m.role, m.text, m.is_compaction) for m in extractor.from_omp(self.omp.load(OMP_SESSION)) if not m.is_contextual]
        returned = [(m.role, m.text, m.is_compaction) for m in extractor.from_omp(back_store.load(OMP_SESSION))]
        self.assertEqual(returned, original)


class OmpCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.agent = self.root / ".omp" / "agent"
        self.base = ["--omp-agent-home", str(self.agent), "--claude-home", str(self.root / ".claude"), "--pi-agent-home", str(self.root / ".pi" / "agent"),
                     "--pi-dcp-home", str(self.root / ".pi-dcp"), "--codex-home", str(self.root / ".codex"), "--opencode-data-home", str(self.root / "opencode"),
                     "--devin-home", str(self.root / "devin"), "--factory-home", str(self.root / "factory"), "--windsurf-home", str(self.root / "windsurf"),
                     "--grok-home", str(self.root / "grok"), "--freebuff-home", str(self.root / "freebuff"), "--t3-home", str(self.root / "t3")]

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_cli(self, *args: str) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            status = main([*self.base, *args])
        return status, output.getvalue()

    def test_claude_to_omp_dry_run_write_and_conflicts(self) -> None:
        session_id = "0f6c2a1e-5b7d-4c3a-9e8f-1a2b3c4d5e6f"
        claude_fixture(self.root / ".claude", session_id)
        sessions = self.agent / "sessions"

        status, out = self.run_cli("claude-to-omp", session_id)
        self.assertEqual(status, 0)
        self.assertIn(f"destination: {sessions / OMP_BUCKET}", out)
        self.assertFalse(sessions.exists(), "a dry run must not write")

        self.assertEqual(self.run_cli("claude-to-omp", session_id, "--write")[0], 0)
        status, out = self.run_cli("list", "omp")
        self.assertEqual(status, 0)
        self.assertIn(f"omp\t{session_id}\t", out)

        status, out = self.run_cli("claude-to-omp", session_id, "--write")
        self.assertIn("Destination exists, skipping", out)
        status, out = self.run_cli("claude-to-omp", session_id, "--write", "--on-conflict", "fork")
        self.assertIn("Forked to new session ID", out)
        self.assertEqual(len(list(sessions.rglob("*.jsonl"))), 2)

    def test_every_omp_command_runs(self) -> None:
        write_omp_session(self.agent / "sessions", rich_session())
        out_roots = {"pi": ".pi", "codex": ".codex", "opencode": "opencode", "claude": ".claude", "devin": "devin", "factory": "factory",
                     "windsurf": "windsurf", "grok": "grok", "freebuff": "freebuff", "t3": "t3"}
        for target, root in out_roots.items():
            with self.subTest(command=f"omp-to-{target}"):
                status, out = self.run_cli(f"omp-to-{target}", OMP_SESSION)
                self.assertEqual(status, 0)
                destination = next(line.split(":", 1)[1].strip() for line in out.splitlines() if line.startswith("destination:"))
                self.assertTrue(Path(destination).is_relative_to(self.root / root), destination)
        status, out = self.run_cli("to-trace", "omp", OMP_SESSION, "--format", "sharegpt")
        self.assertEqual(status, 0)
        self.assertIn("Changelog written.", out)
        # A missing session reaches the source's own store, which names itself in the error.
        not_found = {"pi": "Pi session", "codex": "Codex session", "opencode": "OpenCode session", "devin": "Devin session", "factory": "Factory session",
                     "windsurf": "Windsurf session", "grok": "Grok session", "freebuff": "Freebuff session", "t3": "T3 thread"}
        for source, message in not_found.items():
            with self.subTest(command=f"{source}-to-omp"), self.assertRaisesRegex(FileNotFoundError, message):
                self.run_cli(f"{source}-to-omp", "no-such-session")

    def test_export_all_can_target_omp(self) -> None:
        session_id = "01234567-89ab-cdef-0123-456789abcdef"
        JsonlFile(self.root / ".codex" / "sessions" / "2026" / "06" / "10" / f"rollout-2026-06-10T23-22-58-{session_id}.jsonl").write([
            {"timestamp": "2026-06-10T23:22:58.000Z", "type": "session_meta", "payload": {"id": session_id, "timestamp": "2026-06-10T23:22:58.000Z", "cwd": "/work/demo-app"}},
            {"timestamp": "2026-06-10T23:23:00.000Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}]}},
        ])
        status, out = self.run_cli("export-all", "--targets", "omp", "--write")
        self.assertEqual(status, 0)
        self.assertIn("omp:ok", out)
        self.assertEqual(texts(MessageExtractor().from_omp(OmpStore(self.agent).load(session_id))), [("user", "hello")])


class OmpSearchAndTraceTests(unittest.TestCase):
    def test_search_indexes_the_active_branch(self) -> None:
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            write_omp_session(root / ".omp" / "agent" / "sessions", rich_session())
            engine = SessionSearchEngine(CodexStore(root / ".codex"), PiStore(root / ".pi"), OpenCodeStore(root / "opencode"), root / "search.sqlite",
                                         omp=OmpStore(root / ".omp" / "agent"))
            try:
                engine.refresh_index(provider="omp")
                hits = engine.search(query="changelog", provider="omp", stale_policy="skip")["results"]
                self.assertEqual({h["session_id"] for h in hits}, {OMP_SESSION})
                self.assertEqual(engine.search(query="abandoned", provider="omp", stale_policy="skip")["results"], [])
                self.assertEqual(engine.list_chats(provider="omp", cwd=OMP_CWD)[0]["session_id"], OMP_SESSION)
            finally:
                engine.close()

    def test_trace_export(self) -> None:
        with tempfile.TemporaryDirectory() as root_name:
            agent = Path(root_name) / "agent"
            write_omp_session(agent / "sessions", rich_session())
            session = OmpStore(agent).load(OMP_SESSION)
            records = build_trace("openai", session, MessageExtractor().from_session(session))
            self.assertIn("Changelog written.", json.dumps(records))


if __name__ == "__main__":
    unittest.main()
