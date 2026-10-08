"""Pre-approval syntax guard and optional sandboxed lint feedback (offline, deterministic)."""
import json
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest

from eira_harness.agent import Agent
from eira_harness.cli import build_parser, policy_from
from eira_harness.security import HarnessError, Redactor, Workspace
from eira_harness.store import Store
from eira_harness import syntax
from eira_harness.syntax import context_block, lint_command, parse_lint, scopes
from eira_harness.text import lines
from eira_harness.tools import Policy, Toolbox

try:
    from fakes import fake_docker
except ImportError:  # run as tests.test_syntax_guard from the repository root
    from tests.fakes import fake_docker

INVOICE = (
    "import math\n"
    "\n"
    "\n"
    "class Invoice:\n"
    "    def __init__(self, items):\n"
    "        self.items = items\n"
    "\n"
    "    def total(self):\n"
    "        prices = [\n"
    "            item.price\n"
    "            for item in self.items\n"
    "        ]\n"
    "        return math.fsum(prices)\n"
)


def tool_message(name, args, call_id="call_1"):
    return {"role": "assistant", "content": None, "tool_calls": [{"id": call_id,
            "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


class SequenceProvider:
    model = "test-fixture"

    def __init__(self, messages):
        self.responses = iter(messages)

    def complete(self, messages, tools):
        return next(self.responses), {"total_tokens": 10}


class GuardTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root, Redactor())
        self.session = self.store.create("syntax")
        self.asked = []
        self.answer = True
        self.policy = Policy(approve=lambda name, detail: self.asked.append((name, detail)) or self.answer)
        self.tools = Toolbox(Workspace(self.root), self.store, self.policy, self.session)
        self.events = []
        self.tools.on_event = lambda kind, payload: self.events.append((kind, payload))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def put(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode())
        return path

    def edit(self, path, old, new):
        return self.tools.call("edit_file", {"path": path, "old_string": old, "new_string": new})

    def write(self, path, content, digest="new"):
        return self.tools.call("write_file", {"path": path, "content": content, "expected_sha256": digest})


class PythonGuardTests(GuardTestCase):
    def test_regression_is_rejected_before_approval_with_scoped_context(self):
        target = self.put("inv.py", INVOICE)
        with self.assertRaises(HarnessError) as caught:
            self.edit("inv.py", "            for item in self.items\n        ]\n", "            for item in self.items\n")
        message = str(caught.exception)
        for expected in ("Syntax check failed for inv.py", f"(Python {sys.version_info.major}.{sys.version_info.minor} parser)",
                         "'[' was never closed (line 9, column 18)", "class Invoice:", "def total(self):", "█",
                         "The edit was not applied; the file is unchanged."):
            self.assertIn(expected, message)
        block = message.split("\n", 1)[1].split("\n")
        self.assertEqual(block[:4], ["    ⋮", "    4│class Invoice:", "    ⋮", "    6│        self.items = items"])
        self.assertIn("    9█        prices = [", block)
        self.assertNotIn("import math", message)
        self.assertEqual(self.asked, [])
        self.assertEqual(target.read_bytes(), INVOICE.encode())
        self.assertEqual(self.events, [("syntax_check_failed",
                                        {"path": "inv.py", "language": "python", "line": 9, "rejected": True})])

    def test_rejection_is_journaled_through_the_agent(self):
        self.put("inv.py", INVOICE)
        emitted = []
        provider = SequenceProvider([tool_message("edit_file", {"path": "inv.py", "old_string": "        ]\n", "new_string": ""}),
                                     {"role": "assistant", "content": "done"}])
        agent = Agent(provider, self.store, self.tools, emitted.append)
        self.assertEqual(agent.run("break it")["status"], "completed")
        events = [e["payload"] for e in self.store.events(self.session) if e["kind"] == "syntax_check_failed"]
        self.assertEqual(events, [{"path": "inv.py", "language": "python", "line": 9, "rejected": True}])
        failed = [e for e in emitted if e["event"] == "tool_completed"]
        self.assertFalse(failed[0]["ok"])
        self.assertIn("Syntax check failed for inv.py", failed[0]["error"])
        self.assertEqual((self.root / "inv.py").read_text(), INVOICE)

    def test_warn_mode_applies_and_off_mode_skips(self):
        target = self.put("inv.py", INVOICE)
        self.policy.syntax_guard = "warn"
        result = self.edit("inv.py", "        ]\n", "")
        self.assertNotIn("        ]\n", target.read_text())
        check = result["checks"][0]
        self.assertEqual((check["type"], check["result"], check["rejected"], check["line"]), ("syntax", "error", False, 9))
        self.assertIn("was never closed", check["message"])
        self.assertNotIn("note", check)
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(self.events, [])
        self.policy.syntax_guard = "off"
        target.write_text(INVOICE)
        self.assertNotIn("checks", self.edit("inv.py", "        ]\n", ""))
        self.assertNotIn("checks", self.write("other.py", "def broken(:\n"))

    def test_new_and_already_broken_files_get_warnings(self):
        result = self.write("new.py", "def broken(:\n    pass\n")
        self.assertTrue((self.root / "new.py").exists())
        self.assertEqual((result["checks"][0]["result"], result["checks"][0]["rejected"], result["checks"][0]["note"]),
                         ("error", False, "new file"))
        result = self.edit("new.py", "    pass\n", "    return 1\n")
        self.assertEqual(result["checks"][0]["note"], "file already failed to parse before this edit")
        self.assertIn("return 1", (self.root / "new.py").read_text())
        self.assertEqual(self.events, [])

    def test_write_file_replacing_valid_file_with_broken_one_is_rejected(self):
        target = self.put("ok.py", "x = 1\n")
        digest = self.tools.read_file("ok.py")["sha256"]
        with self.assertRaisesRegex(HarnessError, "Syntax check failed for ok.py"):
            self.write("ok.py", "x = (\n", digest)
        self.assertEqual(target.read_text(), "x = 1\n")
        self.assertEqual(self.asked, [])

    def test_valid_edit_reports_ok(self):
        self.put("inv.py", INVOICE)
        result = self.edit("inv.py", "math.fsum(prices)", "sum(prices)")
        self.assertEqual(result["checks"], [{"type": "syntax", "path": "inv.py", "language": "python", "result": "ok"}])
        result = self.write("stub.pyi", "def f() -> int: ...\n")
        self.assertEqual(result["checks"][0]["result"], "ok")

    def test_parsing_never_executes_or_warns(self):
        marker = self.root / "executed"
        code = f"import os\nopen({str(marker)!r}, 'w').close()\npattern = '\\d'\n"
        self.put("side.py", code)
        result = self.edit("side.py", "pattern", "other")
        self.assertEqual(result["checks"][0]["result"], "ok")
        self.assertFalse(marker.exists())

    def test_indentation_tab_and_nul_errors(self):
        self.put("ind.py", "def f():\n    return 1\n")
        with self.assertRaisesRegex(HarnessError, r"(?s)Syntax check failed for ind.py.*line 2"):
            self.edit("ind.py", "    return 1", "return 1")
        with self.assertRaisesRegex(HarnessError, r"Syntax check failed for ind.py.*\(line 1"):
            self.edit("ind.py", "def f", "\x00def f")
        self.assertEqual((self.root / "ind.py").read_text(), "def f():\n    return 1\n")

    def test_deep_nesting_is_not_checked(self):
        for name, content in (("deep.py", "a" + ".b" * 100_000 + "\n"), ("neg.py", "-" * 100_000 + "1\n"),
                              ("deep.json", "[" * 100_000 + "]" * 100_000), ("deep.toml", "a = " + "[" * 100_000 + "]" * 100_000 + "\n")):
            with self.subTest(name=name):
                result = self.write(name, content)
                self.assertEqual(result["checks"][0]["result"], "not checked")
                self.assertEqual((self.root / name).read_text(), content)
        self.put("grow.py", "x = 1\n")
        result = self.edit("grow.py", "x = 1", "x = a" + ".b" * 40_000)  # new_string is capped at 100,000 chars
        self.assertEqual(result["checks"][0]["result"], "not checked")
        self.assertEqual(self.events, [])

    def test_oversized_text_is_not_checked(self):
        self.assertEqual(syntax.guard("big.py", None, "x" * 5_000_001, "reject", lambda *a, **k: None)["result"], "not checked")
        self.assertIsNone(syntax.guard("a.js", "", "{", "reject", lambda *a, **k: None))
        self.assertIsNone(syntax.guard("a.py", "x = 1", "x = (", "off", lambda *a, **k: None))

    def test_unknown_mode_fails_safe_as_reject(self):
        with self.assertRaises(HarnessError):
            syntax.guard("a.py", "x = 1\n", "x = (\n", "bogus", lambda *a, **k: None)


class DataFormatGuardTests(GuardTestCase):
    def test_json_trailing_comma_is_rejected(self):
        original = '{\n  "name": "demo",\n  "version": "1.0.0"\n}\n'
        target = self.put("package.json", original)
        with self.assertRaises(HarnessError) as caught:
            self.edit("package.json", '"1.0.0"', '"1.0.0",')
        message = str(caught.exception)
        self.assertIn("Syntax check failed for package.json (JSON, Python", message)
        self.assertIn("(line 4, column 1)", message)
        self.assertIn("    4█}", message)
        self.assertEqual(target.read_text(), original)
        self.assertEqual(self.asked, [])
        self.assertEqual(self.events[0][1]["language"], "json")

    def test_jsonc_file_can_be_edited_with_a_warning(self):
        self.put("tsconfig.json", '{\n  // compiler options\n  "compilerOptions": {"strict": true}\n}\n')
        result = self.edit("tsconfig.json", '"strict": true', '"strict": false')
        check = result["checks"][0]
        self.assertEqual((check["language"], check["result"], check["rejected"], check["note"]),
                         ("json", "error", False, "file already failed to parse before this edit"))
        self.assertIn('"strict": false', (self.root / "tsconfig.json").read_text())

    def test_toml_unclosed_table_header_is_rejected_with_position(self):
        original = '[project]\nname = "demo"\n\n[tool.ruff]\nline-length = 100\n'
        target = self.put("pyproject.toml", original)
        broken = original.replace("[tool.ruff]", "[tool.ruff")
        try:
            tomllib.loads(broken)
        except tomllib.TOMLDecodeError as exc:
            expected = str(exc)
        position = expected[expected.rindex("(at line"):].strip("()").replace("at ", "")
        with self.assertRaises(HarnessError) as caught:
            self.edit("pyproject.toml", "[tool.ruff]", "[tool.ruff")
        message = str(caught.exception)
        self.assertIn("Syntax check failed for pyproject.toml (TOML, Python", message)
        self.assertIn(f"({position})", message)
        self.assertIn("line 4", message)
        self.assertEqual(target.read_text(), original)

    def test_toml_error_at_end_of_document_names_a_line(self):
        self.put("cfg.toml", "a = [1]\n")
        with self.assertRaisesRegex(HarnessError, r"\(line \d+, column \d+\)"):
            self.edit("cfg.toml", "a = [1]", "a = [1,")

    def test_unchecked_languages_produce_no_checks(self):
        self.put("app.js", "let x = 1;\n")
        self.put("ci.yml", "a: 1\n")
        self.assertNotIn("checks", self.edit("app.js", "1;", "(;"))
        self.assertNotIn("checks", self.edit("ci.yml", "a: 1", "a: [1"))
        self.assertNotIn("checks", self.write("data.xml", "<a>"))


class FormatTests(unittest.TestCase):
    def test_scope_finder(self):
        rows = lines(INVOICE)
        self.assertEqual(scopes(rows, 9), [4, 8])
        self.assertEqual(scopes(rows, 1), [])
        nested = "class A:\n    x = 1\n    class B:\n        async  def go(self):\n\n            await y(\n"
        self.assertEqual(scopes(lines(nested), 5), [1, 3, 4])

    def test_block_limits(self):
        long_line = "x" * 1000
        block = context_block("\n".join([long_line] * 10), 5, "json")
        self.assertTrue(all(len(row) <= 206 for row in block.split("\n")))
        deep = "".join("    " * depth + f"def f{depth}():\n" for depth in range(60)) + "    " * 60 + "x = (\n"
        block = context_block(deep, 61, "python")
        self.assertLessEqual(len(block.split("\n")), 40)
        self.assertLessEqual(len(block), 3000)
        self.assertIn("   61█", block)
        self.assertEqual(context_block("", 1, "python"), "")


class LintTests(GuardTestCase):
    def setUp(self):
        super().setUp()
        self.log = self.root / "docker.jsonl"
        self.policy.approve_writes = True
        self.policy.shell_mode = "docker"
        self.policy.lint_commands = parse_lint(['*.py=sh -c "echo lint-failed; exit 1"'])
        self.put("pkg/a.py", "x = 1\n")

    def lint_checks(self, result):
        return [check for check in result.get("checks", []) if check["type"] == "lint"]

    def test_lint_output_is_attached(self):
        with fake_docker(self.log):
            result = self.edit("pkg/a.py", "x = 1", "x = 2")
        lint = self.lint_checks(result)
        self.assertEqual(len(lint), 1)
        self.assertEqual((lint[0]["path"], lint[0]["exit_code"]), ("pkg/a.py", 1))
        self.assertIn("lint-failed", lint[0]["output"])
        self.assertEqual(lint[0]["command"], "sh -c \"echo lint-failed; exit 1\" pkg/a.py")
        self.assertEqual(result["checks"][0]["type"], "syntax")
        self.assertEqual([name for name, _ in self.asked], ["shell"])
        run = json.loads(self.log.read_text().splitlines()[0])
        self.assertIn("--network=none", run)
        self.assertEqual((self.root / "pkg/a.py").read_text(), "x = 2\n")

    def test_lint_skipped_when_shell_disabled_or_denied(self):
        self.policy.shell_mode = "disabled"
        result = self.edit("pkg/a.py", "x = 1", "x = 2")
        self.assertEqual(self.lint_checks(result),
                         [{"type": "lint", "path": "pkg/a.py", "status": "skipped", "reason": "shell is disabled"}])
        self.assertEqual((self.root / "pkg/a.py").read_text(), "x = 2\n")
        self.policy.shell_mode, self.answer = "docker", False
        with fake_docker(self.log):
            result = self.write("pkg/b.py", "y = 1\n")
        lint = self.lint_checks(result)
        self.assertEqual(lint[0]["status"], "skipped")
        self.assertIn("denied", lint[0]["reason"])
        self.assertEqual((self.root / "pkg/b.py").read_text(), "y = 1\n")
        self.assertFalse(self.log.exists())

    def test_unmatched_unchanged_and_failed_edits_do_not_lint(self):
        with fake_docker(self.log):
            self.assertEqual(self.lint_checks(self.write("notes.md", "hi\n")), [])
            digest = self.tools.read_file("pkg/a.py")["sha256"]
            self.assertEqual(self.tools.call("write_file", {"path": "pkg/a.py", "content": "x = 1\n",
                                                            "expected_sha256": digest}), {"path": "pkg/a.py", "changed": False})
            with self.assertRaises(HarnessError):
                self.edit("pkg/a.py", "x = 1", "x = (")
        self.assertEqual(self.asked, [])
        self.assertFalse(self.log.exists())

    def test_at_most_five_lint_runs_per_call(self):
        self.policy.shell_mode = "disabled"
        self.policy.lint_commands = parse_lint([f"*.py=check{n}" for n in range(8)])
        self.assertEqual(len(self.lint_checks(self.edit("pkg/a.py", "x = 1", "x = 2"))), 5)

    def test_patch_shaped_results_lint_non_deleted_paths(self):
        self.policy.shell_mode = "disabled"
        hook = syntax.lint_hook(self.tools)
        result = hook("apply_patch", {}, {"files": [{"path": "a.py", "op": "update"}, {"path": "gone.py", "op": "delete"},
                                                    {"path": "old.py", "op": "move", "to": "new.py"}], "checks": []})
        self.assertEqual([check["path"] for check in result["checks"]], ["a.py", "new.py"])
        self.assertEqual(hook("read_file", {}, {"path": "a.py"}), {"path": "a.py"})

    def test_command_templates_and_parsing(self):
        self.assertEqual(lint_command("ruff check {path}", "a b.py"), "ruff check 'a b.py'")
        self.assertEqual(lint_command("mypy", "src/x.py"), "mypy src/x.py")
        self.assertEqual(lint_command("mypy", "-x.py"), "mypy ./-x.py")
        self.assertEqual(lint_command("ruff {path} && echo {path}", "$(id).py"), "ruff '$(id).py' && echo '$(id).py'")
        self.assertEqual(parse_lint(["*.py = ruff check {path}", "*.json=jq . {path}"]),
                         (("*.py", "ruff check {path}"), ("*.json", "jq . {path}")))
        for bad in ("ruff", "=ruff", "*.py=", " = "):
            with self.subTest(bad=bad), self.assertRaisesRegex(HarnessError, "GLOB=COMMAND"):
                parse_lint([bad])

    def test_cli_flags_reach_the_policy(self):
        args = build_parser().parse_args(["run", "task", "--syntax-guard", "warn", "--lint-cmd", "*.py=ruff check {path}",
                                          "--lint-cmd", "*.toml=taplo check"])
        policy = policy_from(args)
        self.assertEqual(policy.syntax_guard, "warn")
        self.assertEqual(policy.lint_commands, (("*.py", "ruff check {path}"), ("*.toml", "taplo check")))
        defaults = policy_from(build_parser().parse_args(["chat"]))
        self.assertEqual((defaults.syntax_guard, defaults.lint_commands), ("reject", ()))
        with self.assertRaises(SystemExit), open("/dev/null", "w") as sink:
            stderr, sys.stderr = sys.stderr, sink
            try:
                build_parser().parse_args(["run", "task", "--syntax-guard", "maybe"])
            finally:
                sys.stderr = stderr


class ApplyPatchGuardTests(GuardTestCase):
    def setUp(self):
        super().setUp()
        if "apply_patch" not in self.tools.registry:
            self.skipTest("apply_patch is not registered")

    def test_multi_file_patch_breaking_python_is_rejected_whole(self):
        first, second = self.put("a.py", "x = 1\n"), self.put("b.py", "y = [1]\n")
        patch = ("*** Begin Patch\n*** Update File: a.py\n@@\n-x = 1\n+x = 2\n"
                 "*** Update File: b.py\n@@\n-y = [1]\n+y = [1\n*** End Patch\n")
        with self.assertRaisesRegex(HarnessError, "Syntax check failed for b.py"):
            self.tools.call("apply_patch", {"input": patch})
        self.assertEqual((first.read_text(), second.read_text()), ("x = 1\n", "y = [1]\n"))
        self.assertEqual(self.asked, [])


if __name__ == "__main__":
    unittest.main()
