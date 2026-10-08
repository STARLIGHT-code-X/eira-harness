"""Head-and-tail tool output with full redacted copies saved for read_output."""
import io
import json
import os
from pathlib import Path
import random
import stat
import tempfile
import time
import unittest
from unittest.mock import patch

from eira_harness import outputs
from eira_harness.agent import Agent, Limits
from eira_harness.outputs import OutputStore, head_tail
from eira_harness.security import HarnessError, Redactor, Workspace
from eira_harness.store import Store
from eira_harness.terminal import Terminal
from eira_harness.text import split_lines
from eira_harness.tools import SHELL_CAPTURE_BYTES, Policy, Toolbox

try:
    from fakes import fake_docker
except ImportError:  # run as tests.test_outputs from the repository root
    from tests.fakes import fake_docker

POSIX_SH = os.name == "posix" and Path("/bin/sh").exists()


def call(name, args, call_id):
    return {"role": "assistant", "content": None, "tool_calls": [{"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}]}


class HeadTailTests(unittest.TestCase):
    def test_keeps_both_ends_with_an_exact_marker(self):
        text = "".join(f"line {i}\n" for i in range(1, 100_001))
        visible, info = head_tail(text, 20_000, "o-0123456789ab")
        self.assertTrue(visible.startswith("line 1\nline 2\n"))
        self.assertTrue(visible.endswith("line 100000\n"))
        head, rest = visible.split("\n…[", 1)
        mark, tail = rest.split("]…\n", 1)
        self.assertTrue(text.startswith(head) and text.endswith(tail))
        omitted = len(text) - len(head) - len(tail)
        self.assertEqual(info["omitted_chars"], omitted)
        lines = [line for line, _ in split_lines(text)]
        first = len(split_lines(head)) + 1
        last = len(lines) - len(split_lines(tail))
        self.assertEqual((info["start_line"], info["end_line"]), (first, last))
        self.assertEqual(mark, f'{omitted:,} characters truncated (lines {first:,}-{last:,}); '
                               f'read_output(output_id="o-0123456789ab", start_line={first}) shows them')
        # Both cuts landed on line boundaries.
        self.assertEqual(head.splitlines()[-1], f"line {first - 1}")
        self.assertEqual(tail.splitlines()[0], f"line {last + 1}")
        marker = visible[len(head):len(visible) - len(tail)]
        self.assertLessEqual(len(visible), 20_000 + len(marker))
        self.assertGreater(len(head) + len(tail), 19_600)

    def test_short_text_is_unchanged(self):
        self.assertEqual(head_tail("abc\n" * 10, 40), ("abc\n" * 10, None))
        self.assertEqual(head_tail("", 10), ("", None))

    def test_code_points_survive_cuts_without_line_boundaries(self):
        text = "é🙂" * 30_000
        visible, info = head_tail(text, 1_000)
        head, tail = visible.split("\n…[", 1)[0], visible.rsplit("]…\n", 1)[1]
        self.assertTrue(text.startswith(head) and text.endswith(tail))
        self.assertEqual(len(head) + len(tail), 1_000)
        self.assertIn("é", tail)
        self.assertIn("🙂", head)
        visible.encode("utf-8")  # no lone surrogates
        self.assertIn("could not be saved", visible)
        self.assertEqual((info["start_line"], info["end_line"]), (1, 1))

    def test_crlf_and_cr_count_as_one_line_each(self):
        text = "".join(f"r{i}\r\n" if i % 2 else f"c{i}\r" for i in range(1, 5001))
        visible, info = head_tail(text, 2_000)
        head = visible.split("\n…[", 1)[0]
        self.assertTrue(head.endswith(("\r\n", "\r")))
        self.assertEqual(info["start_line"], len(split_lines(head)) + 1)


    def test_line_helpers_match_split_lines(self):
        rng = random.Random(7)
        for _ in range(300):
            text = "".join(rng.choice(["a", "\r", "\n", "\r\n", "\x0c", "é"]) for _ in range(rng.randrange(0, 40)))
            pairs = split_lines(text)
            self.assertEqual(outputs.count_lines(text), len(pairs))
            first = rng.randrange(1, len(pairs) + 2)
            last = rng.randrange(first, len(pairs) + 3)
            expected = [(n, a, b) for n, (a, b) in enumerate(pairs, 1) if first <= n <= last]
            self.assertEqual(list(outputs._iter_lines(text, first, last)), expected)
            for index in range(len(text)):
                self.assertEqual(outputs._line_of(text, index), len(split_lines(text[:index + 1])) if not (
                    text[index] == "\n" and index and text[index - 1] == "\r") else len(split_lines(text[:index])))


class OutputCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.session = self.store.create("outputs")
        self.policy = Policy(approve=lambda name, detail: True, shell_mode="docker")
        self.tools = Toolbox(Workspace(self.root), self.store, self.policy, self.session)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def saved_path(self, output_id, session=None):
        return self.root / ".eira" / "outputs" / (session or self.session) / f"{output_id}.txt"


class OutputStoreTests(OutputCase):
    def test_paging_query_and_validation(self):
        saved = self.tools.outputs.save("".join(f"row {i}\n" for i in range(1, 1001)), "test")
        self.assertEqual((saved["lines"], saved["bytes"]), (1000, len("".join(f"row {i}\n" for i in range(1, 1001)))))
        page = self.tools.call("read_output", {"output_id": saved["output_id"]})
        self.assertEqual((page["start_line"], page["end_line"], page["next_start_line"]), (1, 400, 401))
        self.assertTrue(page["content"].startswith("row 1\n") and page["content"].endswith("row 400\n"))
        page = self.tools.call("read_output", {"output_id": saved["output_id"], "start_line": 990, "end_line": 995})
        self.assertEqual(page["content"], "".join(f"row {i}\n" for i in range(990, 996)))
        self.assertNotIn("next_start_line", page)
        found = self.tools.call("read_output", {"output_id": saved["output_id"], "query": "row 99"})
        self.assertEqual([m["line"] for m in found["matches"]][:3], [99, 990, 991])
        self.assertFalse(found["truncated"])
        many = self.tools.read_output(saved["output_id"], query="row")
        self.assertEqual((len(many["matches"]), many["truncated"]), (100, True))
        for bad in ("o-../../x", "o-XYZ", "o-0123456789AB", "../state.db", "o-0123456789abc"):
            with self.subTest(bad=bad), self.assertRaisesRegex(HarnessError, "output_id must be"):
                self.tools.call("read_output", {"output_id": bad})
        with self.assertRaises(HarnessError):
            self.tools.call("read_output", {"output_id": "o-" + "0" * 30})
        with self.assertRaisesRegex(HarnessError, "past the end"):
            self.tools.read_output(saved["output_id"], start_line=2000)
        with self.assertRaisesRegex(HarnessError, "before start_line"):
            self.tools.read_output(saved["output_id"], start_line=5, end_line=4)

    def test_long_lines_are_paged_by_characters(self):
        saved = self.tools.outputs.save(("x" * 1000 + "\n") * 100 + "y" * 50_000, "test")
        page = self.tools.read_output(saved["output_id"])
        self.assertLessEqual(len(page["content"]), 24_000)
        self.assertEqual(page["next_start_line"], page["end_line"] + 1)
        last = self.tools.read_output(saved["output_id"], start_line=101)
        self.assertTrue(last["line_truncated"])
        self.assertEqual(len(last["content"]), 24_000)

    def test_sessions_are_isolated(self):
        saved = self.tools.outputs.save("only in A\n", "test")
        other = Toolbox(Workspace(self.root), self.store, self.policy, self.store.create("B"))
        with self.assertRaisesRegex(HarnessError, "not available in this session"):
            other.read_output(saved["output_id"])
        self.assertEqual(self.tools.read_output(saved["output_id"])["content"], "only in A\n")

    def test_symlinked_directories_are_refused(self):
        (self.root / "elsewhere").mkdir()
        (self.root / ".eira" / "outputs").symlink_to(self.root / "elsewhere")
        with self.assertRaisesRegex(HarnessError, "symlink"):
            self.tools.outputs.save("x", "test")
        self.assertEqual(list((self.root / "elsewhere").iterdir()), [])
        self.assertIsNone(self.tools._save_output("x", "test"))

    def test_session_cap_evicts_oldest(self):
        with patch.object(outputs, "SESSION_CAP_BYTES", 10_000):
            first = self.tools.outputs.save("a" * 6_000, "test")["output_id"]
            second = self.tools.outputs.save("b" * 6_000, "test")["output_id"]
            third = self.tools.outputs.save("c" * 20_000, "test")["output_id"]
        for evicted in (first, second):
            with self.assertRaisesRegex(HarnessError, "expired or never saved"):
                self.tools.read_output(evicted)
        self.assertEqual(self.tools.read_output(third)["content"], "c" * 20_000)

    def test_old_outputs_are_swept_from_every_session(self):
        other = OutputStore(self.root / ".eira", "b" * 16)
        recent = OutputStore(self.root / ".eira", "c" * 16)
        kept = recent.save("recent\n", "test")["output_id"]
        stale = other.save("old\n", "test")["output_id"]
        old = time.time() - 8 * 24 * 3600
        os.utime(self.saved_path(stale, "b" * 16), (old, old))
        os.utime(self.saved_path(stale, "b" * 16).parent, (old, old))
        self.tools.outputs.save("new\n", "test")
        self.assertFalse(self.saved_path(stale, "b" * 16).exists())
        self.assertFalse((self.root / ".eira" / "outputs" / ("b" * 16)).exists())
        self.assertEqual(recent.read(kept)["content"], "recent\n")

    def test_saved_copy_is_capped(self):
        saved = self.tools.outputs.save("z" * (5 * 1024 * 1024), "test")
        self.assertLessEqual(saved["bytes"], 4 * 1024 * 1024)
        self.assertTrue(self.saved_path(saved["output_id"]).read_text().endswith(outputs.CUT_NOTE))

    def test_describe(self):
        self.assertEqual(self.tools.describe("read_output", {"output_id": "o-0123456789ab", "start_line": 5}),
                         "o-0123456789ab:5-")
        self.assertEqual(self.tools.describe("read_output", {"output_id": "o-0123456789ab", "query": "FAILED"}),
                         "o-0123456789ab 'FAILED'")
        self.assertEqual(self.tools.describe("read_output", {}), "")


@unittest.skipUnless(POSIX_SH, "the fake Docker shim needs /bin/sh")
class ShellOutputTests(OutputCase):
    def test_failure_at_the_end_survives_and_is_pageable(self):
        with fake_docker():
            result = self.tools.call("shell", {"command": "seq 49999; echo 'FAILED test_x'"})
        self.assertIn("FAILED test_x", result["output"])
        self.assertTrue(result["output"].startswith("1\n2\n"))
        self.assertIn("characters truncated", result["output"])
        self.assertEqual((result["truncated"], result["stopped"], result["total_lines"]), (True, None, 50_000))
        self.assertEqual(result["total_bytes"], len("".join(f"{i}\n" for i in range(1, 50_000))) + 14)
        self.assertRegex(result["output_id"], r"^o-[0-9a-f]{12}$")
        page = self.tools.call("read_output", {"output_id": result["output_id"], "start_line": 49_990})
        self.assertEqual(page["content"], "".join(f"{i}\n" for i in range(49_990, 50_000)) + "FAILED test_x\n")
        self.assertNotIn("next_start_line", page)
        found = self.tools.call("read_output", {"output_id": result["output_id"], "query": "FAILED"})
        self.assertEqual(found["matches"], [{"line": 50_000, "text": "FAILED test_x"}])
        # The marker's start_line points at the first omitted line.
        first = int(result["output"].split("start_line=", 1)[1].split(")", 1)[0])
        page = self.tools.read_output(result["output_id"], start_line=first, end_line=first)
        self.assertNotIn(page["content"], result["output"].split("\n…[", 1)[0] + "\n")

    def test_saved_output_is_redacted_and_private(self):
        with patch.dict(os.environ, {"EIRA_TEST_TOKEN": "secret-value-123"}):
            self.store.redact = Redactor()
        with fake_docker():
            # The command never contains the value; tr assembles it inside the container.
            result = self.tools.shell("seq 30000 | sed 's/$/ secret-value-1Y3/' | tr Y 2")
        self.assertNotIn("secret-value-123", json.dumps(result))
        path = self.saved_path(result["output_id"])
        saved = path.read_text()
        self.assertIn("[REDACTED]", saved)
        self.assertNotIn("secret-value-123", saved)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(path.parent.parent.stat().st_mode), 0o700)

    def test_output_limit_stops_and_saves_incomplete_output(self):
        self.assertEqual(SHELL_CAPTURE_BYTES, 4 * 1024 * 1024)
        with fake_docker():
            result = self.tools.shell("head -c 5000000 /dev/zero | tr '\\0' x")
        self.assertEqual(result["stopped"], "output_limit")
        self.assertTrue(result["truncated"])
        self.assertIn("incomplete", result["note"])
        self.assertGreater(result["total_bytes"], SHELL_CAPTURE_BYTES)
        self.assertLessEqual(self.saved_path(result["output_id"]).stat().st_size, SHELL_CAPTURE_BYTES)

    def test_small_output_saves_nothing(self):
        with fake_docker():
            result = self.tools.shell("printf 'a\\nb\\n'")
        self.assertEqual((result["output"], result["output_id"], result["total_lines"]), ("a\nb\n", None, 2))
        self.assertFalse((self.root / ".eira" / "outputs").exists())


class FetchOutputTests(OutputCase):
    def test_long_pages_keep_head_and_tail(self):
        body = "".join(f"para {i:06d} " + "w" * 87 + "\n" for i in range(2_000)).encode()
        self.assertEqual(len(body), 200_000)
        self.policy.allowed_hosts.add("example.com")
        with patch("eira_harness.network.request_bytes", return_value=(200, {"content-type": "text/plain"}, body)) as fetch:
            result = self.tools.call("fetch_url", {"url": "https://example.com/big.txt"})
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.kwargs.get("max_bytes", 1_000_000), 1_000_000)
        self.assertTrue(result["text"].startswith("para 000000"))
        self.assertTrue(result["text"].endswith("para 001999 " + "w" * 87 + "\n"))
        self.assertIn("characters truncated", result["text"])
        self.assertLess(len(result["text"]), 30_300)
        self.assertEqual((result["total_chars"], result["truncated"]), (200_000, True))
        self.assertEqual(self.tools.read_output(result["output_id"], query="para 001000")["matches"][0]["line"], 1001)
        self.assertEqual(set(result), {"url", "content_type", "text", "truncated", "trust", "total_chars", "output_id"})

    def test_short_pages_are_unchanged(self):
        self.policy.allowed_hosts.add("example.com")
        with patch("eira_harness.network.request_bytes", return_value=(200, {"content-type": "text/plain"}, b"hello")):
            result = self.tools.fetch_url("https://example.com/")
        self.assertEqual((result["text"], result["truncated"], result["total_chars"], result["output_id"]),
                         ("hello", False, 5, None))

    def test_network_default_still_cuts_at_30000(self):
        from eira_harness.network import fetch_public
        with patch("eira_harness.network.request_bytes", return_value=(200, {"content-type": "text/plain"}, b"x" * 40_000)):
            result = fetch_public("https://example.com/")
        self.assertEqual((len(result["text"]), result["truncated"]), (30_000, True))


class FitTests(OutputCase):
    def test_fit_keeps_head_and_tail_and_saves_the_full_result(self):
        agent = Agent(None, self.store, self.tools, limits=Limits(max_tool_output_chars=2_000))
        content = "HEAD" + "q" * 50_000 + "TAIL"
        encoded = agent.fit({"ok": True, "result": {"path": "big.txt", "content": content, "sha256": "abc"}})
        self.assertLessEqual(len(encoded), 2_000)
        data = json.loads(encoded)
        self.assertEqual((data["result"]["path"], data["result"]["sha256"], data["truncated"]), ("big.txt", "abc", True))
        self.assertTrue(data["result"]["content"].startswith("HEADqq"))
        self.assertTrue(data["result"]["content"].endswith("qqTAIL"))
        self.assertIn("characters truncated; see output_id", data["result"]["content"])
        saved = self.tools.read_output(data["output_id"], query="TAIL")
        self.assertEqual(len(saved["matches"]), 1)
        full = json.loads(self.saved_path(data["output_id"]).read_text())
        self.assertEqual(full["result"]["content"], content)

    def test_fit_preview_fallback_carries_output_id(self):
        agent = Agent(None, self.store, self.tools, limits=Limits(max_tool_output_chars=1_000))
        encoded = agent.fit({"ok": True, "result": {"paths": ["p" * 300 for _ in range(200)]}})
        self.assertLessEqual(len(encoded), 1_000)
        data = json.loads(encoded)
        self.assertIn("preview", data)
        self.assertEqual(json.loads(self.saved_path(data["output_id"]).read_text())["result"]["paths"][199], "p" * 300)

    def test_small_results_save_nothing(self):
        agent = Agent(None, self.store, self.tools)
        self.assertEqual(agent.fit({"ok": True, "result": {"x": 1}}), '{"ok": true, "result": {"x": 1}}')
        self.assertFalse((self.root / ".eira" / "outputs").exists())


@unittest.skipUnless(POSIX_SH, "the fake Docker shim needs /bin/sh")
class AgentEndToEndTests(OutputCase):
    def test_model_pages_a_long_shell_output_and_prefix_stays_stable(self):
        class Provider:
            model = "scripted"

            def __init__(self):
                self.seen = []

            def complete(self, messages, tools):
                self.seen.append(json.loads(json.dumps(messages)))
                if len(self.seen) == 1:
                    return call("shell", {"command": "seq 50000"}, "c1"), {"total_tokens": 1}
                if len(self.seen) == 2:
                    result = json.loads(messages[-1]["content"])["result"]
                    return call("read_output", {"output_id": result["output_id"], "start_line": 49_990}, "c2"), {"total_tokens": 1}
                return {"role": "assistant", "content": "done"}, {"total_tokens": 1}

        provider, events = Provider(), []
        with fake_docker():
            result = Agent(provider, self.store, self.tools, emit=events.append).run("Run it")
        self.assertEqual(result["status"], "completed")
        for before, after in zip(provider.seen, provider.seen[1:]):
            self.assertEqual(after[:len(before)], before)
        journal = [m for m in self.store.messages(self.session) if m["role"] == "tool"]
        seen = [m for m in provider.seen[-1] if m["role"] == "tool"]
        self.assertEqual(journal, seen)
        page = json.loads(seen[1]["content"])["result"]
        self.assertTrue(page["content"].endswith("49999\n50000\n"))
        saved = [e for e in events if e["event"] == "output_saved"]
        self.assertEqual(len(saved), 1)
        self.assertEqual({k: saved[0][k] for k in ("tool", "lines")}, {"tool": "shell", "lines": 50_000})
        self.assertIn("output_saved", [e["kind"] for e in self.store.events(self.session)])


class TerminalTests(unittest.TestCase):
    def test_output_saved_line(self):
        out = io.StringIO()
        Terminal(out).emit({"event": "output_saved", "output_id": "o-0123456789ab", "tool": "shell",
                            "bytes": 10, "lines": 4977})
        self.assertEqual(out.getvalue(), "  · long output saved (4,977 lines)\n")
        out = io.StringIO()
        Terminal(out).emit({"event": "output_saved", "lines": "x"})
        self.assertEqual(out.getvalue(), "  · long output saved\n")


if __name__ == "__main__":
    unittest.main()
