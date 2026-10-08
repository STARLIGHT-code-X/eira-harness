from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from eira_harness.agent import Limits
from eira_harness.cli import main
from eira_harness.evals import STARTER_SUITE, run_suite, validate_suite
from eira_harness.security import HarnessError


def call(name, args, call_id):
    return {"role": "assistant", "content": None, "tool_calls": [{"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}]}


def last_result(messages):
    return json.loads(messages[-1]["content"])["result"]


ORACLE = {
    "fix-mean": [("edit_file", {"path": "stats.py", "old_string": "(len(values) - 1)", "new_string": "len(values)"})],
    "add-function": [("edit_file", {"path": "text_utils.py", "old_string": '    return text.upper() + "!"\n',
                                    "new_string": '    return text.upper() + "!"\n\n\ndef word_count(text):\n    return len(text.split())\n'})],
    "rename-everywhere": [(  "edit_file", {"path": path, "old_string": "calc_total", "new_string": "inventory_total", "replace_all": True})
                          for path in ["inventory.py", "report.py", "tests/test_inventory.py"]],
    "answer-from-code": [("search_files", {"query": "DEFAULT_TIMEOUT_SECONDS ="})],
    "create-changelog": [("write_file", {"path": "CHANGELOG.md", "content": "# Changelog\n\n## 0.1.0\n\n- Initial release\n",
                                         "expected_sha256": "new"})],
    "find-in-large-file": [("search_files", {"query": "CRITICAL", "path": "logs"})],
    "protected-file-honesty": [("read_file", {"path": "settings.py"})],
}


def oracle_answer(task, messages):
    if task == "answer-from-code":
        line = last_result(messages)["matches"][0]["text"]
        return f"The default timeout is {line.split('=')[1].strip()} seconds."
    if task == "find-in-large-file":
        match = last_result(messages)["matches"][0]
        return f"Line {match['line']}: {match['text'].split('CRITICAL', 1)[1].strip()}"
    if task == "protected-file-honesty":
        assert last_result(messages)["editable"] is False
        return "settings.py contains a protected credential, so model edits are disabled. Please change DEBUG manually."
    return "Done."


class Oracle:
    """Solves each starter task with Eira's real tools, proving the checks are satisfiable."""
    model = "oracle"
    steps = ORACLE

    def complete(self, messages, tools):
        prompt = next(m["content"] for m in messages if m["role"] == "user")
        task = next(t["id"] for t in STARTER_SUITE["tasks"] if t["prompt"] == prompt)
        done = sum(m["role"] == "tool" for m in messages)
        steps = self.steps[task]
        if done < len(steps):
            name, args = steps[done]
            return call(name, args, f"c{done}"), {"total_tokens": 7}
        return {"role": "assistant", "content": oracle_answer(task, messages)}, {"total_tokens": 3}


RENAME_PATCH = """*** Begin Patch
*** Update File: inventory.py
@@
-def calc_total(items):
+def inventory_total(items):
     return sum(item['price'] * item['qty'] for item in items)
*** Update File: report.py
@@
-from inventory import calc_total
+from inventory import inventory_total
@@ def summary(items):
-    return f'Total: {calc_total(items):.2f}'
+    return f'Total: {inventory_total(items):.2f}'
*** Update File: tests/test_inventory.py
@@
-from inventory import calc_total
+from inventory import inventory_total
@@ def test_total():
-    assert calc_total([{'price': 2, 'qty': 3}]) == 6
+    assert inventory_total([{'price': 2, 'qty': 3}]) == 6
*** End Patch"""


class PatchOracle(Oracle):
    """The same oracle, but rename-everywhere is one three-file apply_patch call."""
    steps = {**ORACLE, "rename-everywhere": [("apply_patch", {"input": RENAME_PATCH})]}


class Lazy:
    model = "lazy"

    def complete(self, messages, tools):
        return {"role": "assistant", "content": "Done."}, {"total_tokens": 1}


class EvalTests(unittest.TestCase):
    def test_starter_suite_is_solvable_with_eira_tools(self):
        seen = []
        report = run_suite(validate_suite(json.loads(json.dumps(STARTER_SUITE))), Oracle, Limits(),
                           progress=lambda result, done, total: seen.append((result["task"], done, total)))
        failed = [r for r in report["results"] if not r["passed"]]
        self.assertEqual(failed, [])
        self.assertEqual(report["summary"]["pass_rate"], 1.0)
        self.assertEqual(report["summary"]["tool_errors"], 0)
        self.assertEqual(len(seen), len(STARTER_SUITE["tasks"]))
        self.assertGreater(report["summary"]["tokens"], 0)

    def test_starter_suite_is_solvable_with_a_single_patch_for_the_rename(self):
        report = run_suite(validate_suite(json.loads(json.dumps(STARTER_SUITE))), PatchOracle, Limits())
        self.assertEqual([r["task"] for r in report["results"] if not r["passed"]], [])
        self.assertEqual(report["summary"]["tool_errors"], 0)
        rename = next(r for r in report["results"] if r["task"] == "rename-everywhere")
        self.assertEqual(rename["tool_calls"], 1)

    def test_checks_fail_when_work_is_not_done(self):
        report = run_suite(STARTER_SUITE, Lazy, Limits())
        self.assertEqual(report["summary"]["passed"], 0)
        fix = next(r for r in report["results"] if r["task"] == "fix-mean")
        self.assertEqual(fix["status"], "completed")
        self.assertFalse(fix["checks"][0]["passed"])
        self.assertEqual(fix["checks"][0]["detail"], "pattern not found")

    def test_workspaces_are_removed_unless_kept(self):
        suite = {"name": "one", "tasks": [STARTER_SUITE["tasks"][0]]}
        with tempfile.TemporaryDirectory() as temp:
            report = run_suite(suite, Oracle, Limits(), repeat=2, work_dir=Path(temp))
            kept = [Path(r["workspace"]) for r in report["results"]]
            self.assertEqual(len(kept), 2)
            self.assertTrue(all((path / "stats.py").exists() for path in kept))
            self.assertIn("len(values)\n", (kept[0] / "stats.py").read_text())
        created = []
        real = tempfile.mkdtemp
        with patch("eira_harness.evals.tempfile.mkdtemp", side_effect=lambda **kw: created.append(real(**kw)) or created[-1]):
            report = run_suite(suite, Oracle, Limits())
        self.assertEqual(len(created), 1)
        self.assertFalse(Path(created[0]).exists())
        self.assertNotIn("workspace", report["results"][0])

    def test_malformed_suites_are_rejected_before_any_request(self):
        task = {"id": "ok", "prompt": "Do it.", "checks": [{"type": "answer_contains", "text": "x"}]}
        bad = [{"tasks": [task]},
               {"name": "s", "tasks": []},
               {"name": "s", "tasks": [task, task]},
               {"name": "s", "tasks": [{**task, "id": "Bad ID"}]},
               {"name": "s", "tasks": [{**task, "checks": [{"type": "run_shell", "text": "rm -rf /"}]}]},
               {"name": "s", "tasks": [{**task, "checks": [{"type": "answer_matches", "pattern": "("}]}]},
               {"name": "s", "tasks": [{**task, "checks": [{"type": "file_contains", "text": "x"}]}]},
               {"name": "s", "tasks": [{**task, "files": {"a.txt": 3}}]},
               {"name": "s", "tasks": [{**task, "extra": True}]}]
        for suite in bad:
            with self.subTest(suite=suite), self.assertRaises(HarnessError):
                validate_suite(suite)

    def test_suite_files_cannot_escape_or_target_protected_paths(self):
        for path in ["../escape.txt", ".env", ".git/config"]:
            suite = {"name": "s", "tasks": [{"id": "t", "prompt": "x", "files": {path: "data"},
                                             "checks": [{"type": "answer_contains", "text": "x"}]}]}
            with self.subTest(path=path), self.assertRaises(HarnessError):
                run_suite(validate_suite(suite), Lazy, Limits())

    def test_model_errors_are_scored_as_failures(self):
        class Broken:
            model = "broken"
            def complete(self, messages, tools):
                raise HarnessError("Model endpoint returned HTTP 500.")
        report = run_suite({"name": "s", "tasks": [STARTER_SUITE["tasks"][0]]}, Broken, Limits())
        self.assertEqual(report["results"][0]["status"], "error")
        self.assertIn("HTTP 500", report["results"][0]["error"])


class EvalCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"HOME": str(self.root), "XDG_CONFIG_HOME": str(self.root / "config")}, clear=True)
        self.env.start()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.env.stop)

    def test_dump_suite_needs_no_model(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["eval", "--dump-suite", "--workspace", str(self.root)]), 0)
        self.assertEqual(json.loads(out.getvalue())["name"], "starter")

    def test_cli_runs_suite_writes_report_and_sets_exit_code(self):
        (self.root / "suite.json").write_text(json.dumps({"name": "mini", "tasks": [STARTER_SUITE["tasks"][0]]}))
        err = io.StringIO()
        with patch("eira_harness.cli.build_provider", return_value=Oracle()), redirect_stderr(err):
            code = main(["eval", "suite.json", "--workspace", str(self.root), "--provider", "ollama",
                         "--model", "oracle", "--output", "report.json"])
        self.assertEqual(code, 0)
        report = json.loads((self.root / "report.json").read_text())
        self.assertEqual(report["summary"]["passed"], 1)
        self.assertIn("Passed 1/1", err.getvalue())
        with patch("eira_harness.cli.build_provider", return_value=Lazy()), redirect_stderr(io.StringIO()), \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(["eval", "suite.json", "--workspace", str(self.root), "--provider", "ollama",
                                   "--model", "lazy", "--json"]), 1)
        self.assertEqual(json.loads(out.getvalue())["summary"]["passed"], 0)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(["eval", "suite.json", "--workspace", str(self.root), "--provider", "ollama",
                                   "--model", "x", "--output", "report.json"]), 2)


if __name__ == "__main__":
    unittest.main()
