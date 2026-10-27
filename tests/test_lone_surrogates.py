"""A lone UTF-16 surrogate escape (half an emoji, written when an agent cuts
text mid-character) must not make a session unreadable. orjson refuses such a
line; the loaders fall back to the standard parser and replace the half with
U+FFFD so the text stays encodable for SQLite and file writes."""

import tempfile
import unittest
from pathlib import Path

from session_sdk.jsonl import JsonlFile, _loads
from session_sdk.stores import ClaudeStore

HALF_EMOJI = "\\" + "ud83d"  # the six characters of the JSON escape, as written on disk


class LoneSurrogateTests(unittest.TestCase):
    def test_loads_replaces_a_lone_surrogate(self) -> None:
        value = _loads(('{"text":"cut ' + HALF_EMOJI + ' here","ok":"fine"}').encode("utf-8"))
        self.assertEqual(value, {"text": "cut \ufffd here", "ok": "fine"})
        value["text"].encode("utf-8")  # encodable again

    def test_loads_keeps_a_whole_emoji(self) -> None:
        pair = "\\" + "ud83d" + "\\" + "ude00"
        self.assertEqual(_loads(('{"text":"' + pair + '"}').encode("utf-8")), {"text": "\U0001F600"})

    def test_jsonl_file_reads_the_line(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "s.jsonl"
            path.write_text('{"a":1}\n{"text":"x' + HALF_EMOJI + '"}\n', encoding="utf-8")
            self.assertEqual(JsonlFile(path).read(), [{"a": 1}, {"text": "x\ufffd"}])

    def test_claude_session_with_a_lone_surrogate_still_loads(self) -> None:
        with tempfile.TemporaryDirectory() as root_name:
            claude_home = Path(root_name) / ".claude"
            project = claude_home / "projects" / "C--projects-test"
            project.mkdir(parents=True)
            session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            common = '"cwd":"C:\\\\Projects\\\\test","sessionId":"' + session_id + '","version":"2.1.140","userType":"external","entrypoint":"cli"'
            lines = [
                '{"parentUuid":null,"isSidechain":false,"type":"user","message":{"role":"user","content":"hello ' + HALF_EMOJI + '"},"uuid":"aaa-111","timestamp":"2026-05-23T03:12:51.338Z",' + common + "}",
                '{"parentUuid":"aaa-111","isSidechain":false,"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"hi there"}],"model":"claude-opus-4-7"},"uuid":"bbb-222","timestamp":"2026-05-23T03:12:52.000Z",' + common + "}",
            ]
            (project / f"{session_id}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

            store = ClaudeStore(claude_home)
            self.assertEqual([s.session_id for s in store.list()], [session_id])
            session = store.load(session_id)
            self.assertEqual(session.session_id, session_id)


if __name__ == "__main__":
    unittest.main()
