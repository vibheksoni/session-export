from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from session_sdk.jsonl import JsonlFile
from session_sdk.search import SessionSearchEngine, SessionSearchIndex
from session_sdk.stores import CodexStore, OpenCodeStore, PiStore


def write_pi(home: Path, session_id: str, texts: list[str]) -> Path:
    path = home / "sessions" / "--C--home-user--" / f"2026-06-28T04-20-31-629Z_{session_id}.jsonl"
    records: list[dict] = [{"type": "session", "version": 3, "id": session_id, "timestamp": "2026-06-28T04:20:31.629Z", "cwd": r"C:\home\user"}]
    for i, text in enumerate(texts):
        records.append({
            "type": "message", "id": f"{i:08x}", "parentId": records[-1].get("id"), "timestamp": f"2026-06-28T04:20:{32 + i:02d}.000Z",
            "message": {"role": "user" if i % 2 == 0 else "assistant", "content": [{"type": "text", "text": text}]},
        })
    JsonlFile(path).write(records, overwrite=True)
    return path


class IndexV2Tests(unittest.TestCase):
    def engine(self, root: Path, index: Path | None = None) -> SessionSearchEngine:
        engine = SessionSearchEngine(CodexStore(root / ".codex"), PiStore(root / ".pi" / "agent"), OpenCodeStore(root / "opencode"), index)
        engine.PATH_TTL = 0  # rescan the session folders on every call
        return engine

    def hits(self, engine: SessionSearchEngine, query: str) -> int:
        return len(engine.search(query=query, provider="pi", stale_policy="refresh")["results"])

    def test_reindexing_a_grown_session_replaces_its_rows(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            home = root / ".pi" / "agent"
            sid = "11111111-1111-7111-8111-111111111111"
            write_pi(home, sid, ["alpha bravo", "charlie"])
            engine = self.engine(root)
            self.assertEqual(self.hits(engine, "alpha"), 1)
            write_pi(home, sid, ["alpha bravo", "charlie", "delta echo", "alpha again"])
            self.assertEqual(self.hits(engine, "delta"), 1)
            self.assertEqual(self.hits(engine, "alpha"), 2)
            db = engine._index._db
            self.assertEqual(db.execute("SELECT count(*) FROM messages").fetchone()[0], 4)
            self.assertEqual(db.execute("SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'alpha'").fetchone()[0], 2)

    def test_sessions_created_after_the_first_search_are_found(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            home = root / ".pi" / "agent"
            write_pi(home, "22222222-2222-7222-8222-222222222222", ["first session"])
            engine = self.engine(root)
            self.assertEqual(self.hits(engine, "zulu"), 0)
            write_pi(home, "33333333-3333-7333-8333-333333333333", ["zulu shows up later"])
            self.assertEqual(self.hits(engine, "zulu"), 1)

    def test_vanished_files_do_not_break_status_and_deleted_sessions_are_pruned(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            home = root / ".pi" / "agent"
            path = write_pi(home, "44444444-4444-7444-8444-444444444444", ["yankee"])
            engine = self.engine(root)
            self.assertEqual(self.hits(engine, "yankee"), 1)
            summaries = engine._scoped_summaries(engine._stores["pi"] if hasattr(engine, "_stores") else PiStore(home), cwd=None, cwd_match="exact", workers=1)
            path.unlink()
            status = engine._index.status(summaries)  # listed before the file vanished: must not raise
            self.assertEqual(status["sessions"], len(summaries) - 1)
            engine.refresh_index(provider="pi")
            self.assertEqual(engine._index._db.execute("SELECT count(*) FROM messages").fetchone()[0], 0)

    def test_a_v1_index_is_replaced_by_the_v2_schema(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "search.sqlite"
            db = sqlite3.connect(path)
            db.executescript("""
                CREATE TABLE sessions (path TEXT PRIMARY KEY, provider TEXT, session_id TEXT, cwd TEXT, timestamp TEXT, mtime_ns INTEGER, size INTEGER, message_count INTEGER);
                CREATE VIRTUAL TABLE messages_fts USING fts5(provider UNINDEXED, session_id UNINDEXED, path UNINDEXED, cwd UNINDEXED, timestamp UNINDEXED, message_index UNINDEXED, role UNINDEXED, message_type UNINDEXED, text, tokenize='unicode61');
                INSERT INTO sessions VALUES ('x', 'pi', 's', 'c', 't', 1, 1, 1);
            """)
            db.commit()
            db.close()
            index = SessionSearchIndex(path)
            db = index._db
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT count(*) FROM sessions").fetchone()[0], 0)
            self.assertIsNotNone(db.execute("SELECT 1 FROM sqlite_master WHERE name = 'messages'").fetchone())
            index.close()

    def test_listing_reuses_summaries_until_a_file_changes(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            home = root / ".pi" / "agent"
            path = write_pi(home, "55555555-5555-7555-8555-555555555555", ["one"])
            store = PiStore(home)
            calls = 0
            original = store._safe_metadata

            def counting(p):
                nonlocal calls
                calls += 1
                return original(p)

            store._safe_metadata = counting  # type: ignore[method-assign]
            store.list_metadata()
            store.invalidate_paths()
            store.list_metadata()
            self.assertEqual(calls, 1)
            write_pi(home, "55555555-5555-7555-8555-555555555555", ["one", "two", "three"])
            store.invalidate_paths()
            store.list_metadata()
            self.assertEqual(calls, 2)
            self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()


class PathKeyTests(unittest.TestCase):
    def test_path_keys_keep_case_when_the_filesystem_is_case_sensitive(self) -> None:
        # On Linux normcase does not fold case, so the key must still name the real file.
        with mock.patch("session_sdk.search.os.path.normcase", side_effect=lambda value: value):
            key = SessionSearchIndex._path_key(Path("/Tmp/Sessions/Rollout.jsonl"))
        self.assertEqual(key, os.path.abspath("/Tmp/Sessions/Rollout.jsonl"))
        self.assertIn("Sessions", key)

