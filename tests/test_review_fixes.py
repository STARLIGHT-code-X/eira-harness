"""Regression tests for defects found in the 0.4 review."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from eira_harness.agent import COMPACT_PROMPT, UPDATE_NOTE, Agent, Limits
from eira_harness.cli import main
from eira_harness.evals import STARTER_SUITE, run_suite, validate_suite
from eira_harness.provider import Provider, _anthropic_messages
from eira_harness.security import HarnessError, Redactor, Workspace
from eira_harness.store import Store
from eira_harness.tools import Policy, Toolbox, _diff, glob_match


def call(name, args, call_id, raw=None):
    return {"role": "assistant", "content": None, "tool_calls": [{"id": call_id, "type": "function",
            "function": {"name": name, "arguments": raw if raw is not None else json.dumps(args)}}]}


def text(value):
    return {"role": "assistant", "content": value}


class Scripted:
    model = "scripted"

    def __init__(self, *responses, usage=10):
        self.responses, self.seen, self.usage = list(responses), [], usage

    def complete(self, messages, tools):
        self.seen.append(json.loads(json.dumps(messages)))
        return self.responses.pop(0), {"total_tokens": self.usage}


class Base(unittest.TestCase):
    redactor = None

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root, self.redactor)
        self.session = self.store.create("test")
        self.approvals = []
        policy = Policy(approve=lambda name, detail: self.approvals.append((name, detail)) or True)
        self.tools = Toolbox(Workspace(self.root), self.store, policy, self.session)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def agent(self, provider, events=None, **limits):
        return Agent(provider, self.store, self.tools, (events.append if events is not None else lambda e: None),
                     Limits(**limits))


class ReasoningReplayTests(Base):
    redactor = Redactor(extra=("fixture-secret-value",))

    def test_redaction_withholds_blocks_and_drops_only_leading_reasoning(self):
        blocks = [{"type": "thinking", "thinking": "", "signature": "sig-1"},
                  {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "fixture-secret-value"}}]
        first = {**call("read_file", {"path": "fixture-secret-value"}, "t1"), "anthropic_content": blocks}
        clean = [{"type": "thinking", "thinking": "", "signature": "sig-2"},
                 {"type": "tool_use", "id": "t2", "name": "list_files", "input": {}}]
        second = {**call("list_files", {}, "t2"), "anthropic_content": clean}
        events = []
        self.agent(Scripted(first, second, text("done")), events).run("go")
        stored = [m for m in self.store.messages(self.session) if m["role"] == "assistant"]
        self.assertNotIn("anthropic_content", stored[0])
        self.assertTrue(stored[0]["reasoning_withheld"])
        self.assertEqual(stored[1]["anthropic_content"], clean)
        self.assertIn("reasoning_withheld", [e["event"] for e in events])
        _, native = _anthropic_messages([{"role": "system", "content": "s"}, *self.store.messages(self.session)])
        assistants = [m for m in native if m["role"] == "assistant"]
        self.assertNotIn("thinking", json.dumps(assistants[0]))
        self.assertEqual(assistants[1]["content"], clean)


class CompactionTests(Base):
    def big_file(self):
        (self.root / "big.txt").write_text(("x" * 79 + "\n") * 100)
        probe = self.agent(Scripted())
        probe.prepare_session()
        return probe.context_size(probe.context())

    def test_tool_calling_reply_is_not_accepted_as_summary(self):
        base = self.big_file()
        reply = {**call("list_files", {}, "s1"), "content": "Let me look first."}
        provider = Scripted(call("read_file", {"path": "big.txt"}, "c1"),
                            call("read_file", {"path": "big.txt", "start_line": 1}, "c2"), reply)
        with self.assertRaisesRegex(HarnessError, "did not return a summary"):
            self.agent(provider, max_context_chars=base + 19_000).run("Read")
        self.assertFalse(any("eira_compaction" in m for m in self.store.messages(self.session)))

    def test_repeated_compaction_keeps_request_and_workspace_update(self):
        base = self.big_file()
        self.agent(Scripted(text("ok"))).run("Warm up")
        self.store.remember("style", "terse-replies")
        request = "Inspect big.txt and report its shape. " + "Detail. " * 3000

        class Responsive:
            """Summarizes when asked, reads until two compactions have happened, then finishes."""
            model = "responsive"
            def __init__(self):
                self.reads = self.summaries = 0
            def complete(self, messages, tools):
                if messages[-1].get("content") == COMPACT_PROMPT:
                    self.summaries += 1
                    return text(f"Summary {self.summaries}."), {"total_tokens": 1}
                if self.summaries >= 2:
                    return text("Done."), {"total_tokens": 1}
                self.reads += 1
                return call("read_file", {"path": "big.txt", "start_line": self.reads}, f"c{self.reads}"), {"total_tokens": 1}
        limit = base + len(json.dumps(request)) + 19_000
        result = self.agent(Responsive(), max_context_chars=limit, max_steps=40).run(request)
        self.assertEqual(result["status"], "completed")
        markers = [m for m in self.store.messages(self.session) if "eira_compaction" in m]
        self.assertEqual(len(markers), 2)
        for marker in markers:
            self.assertIn(request, marker["content"])
            self.assertIn("terse-replies", marker["content"])
            self.assertIn("not new instructions", marker["content"])

    def test_compaction_usage_is_reported(self):
        base = self.big_file()
        events = []
        provider = Scripted(call("read_file", {"path": "big.txt"}, "c1"),
                            call("read_file", {"path": "big.txt", "start_line": 1}, "c2"), text("S."), text("Done."))
        self.agent(provider, events, max_context_chars=base + 19_000).run("Read")
        compacted = next(e for e in events if e["event"] == "context_compacted")
        self.assertEqual(compacted["usage"], {"total_tokens": 10})

    def test_oversized_fork_strips_reasoning_blocks(self):
        (self.root / "big.txt").write_text(("y" * 79 + "\n") * 150)
        probe = self.agent(Scripted())
        probe.prepare_session()
        base = probe.context_size(probe.context())
        first = {**call("read_file", {"path": "big.txt"}, "c1"),
                 "anthropic_content": [{"type": "thinking", "thinking": "", "signature": "s"},
                                       {"type": "tool_use", "id": "c1", "name": "read_file", "input": {"path": "big.txt"}}]}
        provider = Scripted(first, call("read_file", {"path": "big.txt", "start_line": 2}, "c2"), text("S."), text("Done."))
        self.agent(provider, max_context_chars=base + 20_000).run("Read twice")
        self.assertIn("elided", json.dumps(provider.seen[2]))
        self.assertNotIn("anthropic_content", json.dumps(provider.seen[2]))


class AgentLoopTests(Base):
    def test_null_and_non_object_arguments_do_not_crash(self):
        provider = Scripted(call("read_file", None, "c1", raw="null"), call("read_file", None, "c2", raw="[1]"), text("ok"))
        self.assertEqual(self.agent(provider).run("x")["status"], "completed")
        results = [json.loads(m["content"]) for m in self.store.messages(self.session) if m["role"] == "tool"]
        self.assertTrue(all(not r["ok"] and "JSON object" in r["error"] for r in results))

    def test_empty_reply_is_not_journaled(self):
        with self.assertRaisesRegex(HarnessError, "neither text nor tool calls"):
            self.agent(Scripted({"role": "assistant", "content": ""})).run("x")
        self.assertEqual([m["role"] for m in self.store.messages(self.session)], ["user"])

    def test_no_repeat_warning_after_human_denial(self):
        (self.root / "a.txt").write_text("a\n")
        self.tools.policy = Policy()
        calls = [call("write_file", {"path": "b.txt", "content": "b", "expected_sha256": "new"}, f"c{i}") for i in range(3)]
        self.agent(Scripted(*calls, text("stop"))).run("x")
        results = [json.loads(m["content"]) for m in self.store.messages(self.session) if m["role"] == "tool"]
        self.assertTrue(all("repeat_warning" not in r for r in results))

    def test_fit_fallback_never_exceeds_limit(self):
        agent = self.agent(Scripted(), max_tool_output_chars=1_000)
        encoded = agent.fit({"ok": True, "result": {"paths": ["p" * 300 for _ in range(200)]}})
        self.assertLessEqual(len(encoded), 1_000)
        json.loads(encoded)

    def test_frozen_prompt_survives_new_secret_values(self):
        self.agent(Scripted(text("one"))).run("first")
        frozen = self.store.session_context(self.session)["system"]
        (self.root / "EIRA.md").write_text("Use the deploy-token-123456 runbook.\n")
        with patch.dict(os.environ, {"DEPLOY_TOKEN": "deploy-token-123456"}):
            self.store.redact = Redactor()
            provider = Scripted(text("two"))
            self.agent(provider).run("second")
        self.assertEqual(self.store.session_context(self.session)["system"], frozen)
        self.assertNotIn("deploy-token-123456", json.dumps(provider.seen))

    def test_frozen_prompt_is_redacted_at_rest(self):
        (self.root / "EIRA.md").write_text("token sk-abcdefghijklmnopqrstuvwx\n")
        self.agent(Scripted(text("ok"))).run("x")
        self.assertNotIn("sk-abcdefghijklmnop", self.store.session_context(self.session)["system"])


class ToolFixTests(Base):
    def write(self, name, data):
        (self.root / name).write_text(data, newline="")
        return data

    def test_escape_heavy_pages_fit_and_pages_are_contiguous(self):
        self.write("q.json", "".join('{"k": "\\"v\\"\\t\\\\"}\n' for _ in range(2000)))
        agent = self.agent(Scripted())
        seen, start = [], 1
        while start:
            page = self.tools.read_file("q.json", start)
            encoded = agent.fit({"ok": True, "result": page})
            self.assertNotIn("characters truncated", encoded)
            seen.append(page["content"])
            start = page.get("next_start_line")
        self.assertEqual("".join(seen), (self.root / "q.json").read_bytes().decode())

    def test_long_single_line_is_cut_and_flagged(self):
        self.write("min.js", "a" * 100_000)
        page = self.tools.read_file("min.js")
        self.assertTrue(page["line_truncated"])
        self.assertLessEqual(len(page["content"]), 24_000)

    def test_edit_cannot_assemble_a_protected_value(self):
        with patch.dict(os.environ, {"FIXTURE_API_KEY": "abc123def456"}):
            self.store.redact = Redactor()
            self.write("c.txt", "key=abc123XXXXXX\n")
            with self.assertRaisesRegex(HarnessError, "protected"):
                self.tools.edit_file("c.txt", "XXXXXX", "def456")
        self.assertEqual((self.root / "c.txt").read_text(), "key=abc123XXXXXX\n")

    def test_crlf_files_stay_crlf(self):
        self.write("w.txt", "a\r\nb\r\nc\r\n")
        self.tools.edit_file("w.txt", "b", "b1\nb2")
        self.assertEqual((self.root / "w.txt").read_bytes(), b"a\r\nb1\r\nb2\r\nc\r\n")
        self.tools.edit_file("w.txt", "c\r\n", "c\r\nd\r\n")
        self.assertEqual((self.root / "w.txt").read_bytes(), b"a\r\nb1\r\nb2\r\nc\r\nd\r\n")

    def test_overlapping_matches_are_ambiguous(self):
        self.write("o.txt", "aaa\n")
        with self.assertRaisesRegex(HarnessError, "places"):
            self.tools.edit_file("o.txt", "aa", "b")

    def test_replace_all_result_is_bounded(self):
        self.write("r.txt", "x" * 10_000)
        with self.assertRaisesRegex(HarnessError, "limit"):
            self.tools.edit_file("r.txt", "x", "y" * 1_000, replace_all=True)

    def test_large_file_diff_is_fast_and_numbered(self):
        big = "".join(f"{i}\n" for i in range(200_000))
        started = time.monotonic()
        diff = _diff("f", big, big.replace("\n150000\n", "\n150000 changed\n"))
        self.assertLess(time.monotonic() - started, 2)
        self.assertIn("@@ -149998,7 +149998,7 @@", diff)

    def test_diff_marks_missing_final_newline(self):
        diff = _diff("f", "a\nb", "a\nc")
        self.assertIn("-b\n\\ No newline at end of file\n+c\n", diff)

    def test_line_numbers_agree_across_tools(self):
        self.write("ff.txt", "one\x0ctwo\nthree\nfour\n")
        self.assertEqual(self.tools.read_file("ff.txt")["total_lines"], 3)
        self.assertEqual(self.tools.search_files("four")["matches"][0]["line"], 3)
        self.assertEqual(self.tools.edit_file("ff.txt", "four", "4")["first_changed_line"], 3)

    def test_search_reads_large_files_and_reports_skips(self):
        self.write("large.txt", "filler\n" * 200_000 + "needle\n")
        (self.root / "bin.dat").write_bytes(b"\x00needle")
        result = self.tools.search_files("needle")
        self.assertEqual([m["path"] for m in result["matches"]], ["large.txt"])
        self.assertEqual(result["skipped_files"], 1)

    def test_glob_semantics(self):
        self.assertTrue(glob_match("app.py", "**/*.py"))
        self.assertTrue(glob_match("src/app.py", "src/**/*.py"))
        self.assertFalse(glob_match("src/deep/app.py", "src/*.py"))
        self.assertTrue(glob_match("deep/app.py", "*.py"))
        self.assertTrue(glob_match("src/app.py", "./src/*.py"))

    def test_progress_detail_is_redacted_before_cutting(self):
        secret = "sk-" + "a" * 40
        detail = self.tools.describe("shell", {"command": "x" * 80 + " " + secret})
        self.assertNotIn("aaaaaaaa", detail)


class EvalFixTests(unittest.TestCase):
    def test_bad_paths_and_meaningless_checks_fail_validation(self):
        task = {"id": "t", "prompt": "p", "checks": [{"type": "answer_contains", "text": "x"}]}
        bad = [{**task, "files": {"../x": "y"}}, {**task, "files": {".env": "y"}},
               {**task, "checks": [{"type": "file_contains", "path": ".git/config", "text": "x"}]},
               {**task, "checks": [{"type": "file_unchanged", "path": "nope.txt"}]},
               {**task, "checks": [{"type": "answer_contains", "text": ""}]},
               {**task, "checks": [{"type": "answer_contains", "text": "x", "ignore_case": "yes"}]},
               {**task, "checks": [{"type": "answer_contains", "text": "x", "path": "a"}]}]
        for item in bad:
            with self.subTest(item=item), self.assertRaises(HarnessError):
                validate_suite({"name": "s", "tasks": [task, {**item, "id": "u"}]})

    def test_matches_respects_ignore_case_and_starter_accepts_common_forms(self):
        suite = {"name": "s", "tasks": [{"id": "t", "prompt": "p", "checks": [
            {"type": "answer_matches", "pattern": "DONE", "ignore_case": True}]}]}

        class Says:
            model = "m"
            def complete(self, messages, tools):
                return {"role": "assistant", "content": "done"}, {"total_tokens": 4}
        report = run_suite(validate_suite(suite), Says, Limits())
        self.assertEqual(report["summary"]["passed"], 1)
        self.assertEqual(report["summary"]["tokens"], 4)
        import re
        honesty = next(t for t in STARTER_SUITE["tasks"] if t["id"] == "protected-file-honesty")["checks"][1]["pattern"]
        for answer in ["I couldn't modify settings.py because it holds a redacted credential.",
                       "The file was not changed: edits to protected files are refused."]:
            self.assertRegex(answer, honesty)
        steps = next(t for t in STARTER_SUITE["tasks"] if t["id"] == "add-function")["checks"][0]["pattern"]
        self.assertTrue(re.search(steps, "def word_count(text: str) -> int:", re.M))

    def test_errored_runs_still_count_tokens_and_unattended_runs_deny_side_effects(self):
        suite = {"name": "s", "tasks": [{"id": "t", "prompt": "p", "checks": [{"type": "answer_contains", "text": "x"}]}]}

        class Tries:
            model = "m"
            def __init__(self):
                self.step = 0
            def complete(self, messages, tools):
                self.step += 1
                if self.step == 1:
                    return {"role": "assistant", "content": None, "tool_calls": [
                        {"id": "a", "type": "function", "function": {"name": "fetch_url", "arguments": '{"url": "https://example.com"}'}},
                        {"id": "b", "type": "function", "function": {"name": "market_prices", "arguments": '{"source": "coinbase", "symbol": "BTC-USD"}'}},
                        {"id": "c", "type": "function", "function": {"name": "shell", "arguments": '{"command": "true"}'}}]}, {"total_tokens": 7}
                raise HarnessError("Model endpoint returned HTTP 500.")
        with patch("eira_harness.tools.fetch_public") as fetch, patch("eira_harness.market_data.fetch_prices") as prices:
            report = run_suite(validate_suite(suite), Tries, Limits())
            fetch.assert_not_called()
            prices.assert_not_called()
        result = report["results"][0]
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["tokens"], 7)
        self.assertEqual((result["tool_calls"], result["tool_errors"]), (3, 3))


class CliFixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"HOME": str(self.root), "XDG_CONFIG_HOME": str(self.root / "config")}, clear=True)
        self.env.start()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.env.stop)

    def test_provider_and_limit_flags_reach_their_consumers(self):
        seen = {}

        class Fixture:
            model = "m"
            def complete(self, messages, tools):
                return {"role": "assistant", "content": "ok"}, {}

        def build(provider, model, base_url, **options):
            seen.update(options)
            return Fixture()

        with patch("eira_harness.cli.build_provider", side_effect=build), \
                patch("eira_harness.cli.Agent", wraps=Agent) as agent_cls, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = main(["run", "x", "--workspace", str(self.root), "--provider", "ollama", "--model", "m",
                         "--max-output-tokens", "4096", "--model-timeout", "120", "--no-prompt-cache", "--no-compact"])
        self.assertEqual(code, 0)
        self.assertEqual(seen, {"timeout": 120, "max_output_tokens": 4096, "prompt_cache": False})
        self.assertFalse(agent_cls.call_args.args[4].compact)

    def test_invalid_eval_limits_are_configuration_errors(self):
        with patch("eira_harness.cli.build_provider"), redirect_stderr(io.StringIO()) as err:
            code = main(["eval", "--workspace", str(self.root), "--provider", "ollama", "--model", "m", "--max-steps", "0"])
        self.assertEqual(code, 2)
        self.assertIn("positive", err.getvalue())

    def test_dump_suite_is_sanitized(self):
        (self.root / "s.json").write_text(json.dumps({"name": "s", "tasks": [{"id": "t", "prompt": "hi\u001b]0;x\u0007",
                                                                              "checks": [{"type": "answer_contains", "text": "x"}]}]}))
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(["eval", "s.json", "--dump-suite", "--workspace", str(self.root)]), 0)
        self.assertNotIn("\u001b", out.getvalue())


class ProviderFixTests(unittest.TestCase):
    def response(self, stop):
        return {"type": "message", "role": "assistant", "stop_reason": stop, "content": [{"type": "text", "text": "x"}]}

    def test_stop_reasons_have_accurate_messages(self):
        expected = {"max_tokens": "output limit", "refusal": "declined", "model_context_window_exceeded": "context window"}
        for stop, words in expected.items():
            with self.subTest(stop=stop), patch("eira_harness.provider.request_bytes",
                                                return_value=(200, {}, json.dumps(self.response(stop)).encode())):
                with self.assertRaisesRegex(HarnessError, words):
                    Provider("c", "https://a.example/v1", "k", profile="anthropic").complete([{"role": "user", "content": "x"}], [])

    def test_529_is_retried_and_deadline_uses_full_timeout(self):
        replies = [(529, {}, b"{}"), (200, {}, json.dumps(self.response("end_turn")).encode())]
        timeouts = []

        def fake(url, **kwargs):
            timeouts.append(kwargs["timeout"])
            return replies.pop(0)
        with patch("eira_harness.provider.request_bytes", side_effect=fake), patch("eira_harness.provider.time.sleep"):
            message, _ = Provider("c", "https://a.example/v1", "k", 400, profile="anthropic").complete(
                [{"role": "user", "content": "x"}], [])
        self.assertEqual(message["content"], "x")
        self.assertEqual(len(timeouts), 2)
        self.assertGreater(timeouts[0], 390)

    def test_timeout_bounds(self):
        for value in [0.5, 901]:
            with self.subTest(value=value), self.assertRaises(HarnessError):
                Provider("m", "https://a.example/v1", timeout=value)


if __name__ == "__main__":
    unittest.main()
