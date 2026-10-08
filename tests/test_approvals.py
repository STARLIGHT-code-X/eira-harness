"""Sandboxed shell autorun: the decision table, the destructive heuristic, alerts and the CLI flag."""
from contextlib import contextmanager, nullcontext, redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from eira_harness.agent import Agent
from eira_harness.approvals import Decision, decide_shell, destructive, journaled_alerts
from eira_harness.cli import build_parser, main, policy_from, shell_status
from eira_harness.security import HarnessError, Redactor, Workspace
from eira_harness.store import Store
from eira_harness.terminal import Terminal
from eira_harness.tools import Policy, Toolbox

try:
    from fakes import fake_docker
except ImportError:  # run as tests.test_approvals from the repository root
    from tests.fakes import fake_docker

# shell-protected-paths provides the protected mount plan and post-run detection.
SANDBOX = importlib.util.find_spec("eira_harness.sandbox") is not None
POSIX_SH = os.name == "posix" and Path("/bin/sh").exists()


def protect(toolbox):
    """Stand in for shell-protected-paths until it merges, using its documented contract.

    Its _shell_plan returns plans with 'protected': True, and its post-run check
    appends newly created config paths to shell_alerts and notifies
    sandbox_protected_path_created. Once it is present this does nothing, so
    these tests then exercise the real plan and detection.
    """
    if SANDBOX:
        return toolbox
    plan, run = toolbox._shell_plan, toolbox._shell_run

    def protected_plan(command, timeout):
        return {**plan(command, timeout), "protected": True}

    def detecting_run(command, timeout, current):
        before = (toolbox.workspace.root / ".vscode").exists()
        raw = run(command, timeout, current)
        if not before and (toolbox.workspace.root / ".vscode").exists():
            toolbox.shell_alerts.extend([".vscode"])
            toolbox.notify("sandbox_protected_path_created", paths=[".vscode"])
        return raw
    toolbox._shell_plan, toolbox._shell_run = protected_plan, detecting_run
    return toolbox


@contextmanager
def protected_toolboxes():
    """protect() for every Toolbox built inside the block, such as the CLI's."""
    if SANDBOX:
        yield
        return
    original = Toolbox.__init__

    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        protect(self)
    with patch.object(Toolbox, "__init__", init):
        yield


ASK = ["rm -rf .", "rm -rf ./*", "rm -fr /workspace", "rm -r -f *", 'rm -rf "$DIR"/*', "bash -c 'rm -rf /workspace'",
       "sh -lc 'cd x && rm -rf ..'", "git clean -fdx", "find . -name '*.pyc' -delete", "FOO=1 timeout 5 rm -rf ~",
       "rm -rf -- *"]
AUTO = ["rm -rf build", "rm -rf build/*", "rm -f a.txt", "git clean -n", "git -C . clean -fd",
        "find src -name '*.pyc' -delete", "python -m pytest -q", "echo 'unterminated", "make test && rm -rf dist/*"]


class DestructiveTests(unittest.TestCase):
    def test_prototype_cases(self):
        self.assertEqual(len(ASK) + len(AUTO), 20)
        for command in ASK:
            with self.subTest(command=command):
                self.assertIsInstance(destructive(command), str)
        for command in AUTO:
            with self.subTest(command=command):
                self.assertIsNone(destructive(command))

    def test_reasons_name_the_pattern(self):
        self.assertEqual(destructive("rm -rf ."), 'recursive rm of "."')
        self.assertEqual(destructive("FOO=1 timeout 5 rm -rf ~"), 'recursive rm of "~"')
        self.assertIn("git clean", destructive("git clean -fdx"))
        self.assertEqual(destructive("find . -name '*.pyc' -delete"), 'find -delete in "."')
        self.assertEqual(destructive("shred notes.txt"), "shred overwrites files")

    def test_separators_wrappers_and_nesting(self):
        for command in ["(cd x; rm -rf ..)", "echo hi\nrm -rf .", "rm -rf \\\n .", "ls | xargs rm -rf .",
                        "nice -n 5 rm -rf .", "sudo -u root rm -Rf /", "timeout -s KILL 5 rm -rf .", "/bin/rm -rf .",
                        "env FOO=1 bash -o pipefail -c 'rm -rf .'", "rm -rf ../..", "rm -rf /workspace//",
                        "rm -rf `pwd`", "rm -rf $(pwd)", "rm --recursive $HOME", "find -delete",
                        "find / -exec rm {} \\;", "find . \\( -name a -o -name b \\) -delete", "( find . -delete )",
                        "git -C . clean -fx", "true; sh -c \"bash -c 'rm -rf .'\""]:
            with self.subTest(command=command):
                self.assertIsInstance(destructive(command), str)
        for command in ["git clean -fdxn", "find . -name x", "rm . ", "sh script.sh", "echo ';' && ls",
                        "rm -rf dist 2>&1 | tee log", "", "rm -rf", "git status"]:
            with self.subTest(command=command):
                self.assertIsNone(destructive(command))

    def test_nesting_stops_at_depth_three(self):
        inner = "rm -rf ."
        for _ in range(3):
            inner = "sh -c " + json.dumps(inner)
        self.assertIsInstance(destructive(inner), str)
        self.assertIsNone(destructive("sh -c " + json.dumps(inner)))


class DecisionTableTests(unittest.TestCase):
    def policy(self, **changes):
        return Policy(**{"shell_mode": "docker", "shell_approval": "sandboxed", **changes})

    def test_table(self):
        protected = {"detail": "d", "protected": True}
        self.assertEqual(decide_shell(self.policy(shell_approval="always"), protected, "ls", []),
                         Decision("ask", "approval mode always"))
        self.assertEqual(decide_shell(self.policy(), protected, "ls", []), Decision("auto", "sandboxed"))
        for plan in ({"detail": "d"}, {"detail": "d", "protected": "yes"}, {"detail": "d", "protected": 1}, None):
            with self.subTest(plan=plan):
                self.assertEqual(decide_shell(self.policy(), plan, "ls", []),
                                 Decision("ask", "sandbox protections unavailable"))
        self.assertEqual(decide_shell(self.policy(shell_mode="disabled"), protected, "ls", []).action, "ask")
        decision = decide_shell(self.policy(), protected, "ls", [".vscode", ".vscode", ".husky"])
        self.assertEqual(decision, Decision("ask", "a previous command created protected config paths: "
                                                   ".vscode, .husky; review before continuing"))
        self.assertEqual(decide_shell(self.policy(), protected, "rm -rf .", []), Decision("ask", 'recursive rm of "."'))
        for mode in ("sandboxed", "always"):
            with self.subTest(mode=mode), self.assertRaisesRegex(HarnessError, "^Denied by read-only policy.$"):
                decide_shell(self.policy(read_only=True, shell_approval=mode), protected, "ls", [])

    def test_journaled_alerts_reset_on_human_approval(self):
        def event(kind, **payload):
            return {"kind": kind, "payload": payload}
        created = event("sandbox_protected_path_created", paths=[".vscode"])
        approved = event("approval_decided", tool="shell", decision="approved", reason="r", command_sha256="x")
        self.assertEqual(journaled_alerts([created]), [".vscode"])
        self.assertEqual(journaled_alerts([created, approved]), [])
        self.assertEqual(journaled_alerts([approved, created, created]), [".vscode"])
        for other in (event("approval_decided", tool="shell", decision="auto"),
                      event("approval_decided", tool="shell", decision="denied"),
                      event("approval_decided", tool="apply_patch", decision="approved")):
            with self.subTest(other=other):
                self.assertEqual(journaled_alerts([created, other]), [".vscode"])
        self.assertEqual(journaled_alerts([event("sandbox_protected_path_created", paths="x"), {"kind": "x"}, None]), [])


class ShellTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root, Redactor())
        self.session = self.store.create("approvals")
        self.asked = []
        self.answer = True
        self.policy = Policy(approve=lambda name, detail: self.asked.append((name, detail)) or self.answer,
                             shell_mode="docker", shell_approval="sandboxed")
        self.log = self.root.parent / f"{self.root.name}-docker.jsonl"
        self.addCleanup(lambda: self.log.unlink(missing_ok=True))
        self.tools = self.toolbox()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def toolbox(self):
        tools = protect(Toolbox(Workspace(self.root), self.store, self.policy, self.session))
        # Journal tool events as an Agent would.
        tools.on_event = lambda kind, payload: self.store.event(self.session, kind, payload)
        return tools

    def runs(self):
        if not self.log.exists():
            return []
        return [args for args in map(json.loads, self.log.read_text().splitlines()) if args[0] == "run"]

    def decisions(self):
        return [(e["payload"]["decision"], e["payload"]["reason"])
                for e in self.store.events(self.session) if e["kind"] == "approval_decided"]


@unittest.skipUnless(POSIX_SH, "the fake Docker shim needs /bin/sh")
class SandboxedShellTests(ShellTestCase):
    def test_protected_command_runs_without_approval(self):
        with fake_docker(self.log):
            result = self.tools.call("shell", {"command": "echo ok > out.txt"})
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(self.asked, [])
        self.assertEqual((self.root / "out.txt").read_text(), "ok\n")
        events = [e["payload"] for e in self.store.events(self.session) if e["kind"] == "approval_decided"]
        self.assertEqual(events, [{"tool": "shell", "decision": "auto", "reason": "sandboxed",
                                   "command_sha256": __import__("hashlib").sha256(b"echo ok > out.txt").hexdigest()}])

    def test_auto_decision_is_journaled_through_an_agent(self):
        class Provider:
            model = "fixture"
            calls = iter([{"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function",
                           "function": {"name": "shell", "arguments": json.dumps({"command": "printf hi"})}}]},
                          {"role": "assistant", "content": "done"}])

            def complete(self, messages, tools):
                return next(self.calls), {"total_tokens": 1}
        emitted = []
        tools = protect(Toolbox(Workspace(self.root), self.store, self.policy, self.session))
        with fake_docker(self.log):
            self.assertEqual(Agent(Provider(), self.store, tools, emitted.append).run("go")["status"], "completed")
        self.assertEqual(self.asked, [])
        self.assertEqual(self.decisions(), [("auto", "sandboxed")])
        kinds = [e["event"] for e in emitted]
        self.assertLess(kinds.index("tool_started"), kinds.index("approval_decided"))
        self.assertLess(kinds.index("approval_decided"), kinds.index("tool_completed"))
        stream = io.StringIO()
        terminal = Terminal(stream)
        for event in emitted:
            terminal.emit(event)
        self.assertIn("  · running in the sandbox without approval", stream.getvalue())

    def test_destructive_command_asks_with_a_reason(self):
        self.answer = False
        (self.root / "keep.txt").write_text("keep")
        with fake_docker(self.log):
            with self.assertRaisesRegex(HarnessError, "Action denied"):
                self.tools.call("shell", {"command": "rm -rf ."})
        self.assertEqual(len(self.asked), 1)
        self.assertTrue(self.asked[0][1].endswith('\nReason for review: recursive rm of "."'))
        self.assertEqual(self.runs(), [])
        self.assertTrue((self.root / "keep.txt").exists())
        self.assertEqual(self.decisions(), [("denied", 'recursive rm of "."')])

    def test_headless_default_denies_destructive_commands(self):
        self.policy.approve = Policy().approve
        with fake_docker(self.log):
            with self.assertRaisesRegex(HarnessError, "Action denied"):
                self.tools.shell("git clean -fdx")
            self.assertEqual(self.tools.shell("printf fine")["output"], "fine")
        self.assertEqual(len(self.runs()), 1)

    def test_trust_handoff_alert_asks_until_a_human_approves(self):
        with fake_docker(self.log):
            result = self.tools.shell("mkdir .vscode && echo {} > .vscode/tasks.json")
            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(self.asked, [])
            self.assertEqual(self.tools.shell_alerts, [".vscode"])
            self.answer = False
            with self.assertRaisesRegex(HarnessError, "Action denied"):
                self.tools.shell("echo next")
            self.assertIn("Reason for review: a previous command created protected config paths: .vscode; "
                          "review before continuing", self.asked[-1][1])
            # A denial is not an acknowledgement.
            self.assertEqual(self.tools.shell_alerts, [".vscode"])
            self.answer = True
            self.assertEqual(self.tools.shell("printf next")["output"], "next")
            self.assertEqual(len(self.asked), 2)
            self.assertEqual(self.tools.shell_alerts, [])
            self.assertEqual(self.tools.shell("printf again")["output"], "again")
        self.assertEqual(len(self.asked), 2)
        self.assertEqual([d for d, _ in self.decisions()], ["auto", "denied", "approved", "auto"])

    def test_alerts_survive_a_new_toolbox_and_resume(self):
        with fake_docker(self.log):
            self.tools.shell("mkdir .vscode")
            # chat builds a new Toolbox for every prompt; resume does too.
            second = self.toolbox()
            self.assertEqual(second.shell_alerts, [])
            self.assertEqual(second.shell("printf x")["output"], "x")
            self.assertEqual(len(self.asked), 1)
            self.assertIn(".vscode", self.asked[0][1])
            third = self.toolbox()
            self.assertEqual(third.shell("printf y")["output"], "y")
        self.assertEqual(len(self.asked), 1)
        self.assertEqual([d for d, _ in self.decisions()], ["auto", "approved", "auto"])

    def test_alerts_from_the_journal_without_detection(self):
        self.store.event(self.session, "sandbox_protected_path_created", {"paths": [".github/workflows"]})
        tools = self.toolbox()
        self.answer = False
        with fake_docker(self.log), self.assertRaisesRegex(HarnessError, "denied"):
            tools.shell("printf x")
        self.assertIn(".github/workflows", self.asked[0][1])
        self.assertEqual(self.runs(), [])

    def test_unprotected_plan_asks(self):
        self.tools._shell_plan = lambda command, timeout: {"detail": "Mode: docker"}
        self.answer = False
        with fake_docker(self.log), self.assertRaisesRegex(HarnessError, "denied"):
            self.tools.shell("printf x")
        self.assertEqual(self.asked, [("shell", "Mode: docker\nReason for review: sandbox protections unavailable")])
        self.assertEqual(self.runs(), [])

    def test_read_only_wins_in_sandboxed_mode(self):
        self.policy.read_only = True
        with fake_docker(self.log), self.assertRaisesRegex(HarnessError, "^Denied by read-only policy.$"):
            self.tools.shell("printf x")
        self.assertEqual((self.asked, self.runs(), self.decisions()), ([], [], []))

    def test_always_mode_asks_for_every_command(self):
        self.policy.shell_approval = "always"
        with fake_docker(self.log):
            self.tools.shell("printf a")
            self.tools.shell("printf b")
        self.assertEqual(len(self.asked), 2)
        # The sandbox's protection summary follows the command in the approval text.
        self.assertIn("Command:\nprintf a\n", self.asked[0][1])
        self.assertNotIn("Reason for review", self.asked[1][1])
        self.assertEqual(self.decisions(), [("approved", "approval mode always")] * 2)

    def test_shell_routed_patch_still_needs_write_approval(self):
        if "apply_patch" not in self.tools.registry:
            self.skipTest("apply-patch has not merged")
        self.answer = False
        command = "apply_patch <<'EOF'\n*** Begin Patch\n*** Add File: a.txt\n+hi\n*** End Patch\nEOF"
        with fake_docker(self.log), self.assertRaises(HarnessError):
            self.tools.call("shell", {"command": command})
        self.assertTrue(self.asked and all(name != "shell" for name, _ in self.asked))
        self.assertIn("a.txt", self.asked[0][1])
        self.assertFalse((self.root / "a.txt").exists())
        self.assertEqual(self.runs(), [])


@unittest.skipUnless(SANDBOX and POSIX_SH, "shell-protected-paths has not merged")
class RealDetectionTests(ShellTestCase):
    def test_detected_config_path_makes_the_next_command_ask(self):
        with fake_docker(self.log):
            result = self.tools.shell("mkdir .vscode && echo {} > .vscode/tasks.json")
            self.assertEqual(result.get("protected_paths_created"), [".vscode"])
            self.answer = False
            with self.assertRaises(HarnessError):
                self.tools.shell("printf x")
        self.assertIn(".vscode", self.asked[0][1])


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def test_flag_defaults_and_policy(self):
        args = build_parser().parse_args(["run", "x"])
        self.assertEqual((args.shell_approval, policy_from(args).shell_approval), ("always", "always"))
        args = build_parser().parse_args(["chat", "--shell", "docker", "--shell-approval", "sandboxed"])
        self.assertEqual(policy_from(args).shell_approval, "sandboxed")
        self.assertEqual(build_parser().parse_args(["setup", "--shell-approval", "sandboxed"]).shell_approval, "sandboxed")
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args(["run", "x", "--shell-approval", "never"])

    def test_status_text(self):
        def status(*argv):
            return shell_status(build_parser().parse_args(["chat", *argv]))
        self.assertEqual(status(), "disabled")
        self.assertEqual(status("--shell", "docker"), "approve each command")
        self.assertEqual(status("--shell", "docker", "--shell-approval", "sandboxed"),
                         "commands run automatically inside the protected sandbox; "
                         "destructive commands and trust-handoff alerts still ask")
        self.assertEqual(status("--shell", "docker", "--read-only"), "denied by read-only policy")

    def test_banner_shows_auto_in_sandbox(self):
        stream = io.StringIO()
        Terminal(stream).banner("/tmp/p", "ollama", "m", "abc", shell="docker (auto in sandbox)")
        self.assertIn("shell: docker (auto in sandbox)", stream.getvalue())
        self.assertNotIn("truncated", stream.getvalue())

    def test_sandboxed_requires_docker(self):
        err = io.StringIO()
        with patch("eira_harness.cli.build_provider") as build, redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = main(["run", "x", "--workspace", str(self.root), "--provider", "ollama", "--model", "m",
                         "--shell-approval", "sandboxed"])
        self.assertEqual(code, 2)
        self.assertIn("--shell-approval sandboxed requires --shell docker", err.getvalue())
        build.assert_not_called()

    @unittest.skipUnless(POSIX_SH, "the fake Docker shim needs /bin/sh")
    def test_headless_run_with_piped_stdin(self):
        seen = []

        class Scripted:
            model = "m"
            replies = iter([{"role": "assistant", "content": None, "tool_calls": [{"id": "s1", "type": "function",
                             "function": {"name": "shell", "arguments": json.dumps({"command": "echo ok > out.txt"})}}]},
                            {"role": "assistant", "content": "done"}])

            def complete(self, messages, tools):
                seen.append(messages)
                return next(self.replies), {"total_tokens": 1}
        out = io.StringIO()
        with fake_docker(), protected_toolboxes(), patch("sys.stdin.isatty", return_value=False), \
                patch("eira_harness.cli.build_provider", return_value=Scripted()), \
                redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = main(["run", "x", "--workspace", str(self.root), "--provider", "ollama", "--model", "m",
                         "--shell", "docker", "--shell-approval", "sandboxed", "--json"])
        self.assertEqual(code, 0)
        tool_message = seen[-1][-1]
        self.assertEqual(tool_message["role"], "tool")
        self.assertEqual(json.loads(tool_message["content"])["result"]["exit_code"], 0)
        self.assertEqual((self.root / "out.txt").read_text(), "ok\n")
        events = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertIn({"tool": "shell", "decision": "auto", "reason": "sandboxed"},
                      [{k: e[k] for k in ("tool", "decision", "reason")} for e in events if e["event"] == "approval_decided"])

    @unittest.skipUnless(POSIX_SH, "the fake Docker shim needs /bin/sh")
    def test_headless_always_mode_still_denies(self):
        class Scripted:
            model = "m"
            replies = iter([{"role": "assistant", "content": None, "tool_calls": [{"id": "s1", "type": "function",
                             "function": {"name": "shell", "arguments": json.dumps({"command": "echo ok > out.txt"})}}]},
                            {"role": "assistant", "content": "done"}])

            def complete(self, messages, tools):
                return next(self.replies), {"total_tokens": 1}
        with fake_docker(), protected_toolboxes(), patch("sys.stdin.isatty", return_value=False), \
                patch("eira_harness.cli.build_provider", return_value=Scripted()), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = main(["run", "x", "--workspace", str(self.root), "--provider", "ollama", "--model", "m",
                         "--shell", "docker", "--json"])
        self.assertEqual(code, 0)
        self.assertFalse((self.root / "out.txt").exists())


if __name__ == "__main__":
    unittest.main()
