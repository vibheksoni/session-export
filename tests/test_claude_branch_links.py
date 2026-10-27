"""Claude Code can write a parent link that points forward in the file: a
compaction boundary's logical parent is an attachment whose parentUuid is a
message written after it. Following it looped the branch walk, and every
message before the boundary disappeared from extraction and search
(a 417K-line session kept only its last week, 2026-10-27)."""

import json
import tempfile
import unittest
from pathlib import Path

from session_sdk.converters import MessageExtractor
from session_sdk.stores import ClaudeStore

SESSION = "aaaaaaaa-bbbb-cccc-dddd-111111111111"


def _record(**fields: object) -> str:
    base = {"isSidechain": False, "cwd": "C:\\Projects\\test", "sessionId": SESSION, "version": "2.1.140"}
    base.update(fields)
    return json.dumps(base)


class ForwardParentLinkTests(unittest.TestCase):
    def test_messages_before_a_looping_compaction_are_kept(self) -> None:
        lines = [
            _record(type="user", uuid="u1", parentUuid=None, timestamp="2026-10-01T00:00:00Z",
                    message={"role": "user", "content": "the early question"}),
            _record(type="assistant", uuid="a1", parentUuid="u1", timestamp="2026-10-01T00:00:01Z",
                    message={"role": "assistant", "id": "m1", "content": [{"type": "text", "text": "the early answer"}]}),
            # Written before u2, yet it names u2 as its parent: the broken forward link.
            _record(type="attachment", uuid="att", parentUuid="u2", timestamp="2026-10-01T00:00:02Z", attachment={}),
            _record(type="user", uuid="u2", parentUuid="b1", timestamp="2026-10-01T00:00:03Z",
                    message={"role": "user", "content": "the question that triggered compaction"}),
            _record(type="system", subtype="compact_boundary", uuid="b1", parentUuid=None, logicalParentUuid="att",
                    timestamp="2026-10-01T00:00:04Z", content="Conversation compacted"),
            _record(type="user", uuid="u3", parentUuid="u2", timestamp="2026-10-01T00:00:05Z",
                    message={"role": "user", "content": "the late question"}),
            _record(type="assistant", uuid="a3", parentUuid="u3", timestamp="2026-10-01T00:00:06Z",
                    message={"role": "assistant", "id": "m3", "content": [{"type": "text", "text": "the late answer"}]}),
        ]
        with tempfile.TemporaryDirectory() as root_name:
            claude_home = Path(root_name) / ".claude"
            project = claude_home / "projects" / "C--projects-test"
            project.mkdir(parents=True)
            (project / f"{SESSION}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
            texts = [m.text for m in MessageExtractor().from_claude(ClaudeStore(claude_home).load(SESSION))]
        self.assertIn("the early question", texts)
        self.assertIn("the early answer", texts)
        self.assertIn("the late answer", texts)
        self.assertLess(texts.index("the early answer"), texts.index("the late answer"))


if __name__ == "__main__":
    unittest.main()
