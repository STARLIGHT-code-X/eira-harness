"""Behavioral evals: statistics, command checks, parallel runs, Codex harness, coding suite."""
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch

from eira_harness.agent import Limits
from eira_harness.cli import main
from eira_harness.evals import STARTER_SUITE, compare_reports, load_suite, run_suite, validate_suite
from eira_harness.evalstats import classify, pass_at_k, wilson
from eira_harness.harnesses import parse_codex_events
from eira_harness.security import HarnessError, Workspace
from eira_harness.suites import CODING_SUITE
try:  # discovered as top-level modules (`-s tests`) or as a package
    from fakes import fake_docker
    from test_evals import Lazy, Oracle
except ImportError:
    from tests.fakes import fake_docker
    from tests.test_evals import Lazy, Oracle

IMAGE = os.environ.get("EIRA_DOCKER_IMAGE", "")
FAKES = Path(__file__).resolve().parent / "fakes"


def call(name, args, call_id):
    return {"role": "assistant", "content": None, "tool_calls": [{"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}]}


@contextmanager
def fake_codex():
    with tempfile.TemporaryDirectory() as directory:
        wrapper = Path(directory) / "codex"
        wrapper.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(FAKES / 'codex'))} \"$@\"\n")
        wrapper.chmod(0o700)
        with patch.dict(os.environ, {"PATH": directory + os.pathsep + os.environ.get("PATH", "")}):
            yield


TEST = "python -m unittest -q"
CODING_STEPS = {
    "multi-module-bug": [("shell", {"command": TEST}),
                         ("edit_file", {"path": "pricing/tax.py", "old_string": "rate_percent / 10)", "new_string": "rate_percent / 100)"})],
    "implement-to-spec": [("edit_file", {"path": "textkit/slug.py", "old_string": "    raise NotImplementedError",
                                         "new_string": "    import re\n    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')"}),
                          ("shell", {"command": TEST})],
    "package-rename": [("edit_file", {"path": p, "old_string": "load_cfg", "new_string": "load_config", "replace_all": True})
                       for p in ["app/settings.py", "app/server.py"]],
    "crlf-module-fix": [("edit_file", {"path": "geometry.py", "old_string": "return width + height", "new_string": "return width * height"})],
    "long-traceback": [("shell", {"command": TEST}),
                       ("edit_file", {"path": "service/config.py", "old_string": "POOL_SIZE = 32", "new_string": "POOL_SIZE = 8"})],
    "three-edits": [("apply_patch", {"input": "*** Begin Patch\n*** Update File: limits.py\n@@\n-MAX_USERS = 100\n+MAX_USERS = 500\n"
                                              "@@\n-MAX_UPLOAD_MB = 10\n+MAX_UPLOAD_MB = 25\n@@\n-RETENTION_DAYS = 30\n+RETENTION_DAYS = 90\n*** End Patch\n"})],
    "argparse-flag": [("edit_file", {"path": "tool.py", "old_string": "    parser.add_argument('items', nargs='*')\n",
                                     "new_string": "    parser.add_argument('items', nargs='*')\n    parser.add_argument('--limit', type=int, default=10)\n"}),
                      ("edit_file", {"path": "tool.py", "old_string": "return list(args.items)", "new_string": "return list(args.items)[:args.limit]"})],
    "already-passing": [],
}


class CodingOracle:
    model = "coding-oracle"

    def complete(self, messages, tools):
        prompt = next(m["content"] for m in messages if m["role"] == "user")
        task = next(t["id"] for t in CODING_SUITE["tasks"] if t["prompt"] == prompt)
        done = sum(m["role"] == "tool" for m in messages)
        steps = CODING_STEPS[task]
        if done < len(steps):
            return call(steps[done][0], steps[done][1], f"c{done}"), {"total_tokens": 3}
        return {"role": "assistant", "content": "The tests pass now."}, {"total_tokens": 1}


class StatsTests(unittest.TestCase):
    def test_pass_at_k_and_wilson_match_references(self):
        self.assertAlmostEqual(pass_at_k(10, 3, 1), 0.3)
        self.assertAlmostEqual(pass_at_k(10, 3, 5), 1 - 21 / 252)
        self.assertEqual((pass_at_k(5, 5, 3), pass_at_k(5, 0, 1)), (1.0, 0.0))
        low, high = wilson(0, 10)
        self.assertEqual(low, 0.0)
        self.assertAlmostEqual(high, 0.2775, places=4)
        low, high = wilson(10, 10)
        self.assertEqual(high, 1.0)
        self.assertAlmostEqual(low, 0.7225, places=4)
        self.assertEqual(tuple(round(x, 4) for x in wilson(5, 10)), (0.2366, 0.7634))

    def test_error_taxonomy(self):
        cases = {"old_string was not found. Read the file again": "edit_miss",
                 "old_string matches 2 places.": "edit_ambiguous",
                 "Syntax check failed for a.py": "syntax_rejected",
                 "Action denied. Do not retry": "approval_denied",
                 "Invalid type for path: expected string.": "schema_error",
                 "File changed during approval; edit cancelled.": "stale_edit",
                 "Something unexpected": "other"}
        for text, category in cases.items():
            self.assertEqual(classify(text), category, text)


class ValidationTests(unittest.TestCase):
    def suite(self, check, **extra):
        return {"name": "s", **extra, "tasks": [{"id": "t", "prompt": "p", "files": {"t.py": "x"}, "checks": [check]}]}

    def test_bad_command_checks_are_rejected(self):
        for check in [{"type": "command_succeeds"}, {"type": "command_succeeds", "command": "true", "timeout": 0},
                      {"type": "command_succeeds", "command": "true", "restore": ["other.py"]},
                      {"type": "command_succeeds", "command": "true", "output_matches": "("},
                      {"type": "command_succeeds", "command": "true", "extra": 1}]:
            with self.subTest(check=check), self.assertRaises(HarnessError):
                validate_suite(self.suite(check))

    def test_command_suite_needs_an_image_before_any_request(self):
        suite = validate_suite(self.suite({"type": "command_succeeds", "command": "true"}))

        class Never:
            model = "never"
            def complete(self, messages, tools):
                raise AssertionError("no model call expected")
        with self.assertRaisesRegex(HarnessError, "--docker-image"):
            run_suite(suite, Never, Limits())


class CommandCheckTests(unittest.TestCase):
    def test_oracle_passes_cheater_is_defeated_by_restore(self):
        suite = {"name": "s", "tasks": [next(t for t in CODING_SUITE["tasks"] if t["id"] == "crlf-module-fix")]}
        with fake_docker():
            lazy = run_suite(validate_suite(json.loads(json.dumps(suite))), Lazy, Limits(), image="img")
            oracle = run_suite(validate_suite(json.loads(json.dumps(suite))), CodingOracle, Limits(), image="img")
        self.assertFalse(lazy["results"][0]["passed"])
        self.assertIn("exit 1", lazy["results"][0]["checks"][0]["detail"])
        self.assertTrue(oracle["results"][0]["passed"], oracle["results"][0])

    def test_restore_resets_tests_the_agent_changed(self):
        task = {"id": "t", "prompt": "fix", "files": {"tests/__init__.py": "", "tests/test_main.py":
                "import unittest\n\nclass T(unittest.TestCase):\n    def test_fails(self):\n        self.fail('real failure')\n"},
                "checks": [{"type": "command_succeeds", "command": "python -m unittest -q", "restore": ["tests/test_main.py"]}]}

        class Cheat:
            model = "cheat"
            def __init__(self):
                self.step = 0
            def complete(self, messages, tools):
                self.step += 1
                if self.step == 1:
                    return call("edit_file", {"path": "tests/test_main.py", "old_string": "self.fail('real failure')",
                                              "new_string": "pass"}, "c0"), {}
                return {"role": "assistant", "content": "Fixed."}, {}
        with fake_docker():
            report = run_suite(validate_suite({"name": "s", "tasks": [task]}), Cheat, Limits(), image="img")
        self.assertFalse(report["results"][0]["passed"])


class SuiteTests(unittest.TestCase):
    def test_coding_suite_oracle_passes_and_lazy_fails_all_but_the_last(self):
        context = fake_docker() if not IMAGE else _null()
        image = IMAGE or "img"
        with context:
            oracle = run_suite(load_suite("coding", Workspace(Path.cwd())), CodingOracle, Limits(), image=image)
            lazy = run_suite(load_suite("coding", Workspace(Path.cwd())), Lazy, Limits(), image=image)
        self.assertEqual([r["task"] for r in oracle["results"] if not r["passed"]], [], oracle["results"])
        self.assertEqual([r["task"] for r in lazy["results"] if r["passed"]], ["already-passing"])
        shell_tasks = [r for r in oracle["results"] if r["task"] in {"multi-module-bug", "long-traceback"}]
        self.assertTrue(all(r["tool_calls"] >= 2 for r in shell_tasks))
        # Agent shells ran under sandboxed autorun: nothing was denied, nothing asked.
        self.assertEqual(oracle["summary"]["errors"], {})

    def test_parallel_runs_are_deterministic_and_report_v2(self):
        serial = run_suite(STARTER_SUITE, Oracle, Limits(), repeat=2, jobs=1)
        parallel = run_suite(STARTER_SUITE, Oracle, Limits(), repeat=2, jobs=4)
        key = lambda report: [(r["run"], r["task"], r["passed"]) for r in report["results"]]
        self.assertEqual(key(serial), key(parallel))
        summary = parallel["summary"]
        self.assertEqual(parallel["report_version"], 2)
        for field in ("runs", "passed", "pass_rate", "steps", "tool_calls", "tool_errors", "tokens", "seconds",
                      "pass_rate_ci95", "pass_at_k", "errors", "tokens_mean", "tokens_stdev", "seconds_mean"):
            self.assertIn(field, summary)
        self.assertEqual(summary["pass_at_k"], {"1": 1.0, "2": 1.0})
        self.assertEqual(len(parallel["tasks"]), len(STARTER_SUITE["tasks"]))

    def test_errors_are_classified_per_run(self):
        suite = {"name": "s", "tasks": [{"id": "t", "prompt": "p", "files": {"a.txt": "x\n"},
                                         "checks": [{"type": "answer_contains", "text": "ok"}]}]}

        class Misses:
            model = "m"
            def __init__(self):
                self.step = 0
            def complete(self, messages, tools):
                self.step += 1
                if self.step == 1:
                    return call("edit_file", {"path": "a.txt", "old_string": "nope", "new_string": "y"}, "c0"), {}
                return {"role": "assistant", "content": "ok"}, {}
        report = run_suite(validate_suite(suite), Misses, Limits())
        self.assertEqual(report["summary"]["errors"], {"edit_miss": 1})


class CompareAndCodexTests(unittest.TestCase):
    def test_compare_handles_v1_and_v2_reports(self):
        v2 = run_suite(STARTER_SUITE, Oracle, Limits())
        v1 = {"suite": "starter", "model": "old", "summary": {"runs": 7, "passed": 0, "pass_rate": 0.0},
              "results": [{"task": t["id"], "passed": False} for t in STARTER_SUITE["tasks"]]}
        text = compare_reports(v1, v2)
        self.assertIn("fix-mean", text)
        self.assertIn("+1.00", text)
        self.assertIn("intervals do not overlap", text)
        self.assertIn("difference not established", compare_reports(v2, v2))

    def test_codex_events_are_parsed_without_error_text(self):
        data = b"\n".join(json.dumps(e).encode() for e in [
            {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
            {"type": "item.completed", "item": {"type": "file_change"}},
            {"type": "error", "message": "SECRET"}])
        parsed = parse_codex_events(data)
        self.assertEqual((parsed["answer"], parsed["tool_calls"], parsed["status"]), ("done", 1, "error"))
        self.assertNotIn("SECRET", json.dumps(parsed))

    def test_codex_harness_runs_the_same_checks(self):
        suite = {"name": "s", "tasks": [{"id": "t", "prompt": "write a marker", "checks": [
            {"type": "file_exists", "path": "codex_was_here.txt"}, {"type": "answer_contains", "text": "Fake Codex"}]}]}
        with fake_codex():
            report = run_suite(validate_suite(suite), lambda: None, Limits(), harness="codex")
            with patch.dict(os.environ, {"FAKE_CODEX_MODE": "error"}):
                failed = run_suite(validate_suite(suite), lambda: None, Limits(), harness="codex")
        result = report["results"][0]
        self.assertTrue(result["passed"], result)
        self.assertEqual((report["harness"], result["tokens"], result["tool_calls"]), ("codex", 42, 1))
        self.assertEqual(report["codex_version"], "codex-cli 0.0.0-fake")
        self.assertEqual(failed["results"][0]["status"], "error")
        self.assertNotIn("SECRET-ERROR-TEXT", json.dumps(failed))

    def test_cli_compare_and_missing_codex(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report = run_suite(STARTER_SUITE, Oracle, Limits())
            (root / "a.json").write_text(json.dumps(report))
            (root / "b.json").write_text(json.dumps(report))
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main(["eval", "--compare", "a.json", "b.json", "--workspace", temp]), 0)
            self.assertIn("difference not established", out.getvalue())
            with patch.dict(os.environ, {"PATH": "/nonexistent"}), redirect_stderr(io.StringIO()) as err:
                self.assertEqual(main(["eval", "--harness", "codex", "--workspace", temp]), 2)
            self.assertIn("codex is not on PATH", err.getvalue())


@contextmanager
def _null():
    yield


if __name__ == "__main__":
    unittest.main()
