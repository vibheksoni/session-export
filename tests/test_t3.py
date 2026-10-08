from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from session_sdk.converters import (
    T3ToClaudeConverter,
    T3ToCodexConverter,
    T3ToDevinConverter,
    T3ToFactoryConverter,
    T3ToFreebuffConverter,
    T3ToGrokConverter,
    T3ToOpenCodeConverter,
    T3ToPiConverter,
    T3ToWindsurfConverter,
)
from session_sdk.converters import MessageExtractor
from session_sdk.paths import SessionIdFactory
from session_sdk.search import SessionSearchEngine
from session_sdk.stores import (
    ClaudeStore,
    CodexStore,
    DevinStore,
    FactoryStore,
    FreebuffStore,
    GrokStore,
    OpenCodeStore,
    PiDcpStore,
    PiStore,
    T3Store,
    WindsurfStore,
)
from session_sdk.traces import build_trace

V2_THREADS_DDL = """
CREATE TABLE orchestration_v2_projection_threads (
    thread_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, title TEXT NOT NULL,
    default_provider TEXT NOT NULL, runtime_mode TEXT NOT NULL, interaction_mode TEXT NOT NULL,
    active_provider_thread_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    archived_at TEXT, deleted_at TEXT, payload_json TEXT NOT NULL
);
CREATE TABLE orchestration_v2_projection_messages (
    message_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, run_id TEXT, node_id TEXT,
    role TEXT NOT NULL, streaming INTEGER NOT NULL, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, payload_json TEXT NOT NULL
);
CREATE TABLE projection_projects (
    project_id TEXT PRIMARY KEY, title TEXT NOT NULL, workspace_root TEXT NOT NULL,
    default_model TEXT, scripts_json TEXT NOT NULL, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, deleted_at TEXT
);
"""

V1_DDL = """
CREATE TABLE projection_projects (
    project_id TEXT PRIMARY KEY, title TEXT NOT NULL, workspace_root TEXT NOT NULL,
    default_model TEXT, scripts_json TEXT NOT NULL, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, deleted_at TEXT
);
CREATE TABLE projection_threads (
    thread_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, title TEXT NOT NULL, model TEXT NOT NULL,
    branch TEXT, worktree_path TEXT, latest_turn_id TEXT, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, deleted_at TEXT, model_selection_json TEXT
);
CREATE TABLE projection_thread_messages (
    message_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, turn_id TEXT, role TEXT NOT NULL,
    text TEXT NOT NULL, is_streaming INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
"""

PROJECT_ROOT = r"C:\work\demo"


def make_v2_db(userdata: Path, threads: dict[str, dict], messages: dict[str, list[tuple[str, str]]]) -> Path:
    """Write a statev2.sqlite with projected threads and (role, text) messages."""
    userdata.mkdir(parents=True, exist_ok=True)
    path = userdata / "statev2.sqlite"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(V2_THREADS_DDL)
        connection.execute(
            "INSERT INTO projection_projects VALUES ('proj-1', 'demo', ?, NULL, '{}', '2026-06-01T09:00:00.000Z', '2026-06-01T09:00:00.000Z', NULL)",
            (PROJECT_ROOT,),
        )
        for thread_id, spec in threads.items():
            payload = {
                "id": thread_id,
                "projectId": "proj-1",
                "title": spec.get("title", "Thread"),
                "providerInstanceId": spec.get("instance", "codex"),
                "modelSelection": {"instanceId": spec.get("instance", "codex"), "model": spec.get("model", "gpt-5")},
                "worktreePath": spec.get("worktree"),
                "createdAt": spec.get("created", "2026-06-01T10:00:00.000Z"),
                "updatedAt": spec.get("created", "2026-06-01T10:00:00.000Z"),
            }
            connection.execute(
                "INSERT INTO orchestration_v2_projection_threads VALUES (?, 'proj-1', ?, 'codex', 'full-access', 'default', NULL, ?, ?, NULL, ?, ?)",
                (thread_id, payload["title"], payload["createdAt"], payload["updatedAt"], spec.get("deleted_at"), json.dumps(payload)),
            )
            for index, (role, text) in enumerate(messages.get(thread_id, [])):
                created = f"2026-06-01T10:{index:02d}:00.000Z"
                message_id = f"{thread_id}-m{index}"
                body = {
                    "id": message_id, "threadId": thread_id, "runId": None, "nodeId": None,
                    "role": role, "text": text, "attachments": [], "streaming": False,
                    "createdAt": created, "updatedAt": created, "createdBy": "user", "creationSource": "server",
                }
                connection.execute(
                    "INSERT INTO orchestration_v2_projection_messages VALUES (?, ?, NULL, NULL, ?, 0, ?, ?, ?)",
                    (message_id, thread_id, role, created, created, json.dumps(body)),
                )
        connection.commit()
    finally:
        connection.close()
    return path


def add_v2_message(path: Path, thread_id: str, role: str, text: str, index: int) -> None:
    connection = sqlite3.connect(path)
    try:
        created = f"2026-06-01T11:{index:02d}:00.000Z"
        message_id = f"{thread_id}-x{uuid4().hex}"
        body = {"id": message_id, "threadId": thread_id, "runId": None, "nodeId": None, "role": role,
                "text": text, "attachments": [], "streaming": False, "createdAt": created, "updatedAt": created,
                "createdBy": "user", "creationSource": "server"}
        connection.execute(
            "INSERT INTO orchestration_v2_projection_messages VALUES (?, ?, NULL, NULL, ?, 0, ?, ?, ?)",
            (message_id, thread_id, role, created, created, json.dumps(body)),
        )
        connection.commit()
    finally:
        connection.close()


def make_v1_db(userdata: Path, threads: dict[str, dict], messages: dict[str, list[tuple[str, str]]]) -> Path:
    """Write a legacy state.sqlite with plain projection tables."""
    userdata.mkdir(parents=True, exist_ok=True)
    path = userdata / "state.sqlite"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(V1_DDL)
        connection.execute(
            "INSERT INTO projection_projects VALUES ('proj-1', 'demo', ?, NULL, '{}', '2026-06-01T09:00:00.000Z', '2026-06-01T09:00:00.000Z', NULL)",
            (PROJECT_ROOT,),
        )
        for thread_id, spec in threads.items():
            selection = {"instanceId": spec.get("instance", "claudeAgent"), "model": spec.get("model", "claude-opus")}
            connection.execute(
                "INSERT INTO projection_threads VALUES (?, 'proj-1', ?, ?, NULL, ?, NULL, ?, ?, ?, ?)",
                (thread_id, spec.get("title", "Legacy"), spec.get("model", "claude-opus"), spec.get("worktree"),
                 spec.get("created", "2026-05-01T10:00:00.000Z"), spec.get("created", "2026-05-01T10:00:00.000Z"),
                 spec.get("deleted_at"), json.dumps(selection)),
            )
            for index, (role, text) in enumerate(messages.get(thread_id, [])):
                created = f"2026-05-01T10:{index:02d}:00.000Z"
                connection.execute(
                    "INSERT INTO projection_thread_messages VALUES (?, ?, NULL, ?, ?, 0, ?, ?)",
                    (f"{thread_id}-v1-{index}", thread_id, role, text, created, created),
                )
        connection.commit()
    finally:
        connection.close()
    return path


class T3StoreTests(unittest.TestCase):
    def test_v2_threads_list_with_cwd_counts_and_text_only_records(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / ".t3"
            make_v2_db(
                home / "userdata",
                {
                    "thread-a": {"title": "Fix login", "instance": "codex", "model": "gpt-5", "worktree": r"C:\work\wt"},
                    "thread-b": {"title": "Docs", "instance": "claudeAgent", "model": "claude-opus"},
                },
                {
                    "thread-a": [("user", "fix the login bug"), ("assistant", "done, see auth.ts")],
                    "thread-b": [("user", "write docs")],
                },
            )
            store = T3Store(home)
            summaries = {s.session_id: s for s in store.list()}
            self.assertEqual(set(summaries), {"thread-a", "thread-b"})
            self.assertEqual(summaries["thread-a"].cwd, r"C:\work\wt")
            self.assertEqual(summaries["thread-b"].cwd, PROJECT_ROOT)
            self.assertEqual(summaries["thread-a"].message_count, 2)
            self.assertEqual(summaries["thread-a"].provider, "t3")

            session = store.load("thread-a")
            self.assertEqual(session.cwd, r"C:\work\wt")
            self.assertEqual([r["role"] for r in session.records], ["user", "assistant"])
            messages = MessageExtractor().from_t3(session)
            self.assertEqual([m.text for m in messages], ["fix the login bug", "done, see auth.ts"])
            self.assertEqual(messages[1].provider, "codex")
            self.assertEqual(messages[1].model, "gpt-5")

    def test_legacy_v1_thread_is_read_and_v2_wins_on_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / ".t3"
            make_v2_db(home / "userdata", {"shared": {"title": "New copy"}}, {"shared": [("user", "from v2")]})
            make_v1_db(
                home / "userdata",
                {"shared": {"title": "Old copy"}, "only-v1": {"title": "Legacy", "worktree": r"C:\old"}},
                {"shared": [("user", "from v1")], "only-v1": [("user", "legacy question"), ("assistant", "legacy answer")]},
            )
            store = T3Store(home)
            summaries = {s.session_id: s for s in store.list()}
            self.assertEqual(set(summaries), {"shared", "only-v1"})
            self.assertEqual(store.load("shared").records[0]["text"], "from v2")
            legacy = store.load("only-v1")
            self.assertEqual(legacy.cwd, r"C:\old")
            self.assertEqual(legacy.records[1]["model"], "claude-opus")
            self.assertEqual(legacy.records[1]["instance_id"], "claudeAgent")
            self.assertEqual(summaries["only-v1"].message_count, 2)

    def test_deleted_threads_are_hidden(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / ".t3"
            make_v2_db(
                home / "userdata",
                {"kept": {}, "gone": {"deleted_at": "2026-06-02T00:00:00.000Z"}},
                {"kept": [("user", "hi")], "gone": [("user", "bye")]},
            )
            ids = {s.session_id for s in T3Store(home).list()}
            self.assertEqual(ids, {"kept"})

    def test_store_never_writes_to_the_database(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / ".t3"
            db = make_v2_db(home / "userdata", {"t": {}}, {"t": [("user", "hello")]})
            before = db.read_bytes()
            store = T3Store(home)
            store.list()
            store.load("t")
            self.assertEqual(db.read_bytes(), before)
            connection = T3Store._connect(db)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    connection.execute("DELETE FROM orchestration_v2_projection_threads")
            finally:
                connection.close()

    def test_missing_thread_raises(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = T3Store(Path(root) / ".t3")
            self.assertEqual(store.list(), [])
            with self.assertRaises(FileNotFoundError):
                store.load("nope")


class T3ConverterTests(unittest.TestCase):
    """Every T3-to-X converter: plan, write, then has_changes before and after the source grows."""

    @staticmethod
    def _targets(root: Path) -> dict[str, tuple[object, Path]]:
        pi = PiStore(root / "pi")
        dcp = PiDcpStore(root / "pi-dcp")
        return {
            "pi": (T3ToPiConverter, (pi, dcp)),
            "codex": (T3ToCodexConverter, (CodexStore(root / "codex"),)),
            "opencode": (T3ToOpenCodeConverter, (OpenCodeStore(root / "opencode"),)),
            "claude": (T3ToClaudeConverter, (ClaudeStore(root / "claude"),)),
            "devin": (T3ToDevinConverter, (DevinStore(root / "devin"),)),
            "factory": (T3ToFactoryConverter, (FactoryStore(root / "factory"),)),
            "windsurf": (T3ToWindsurfConverter, (WindsurfStore(root / "windsurf"),)),
            "grok": (T3ToGrokConverter, (GrokStore(root / "grok"),)),
            "freebuff": (T3ToFreebuffConverter, (FreebuffStore(root / "freebuff"),)),
        }

    # Targets whose has_changes cannot be decided from the destination (see converters.py).
    ALWAYS_CHANGED = {"devin", "windsurf", "freebuff"}

    def test_every_target_plans_writes_and_detects_changes(self) -> None:
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            t3_home = root / ".t3"
            db = make_v2_db(
                t3_home / "userdata",
                {"chat-1": {"title": "Chat", "instance": "codex", "model": "gpt-5"}},
                {"chat-1": [("user", "first question"), ("assistant", "first answer")]},
            )
            t3 = T3Store(t3_home)
            ids = SessionIdFactory(preserve_ids=True)
            for name, (cls, stores) in self._targets(root / "out").items():
                with self.subTest(target=name):
                    converter = cls(t3, *stores, ids)
                    plan = converter.plan("chat-1")
                    self.assertEqual(plan.source.provider, "t3")
                    self.assertGreater(len(plan.records), 0)
                    self.assertTrue(converter.has_changes("chat-1"))
                    converter.write(plan, overwrite=True)
                    self.assertTrue(plan.destination.exists())
                    if name not in self.ALWAYS_CHANGED:
                        self.assertFalse(converter.has_changes("chat-1"), "unchanged thread should not be rewritten")
                    add_v2_message(db, "chat-1", "user", "a follow-up", index=1)
                    self.assertTrue(converter.has_changes("chat-1"), "a new message must be detected")

    def test_missing_source_is_a_change(self) -> None:
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            t3 = T3Store(root / ".t3")
            converter = T3ToPiConverter(t3, PiStore(root / "pi"), PiDcpStore(root / "pi-dcp"), SessionIdFactory(preserve_ids=True))
            self.assertTrue(converter.has_changes("does-not-exist"))


class T3TraceAndSearchTests(unittest.TestCase):
    def test_trace_export_from_t3(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / ".t3"
            make_v2_db(home / "userdata", {"t": {}}, {"t": [("user", "question"), ("assistant", "answer")]})
            session = T3Store(home).load("t")
            records = build_trace("sts", session, MessageExtractor().from_t3(session))
            self.assertGreater(len(records), 0)

    def test_search_finds_threads_and_sees_new_messages(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / ".t3"
            db = make_v2_db(home / "userdata", {"alpha": {}, "beta": {}}, {"alpha": [("user", "zebra question")], "beta": [("user", "nothing here")]})
            engine = SessionSearchEngine(
                CodexStore(Path(root) / ".codex"),
                PiStore(Path(root) / ".pi"),
                OpenCodeStore(Path(root) / "opencode"),
                Path(root) / "search.sqlite",
                t3=T3Store(home),
            )
            try:
                engine.refresh_index(provider="t3")
                hits = engine.search(query="zebra", provider="t3", stale_policy="skip")["results"]
                self.assertEqual({h["session_id"] for h in hits}, {"alpha"})

                add_v2_message(db, "beta", "user", "zebra follow-up", index=1)
                engine.refresh_index(provider="t3")
                hits = engine.search(query="zebra", provider="t3", stale_policy="skip")["results"]
                self.assertEqual({h["session_id"] for h in hits}, {"alpha", "beta"})

                # Both threads share one database file. Refreshing must not prune either one.
                status = engine.index_status(provider="t3")["t3"]
                self.assertEqual(status["indexed_sessions"], 2)
                self.assertEqual(status["missing_sessions"], 0)
            finally:
                engine.close()


if __name__ == "__main__":
    unittest.main()
