import json
from pathlib import Path
import tempfile
import unittest

from eira_harness.agent import COMPACT_PROMPT, UPDATE_NOTE, Agent, Limits
from eira_harness.security import HarnessError, Workspace
from eira_harness.store import Store
from eira_harness.tools import Policy, Toolbox


def call(name, args, call_id):
    return {"role": "assistant", "content": None, "tool_calls": [{"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}]}


def text(value):
    return {"role": "assistant", "content": value}


class Scripted:
    model = "scripted"

    def __init__(self, *responses):
        self.responses, self.seen = list(responses), []

    def complete(self, messages, tools):
        self.seen.append(json.loads(json.dumps(messages)))
        return self.responses.pop(0), {"total_tokens": 10}


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.session = self.store.create("test")
        self.tools = Toolbox(Workspace(self.root), self.store, Policy(approve_writes=True), self.session)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def agent(self, provider, **limits):
        return Agent(provider, self.store, self.tools, limits=Limits(**limits))

    def test_requests_are_append_only_within_a_run(self):
        (self.root / "a.txt").write_text("alpha\n")
        provider = Scripted(call("read_file", {"path": "a.txt"}, "c1"),
                            call("edit_file", {"path": "a.txt", "old_string": "alpha", "new_string": "beta"}, "c2"),
                            text("Changed."))
        self.assertEqual(self.agent(provider).run("Change alpha to beta")["status"], "completed")
        for before, after in zip(provider.seen, provider.seen[1:]):
            self.assertEqual(after[:len(before)], before)
        self.assertEqual((self.root / "a.txt").read_text(), "beta\n")

    def test_system_prompt_is_frozen_and_changes_are_appended(self):
        first = Scripted(text("One."))
        self.agent(first).run("First task")
        system = first.seen[0][0]["content"]
        self.store.remember("style", "terse")
        (self.root / "EIRA.md").write_text("Run the unit tests before finishing.\n")
        second = Scripted(text("Two."))
        self.agent(second).run("Second task")
        request = second.seen[0]
        self.assertEqual(request[0]["content"], system)
        self.assertEqual(request[:len(first.seen[0])], first.seen[0])
        note = request[-2]["content"]
        self.assertTrue(note.startswith(UPDATE_NOTE))
        self.assertIn("terse", note)
        self.assertIn("Run the unit tests", note)
        self.assertEqual(request[-1]["content"], "Second task")
        third = Scripted(text("Three."))
        self.agent(third).run("Third task")
        self.assertEqual(sum(m["content"].startswith(UPDATE_NOTE) for m in third.seen[0] if m["role"] == "user"), 1)

    def test_compaction_summarizes_and_keeps_the_journal(self):
        (self.root / "big.txt").write_text(("x" * 79 + "\n") * 100)
        probe = self.agent(Scripted())
        probe.prepare_session()
        base = probe.context_size(probe.context())
        provider = Scripted(call("read_file", {"path": "big.txt"}, "c1"),
                            call("read_file", {"path": "big.txt", "start_line": 1}, "c2"),
                            text("Summary: big.txt holds 100 lines of x."), text("Done."))
        limit = base + 19_000  # one read fits under 80%, two cross it, the fork still fits
        result = self.agent(provider, max_context_chars=limit).run("Inspect big.txt carefully")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["tokens"], 40)
        fork, after = provider.seen[2], provider.seen[3]
        self.assertEqual(fork[-1]["content"], COMPACT_PROMPT)
        self.assertEqual(fork[:-1][:len(provider.seen[1])], provider.seen[1])
        self.assertEqual(len(after), 2)
        self.assertEqual(after[0], provider.seen[0][0])
        self.assertIn("Summary: big.txt holds 100 lines", after[1]["content"])
        self.assertIn("Inspect big.txt carefully", after[1]["content"])
        journal = self.store.messages(self.session)
        self.assertEqual([m["role"] for m in journal], ["user", "assistant", "tool", "assistant", "tool", "user", "assistant"])
        self.assertIn("eira_compaction", journal[5])
        kinds = [event["kind"] for event in self.store.events(self.session)]
        self.assertIn("context_compacted", kinds)

    def test_oversized_compaction_request_elides_tool_results_for_that_request_only(self):
        (self.root / "big.txt").write_text(("y" * 79 + "\n") * 150)
        probe = self.agent(Scripted())
        probe.prepare_session()
        base = probe.context_size(probe.context())
        provider = Scripted(call("read_file", {"path": "big.txt"}, "c1"),
                            call("read_file", {"path": "big.txt", "start_line": 2}, "c2"),
                            text("Summary."), text("Done."))
        limit = base + 20_000
        self.assertEqual(self.agent(provider, max_context_chars=limit).run("Read twice")["status"], "completed")
        fork = provider.seen[2]
        self.assertIn("elided", json.dumps(fork))
        self.assertNotIn("elided", json.dumps(self.store.messages(self.session)))

    def test_compaction_disabled_or_unhelpful_stops_with_history_preserved(self):
        (self.root / "big.txt").write_text(("z" * 79 + "\n") * 100)
        probe = self.agent(Scripted())
        probe.prepare_session()
        base = probe.context_size(probe.context())
        provider = Scripted(call("read_file", {"path": "big.txt"}, "c1"), text("never"))
        with self.assertRaisesRegex(HarnessError, "Context limit"):
            self.agent(provider, max_context_chars=base + 4_000, compact=False).run("Read it")
        self.assertEqual(len(provider.seen), 1)
        empty = Scripted(call("read_file", {"path": "big.txt", "start_line": 1}, "c2"), call("read_file", {"path": "big.txt", "start_line": 2}, "c3"), text(""))
        with self.assertRaisesRegex(HarnessError, "did not return a summary"):
            self.agent(empty, max_context_chars=base + 30_000).run("Read again")
        self.assertEqual(self.store.messages(self.session)[0]["content"], "Read it")

    def test_large_results_are_shrunk_to_valid_json(self):
        agent = self.agent(Scripted(), max_tool_output_chars=2_000)
        encoded = agent.fit({"ok": True, "result": {"path": "big.txt", "content": "q" * 50_000, "sha256": "abc"}})
        data = json.loads(encoded)
        self.assertLessEqual(len(encoded), 2_000)
        self.assertTrue(data["truncated"])
        self.assertEqual(data["result"]["path"], "big.txt")
        self.assertEqual(data["result"]["sha256"], "abc")
        self.assertIn("characters truncated", data["result"]["content"])

    def test_repeated_identical_calls_get_a_warning(self):
        (self.root / "a.txt").write_text("same\n")
        provider = Scripted(*[call("read_file", {"path": "a.txt"}, f"c{i}") for i in range(3)], text("Stop."))
        self.agent(provider).run("Loop")
        results = [json.loads(m["content"]) for m in self.store.messages(self.session) if m["role"] == "tool"]
        self.assertNotIn("repeat_warning", results[1])
        self.assertIn("3 times", results[2]["repeat_warning"])

    def test_tool_progress_events_describe_arguments(self):
        (self.root / "a.txt").write_text("x\n")
        events = []
        Agent(Scripted(call("read_file", {"path": "a.txt"}, "c1"), text("ok")), self.store, self.tools,
              emit=events.append).run("Read")
        started = next(e for e in events if e["event"] == "tool_started")
        self.assertEqual(started["detail"], "a.txt")
        completed = next(e for e in events if e["event"] == "run_completed")
        self.assertIn("seconds", completed)


if __name__ == "__main__":
    unittest.main()
