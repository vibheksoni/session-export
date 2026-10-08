from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from session_sdk.converters import MessageExtractor, T3RecordBuilder, ToT3Converter
from session_sdk.jsonl import JsonlFile
from session_sdk.models import TextMessage
from session_sdk.paths import SessionIdFactory
from session_sdk.stores import (
    T3_PROVIDER_DRIVERS,
    PiStore,
    T3Store,
    T3WriteError,
    _T3_UNISESSIONS_APPLICATION_ID,
)
from unisessions.cli import main

PI_SESSION = "11111111-2222-4333-8444-555555555555"


def write_pi(home: Path, session_id: str, texts: list[str]) -> None:
    """Write a Pi session. Even entries are user turns, odd entries are assistant turns."""
    path = home / "sessions" / "--C--home-user--" / f"2026-06-28T04-20-31-629Z_{session_id}.jsonl"
    records: list[dict] = [{"type": "session", "version": 3, "id": session_id, "timestamp": "2026-06-28T04:20:31.629Z", "cwd": r"C:\home\user"}]
    for i, text in enumerate(texts):
        records.append({
            "type": "message", "id": f"{i:08x}", "parentId": records[-1].get("id") if i else None,
            "timestamp": f"2026-06-28T04:20:{32 + i:02d}.000Z",
            "message": {"role": "user" if i % 2 == 0 else "assistant", "content": [{"type": "text", "text": text}]},
        })
    JsonlFile(path).write(records, overwrite=True)


class T3WriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.pi_home = self.root / "pi"
        write_pi(self.pi_home, PI_SESSION, ["How do I reverse a list?", "Use list[::-1].", "Thanks, what about sorting?", "sorted(xs, reverse=True)."])
        self.t3_home = self.root / "new-t3"
        # Keep the live-directory guard away from the real ~/.t3 during tests.
        self._env = mock.patch.dict(os.environ, {"T3CODE_HOME": str(self.root / "live")})
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def _converter(self, provider: str = "codex") -> ToT3Converter:
        return ToT3Converter(PiStore(self.pi_home), T3Store(self.t3_home), SessionIdFactory(preserve_ids=True), provider)

    def _database(self) -> Path:
        return self.t3_home / "userdata" / "state.sqlite"

    def test_written_database_has_the_t3_legacy_schema_and_marker(self) -> None:
        converter = self._converter()
        plan = converter.plan(PI_SESSION)
        converter.write(plan)
        connection = sqlite3.connect(self._database())
        try:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            self.assertTrue({"projection_projects", "projection_threads", "projection_thread_messages", "orchestration_events"} <= tables)
            self.assertNotIn("orchestration_v2_projection_threads", tables)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM effect_sql_migrations").fetchone()[0], 54)
            self.assertEqual(connection.execute("PRAGMA application_id").fetchone()[0], _T3_UNISESSIONS_APPLICATION_ID)
        finally:
            connection.close()

    def test_round_trip_through_the_t3_reader(self) -> None:
        converter = self._converter()
        converter.write(converter.plan(PI_SESSION))
        store = T3Store(self.t3_home)
        summaries = store.list()
        self.assertEqual([s.session_id for s in summaries], [PI_SESSION])
        self.assertEqual(summaries[0].cwd, r"C:\home\user")
        self.assertEqual(summaries[0].message_count, 4)
        messages = MessageExtractor().from_t3(store.load(PI_SESSION))
        self.assertEqual(
            [(m.role, m.text) for m in messages],
            [
                ("user", "How do I reverse a list?"),
                ("assistant", "Use list[::-1]."),
                ("user", "Thanks, what about sorting?"),
                ("assistant", "sorted(xs, reverse=True)."),
            ],
        )

    def test_thread_uses_the_target_driver_and_safe_runtime(self) -> None:
        converter = self._converter("pi")
        converter.write(converter.plan(PI_SESSION))
        connection = sqlite3.connect(self._database())
        try:
            row = connection.execute("SELECT title, runtime_mode, interaction_mode, model_selection_json FROM projection_threads").fetchone()
        finally:
            connection.close()
        self.assertEqual(row[0], "How do I reverse a list?")
        self.assertEqual(row[1], "approval-required")
        self.assertEqual(row[2], "default")
        self.assertEqual(row[3], '{"instanceId":"pi","model":"default"}')

    def test_second_session_joins_the_same_new_database(self) -> None:
        write_pi(self.pi_home, "66666666-7777-4888-8999-aaaaaaaaaaaa", ["hello", "hi there"])
        converter = self._converter()
        converter.write(converter.plan(PI_SESSION))
        converter.write(converter.plan("66666666-7777-4888-8999-aaaaaaaaaaaa"))
        self.assertEqual(sorted(s.session_id for s in T3Store(self.t3_home).list()), sorted([PI_SESSION, "66666666-7777-4888-8999-aaaaaaaaaaaa"]))

    def test_existing_thread_is_detected_and_protected(self) -> None:
        converter = self._converter()
        plan = converter.plan(PI_SESSION)
        self.assertFalse(converter.destination_taken(plan))
        converter.write(plan)
        self.assertTrue(converter.destination_taken(converter.plan(PI_SESSION)))
        with self.assertRaises(FileExistsError):
            T3Store(self.t3_home).write(plan.destination, plan.records, overwrite=False)

    def test_overwrite_replaces_the_thread_without_duplicating_messages(self) -> None:
        converter = self._converter()
        converter.write(converter.plan(PI_SESSION))
        converter.write(converter.plan(PI_SESSION), overwrite=True)
        self.assertEqual(T3Store(self.t3_home).list()[0].message_count, 4)

    def test_has_changes_follows_the_source(self) -> None:
        converter = self._converter()
        self.assertTrue(converter.has_changes(PI_SESSION))
        converter.write(converter.plan(PI_SESSION))
        self.assertFalse(converter.has_changes(PI_SESSION))
        write_pi(self.pi_home, PI_SESSION, ["How do I reverse a list?", "Use list[::-1].", "Thanks, what about sorting?", "sorted(xs, reverse=True).", "One more question"])
        self.assertTrue(converter.has_changes(PI_SESSION))
        self.assertTrue(converter.has_changes("does-not-exist"))

    def test_refuses_a_home_that_already_has_statev2(self) -> None:
        userdata = self.t3_home / "userdata"
        userdata.mkdir(parents=True)
        (userdata / "statev2.sqlite").write_bytes(b"")
        converter = self._converter()
        with self.assertRaises(T3WriteError):
            converter.write(converter.plan(PI_SESSION))
        self.assertFalse((userdata / "state.sqlite").exists())

    def test_refuses_the_live_t3_directory(self) -> None:
        live_home = self.root / "live"
        converter = ToT3Converter(PiStore(self.pi_home), T3Store(live_home), SessionIdFactory(preserve_ids=True), "codex")
        with self.assertRaises(T3WriteError):
            converter.write(converter.plan(PI_SESSION))
        self.assertFalse((live_home / "userdata").exists())

    def test_refuses_to_modify_a_database_unisessions_did_not_create(self) -> None:
        userdata = self.t3_home / "userdata"
        userdata.mkdir(parents=True)
        foreign = sqlite3.connect(userdata / "state.sqlite")
        foreign.execute("CREATE TABLE unrelated (x INTEGER)")
        foreign.commit()
        foreign.close()
        converter = self._converter()
        with self.assertRaises(T3WriteError):
            converter.write(converter.plan(PI_SESSION))
        connection = sqlite3.connect(userdata / "state.sqlite")
        try:
            self.assertEqual([row[0] for row in connection.execute("SELECT name FROM sqlite_master")], ["unrelated"])
        finally:
            connection.close()

    def test_failed_write_leaves_no_partial_database(self) -> None:
        converter = self._converter()
        plan = converter.plan(PI_SESSION)
        broken = [dict(plan.records[0])]
        broken[0]["messages"] = [{"message_id": "x", "role": "user"}]  # no text or created_at
        with self.assertRaises(T3WriteError):
            T3Store(self.t3_home).write(self._database(), broken)
        self.assertFalse(self._database().exists())
        self.assertFalse(self._database().with_name("state.sqlite.unisessions-tmp").exists())

    def test_cli_refuses_to_write_without_an_explicit_t3_home(self) -> None:
        status = main(["--pi-agent-home", str(self.pi_home), "pi-to-t3", PI_SESSION, "--write"])
        self.assertEqual(status, 2)
        self.assertFalse((self.root / "live" / "userdata").exists())

    def test_cli_writes_into_a_new_home(self) -> None:
        status = main(["--pi-agent-home", str(self.pi_home), "--t3-home", str(self.t3_home), "pi-to-t3", PI_SESSION, "--write", "--t3-provider", "claudeAgent"])
        self.assertEqual(status, 0)
        self.assertEqual(T3Store(self.t3_home).list()[0].message_count, 4)


class T3RecordBuilderTests(unittest.TestCase):
    def test_drops_contextual_messages_and_keeps_order(self) -> None:
        messages = [
            TextMessage("user", "<environment_context>cwd</environment_context>", "2026-06-01T10:00:00.000Z", is_contextual=True),
            TextMessage("user", "real question", "2026-06-01T10:00:00.000Z"),
            TextMessage("assistant", "real answer", "2026-06-01T10:00:00.000Z", model="m", provider="p"),
        ]
        bundle = T3RecordBuilder("codex").build("thread-1", r"C:\repo", "2026-06-01T10:00:00.000Z", messages)[0]
        self.assertEqual([m["text"] for m in bundle["messages"]], ["real question", "real answer"])
        created = [m["created_at"] for m in bundle["messages"]]
        self.assertLess(created[0], created[1], "same-millisecond messages must be strictly ordered")

    def test_compaction_summary_becomes_an_assistant_message(self) -> None:
        messages = [TextMessage("system", "summary of earlier work", "2026-06-01T10:00:00.000Z", is_compaction=True)]
        bundle = T3RecordBuilder("codex").build("thread-1", "", "2026-06-01T10:00:00.000Z", messages)[0]
        self.assertEqual(bundle["messages"][0]["role"], "assistant")

    def test_title_and_workspace_fallbacks(self) -> None:
        bundle = T3RecordBuilder("codex").build("thread-1", "", "2026-06-01T10:00:00.000Z", [])[0]
        self.assertEqual(bundle["thread"]["title"], "Imported thread")
        self.assertTrue(bundle["project"]["workspace_root"])

    def test_ids_are_deterministic(self) -> None:
        messages = [TextMessage("user", "hi", "2026-06-01T10:00:00.000Z")]
        first = T3RecordBuilder("pi").build("thread-1", r"C:\repo", "2026-06-01T10:00:00.000Z", messages)
        second = T3RecordBuilder("pi").build("thread-1", r"C:\repo", "2026-06-01T10:00:00.000Z", messages)
        self.assertEqual(first, second)

    def test_rejects_unknown_providers(self) -> None:
        with self.assertRaises(ValueError):
            T3RecordBuilder("devin")
        self.assertEqual(set(T3_PROVIDER_DRIVERS), {"codex", "claudeAgent", "opencode", "pi", "grok"})


if __name__ == "__main__":
    unittest.main()
