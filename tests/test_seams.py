"""Integration seams added before parallel feature work; all behavior-neutral by default."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile
import unittest

from eira_harness.agent import Agent, Limits
from eira_harness.cli import build_parser, limits_from, policy_from
from eira_harness.security import HarnessError, Redactor, Workspace
from eira_harness.store import Store
from eira_harness.text import lines, split_lines
from eira_harness.tools import SHELL_CAPTURE_BYTES, Policy, Tool, Toolbox, _run_capture

try:
    from fakes import fake_docker
except ImportError:  # run as tests.test_seams from the repository root
    from tests.fakes import fake_docker

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def tool_message(name, args, call_id="call_1"):
    return {"role": "assistant", "content": None, "tool_calls": [{"id": call_id,
            "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


class SequenceProvider:
    model = "test-fixture"

    def __init__(self, messages):
        self.responses = iter(messages)

    def complete(self, messages, tools):
        return next(self.responses), {"total_tokens": 10}


class SeamTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root, Redactor(extra=("fixture-protected-value",)))
        self.session = self.store.create("seams")
        self.asked = []
        self.answer = True
        self.policy = Policy(approve=lambda name, detail: self.asked.append((name, detail)) or self.answer)
        self.tools = Toolbox(Workspace(self.root), self.store, self.policy, self.session)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()


class ToolMetadataTests(SeamTestCase):
    def test_every_builtin_declares_effects(self):
        expected = {"list_files": {"read"}, "read_file": {"read"}, "search_files": {"read"}, "backtest_sma": {"read"},
                    "edit_file": {"read", "write"}, "write_file": {"read", "write"}, "fetch_url": {"network"},
                    "market_prices": {"network"}, "shell": {"exec", "write"}, "remember": {"memory"}, "set_plan": set()}
        self.assertEqual({name: set(tool.effects) for name, tool in self.tools.registry.items()}, expected)
        self.assertTrue(all(type(tool.effects) is frozenset for tool in self.tools.registry.values()))
        self.assertTrue(self.tools.mutating("edit_file"))
        self.assertTrue(self.tools.mutating("shell"))
        self.assertFalse(self.tools.mutating("read_file"))
        self.assertFalse(self.tools.mutating("set_plan"))
        with self.assertRaises(HarnessError):
            self.tools.mutating("missing")

    def test_unknown_effects_are_rejected(self):
        for effects in ({"teleport"}, {"read", "teleport"}, "read", ["read"], None):
            with self.subTest(effects=effects), self.assertRaises(HarnessError):
                self.tools.register(Tool(f"t{len(self.tools.registry)}", "x", {}, [], lambda: {}, effects=effects))
        self.tools.register(Tool("plugin", "x", {}, [], lambda: {}, effects={"read", "network"}))
        self.assertEqual(self.tools.registry["plugin"].effects, frozenset({"read", "network"}))
        self.assertFalse(self.tools.mutating("plugin"))
        self.tools.register(Tool("legacy", "x", {}, [], lambda: {}))
        self.assertEqual(self.tools.registry["legacy"].effects, frozenset())

    def test_schemas_match_golden_bytes(self):
        golden = (FIXTURES / "tool_schemas.json").read_text(encoding="utf-8")
        self.assertEqual(json.dumps(self.tools.schemas(), indent=1, ensure_ascii=False) + "\n", golden)
        self.tools.register(Tool("plugin", "x", {}, [], lambda: {}, effects={"read"}, describe=lambda a: "x"))
        self.assertEqual(set(self.tools.schemas()[-1]["function"]), {"name", "description", "parameters"})

    def test_describe_callbacks(self):
        self.tools.register(Tool("long", "x", {}, [], lambda: {}, describe=lambda a: "x\ny" * 80))
        self.tools.register(Tool("broken", "x", {}, [], lambda: {}, describe=lambda a: a["missing"]))
        self.tools.register(Tool("secret", "x", {}, [], lambda: {}, describe=lambda a: "use fixture-protected-value"))
        self.tools.register(Tool("number", "x", {}, [], lambda: {}, describe=lambda a: 42))
        text = self.tools.describe("long", {"glob": "*.py"})
        self.assertLessEqual(len(text), 100)
        self.assertNotIn("\n", text)
        self.assertTrue(text.startswith("x yx y") and text.endswith("…"))
        self.assertEqual(self.tools.describe("broken", {}), "")
        self.assertEqual(self.tools.describe("secret", {}), "use [REDACTED]")
        self.assertEqual(self.tools.describe("number", {}), "42")
        self.assertEqual(self.tools.describe("long", None), "")
        # Built-in summaries are unchanged.
        self.assertEqual(self.tools.describe("read_file", {"path": "a.py", "start_line": 2}), "a.py:2-")
        self.assertEqual(self.tools.describe("search_files", {"query": "q", "path": "src", "glob": "*.py"}), '"q" in src (*.py)')


class WriteSeamTests(SeamTestCase):
    def setUp(self):
        super().setUp()
        self.target = self.root / "app.py"
        self.target.write_bytes(b"x = 1\r\n")
        # Exercise the guard list itself, without the built-in syntax guard (tests/test_syntax_guard.py).
        self.tools.write_guards.clear()

    def digest(self, path):
        return self.tools.read_file(path)["sha256"]

    def test_raising_guard_refuses_before_approval(self):
        def guard(path, old, new):
            raise HarnessError("guard says no")
        self.tools.write_guards.append(guard)
        with self.assertRaisesRegex(HarnessError, "guard says no"):
            self.tools.call("edit_file", {"path": "app.py", "old_string": "1", "new_string": "2"})
        with self.assertRaisesRegex(HarnessError, "guard says no"):
            self.tools.call("write_file", {"path": "app.py", "content": "y", "expected_sha256": self.digest("app.py")})
        with self.assertRaisesRegex(HarnessError, "guard says no"):
            self.tools.call("write_file", {"path": "new.py", "content": "y", "expected_sha256": "new"})
        self.assertEqual(self.asked, [])
        self.assertEqual(self.target.read_bytes(), b"x = 1\r\n")
        self.assertFalse((self.root / "new.py").exists())

    def test_guard_results_are_collected_in_order(self):
        seen = []
        self.tools.write_guards += [lambda path, old, new: seen.append((path, old, new)) or {"type": "probe"},
                                    lambda path, old, new: None, lambda path, old, new: {"type": "second"}]
        result = self.tools.call("edit_file", {"path": "app.py", "old_string": "1", "new_string": "2"})
        self.assertEqual(result["checks"], [{"type": "probe"}, {"type": "second"}])
        result = self.tools.call("write_file", {"path": "new.py", "content": "z", "expected_sha256": "new"})
        self.assertEqual(result["checks"], [{"type": "probe"}, {"type": "second"}])
        self.assertEqual(seen, [("app.py", "x = 1\r\n", "x = 2\r\n"), ("new.py", None, "z")])

    def test_no_guards_means_no_checks_key(self):
        result = self.tools.call("edit_file", {"path": "app.py", "old_string": "1", "new_string": "2"})
        self.assertNotIn("checks", result)
        result = self.tools.call("write_file", {"path": "app.py", "content": "w", "expected_sha256": result["sha256"]})
        self.assertNotIn("checks", result)
        self.tools.write_guards.append(lambda path, old, new: None)
        result = self.tools.call("write_file", {"path": "b.py", "content": "w", "expected_sha256": "new"})
        self.assertNotIn("checks", result)

    def test_review_paths_force_approval_under_approve_writes(self):
        self.policy.approve_writes = True
        self.tools.review_paths = lambda path: path.endswith(".yml")
        (self.root / "ci.yml").write_text("on: push\n")
        self.answer = False
        with self.assertRaisesRegex(HarnessError, "denied"):
            self.tools.call("edit_file", {"path": "ci.yml", "old_string": "push", "new_string": "pull_request"})
        self.assertEqual(len(self.asked), 1)
        self.assertEqual((self.root / "ci.yml").read_text(), "on: push\n")
        self.answer = True
        self.tools.call("edit_file", {"path": "ci.yml", "old_string": "push", "new_string": "pull_request"})
        self.assertEqual((self.root / "ci.yml").read_text(), "on: pull_request\n")
        self.assertEqual(len(self.asked), 2)
        self.tools.call("write_file", {"path": "deploy.yml", "content": "x\n", "expected_sha256": "new"})
        self.assertEqual(len(self.asked), 3)
        self.tools.call("edit_file", {"path": "app.py", "old_string": "1", "new_string": "2"})
        self.assertEqual(len(self.asked), 3)
        self.assertEqual(self.target.read_bytes(), b"x = 2\r\n")

    def test_read_only_wins_over_review_paths(self):
        self.policy.read_only = True
        self.tools.review_paths = lambda path: True
        with self.assertRaisesRegex(HarnessError, "read-only"):
            self.tools.call("edit_file", {"path": "app.py", "old_string": "1", "new_string": "2"})
        self.assertEqual(self.asked, [])
        self.assertEqual(self.target.read_bytes(), b"x = 1\r\n")

    def test_always_ask_never_removes_a_prompt(self):
        self.answer = False
        for always_ask in (False, True):
            with self.assertRaises(HarnessError):
                self.policy.require("write_file", "d", workspace_write=True, always_ask=always_ask)
        self.policy.approve_writes = True
        self.policy.require("write_file", "d", workspace_write=True)
        with self.assertRaises(HarnessError):
            self.policy.require("fetch_url", "d", always_ask=False)
        self.assertEqual(len(self.asked), 3)


class HookAndEventTests(SeamTestCase):
    def test_after_call_hooks_run_in_order(self):
        order = []
        self.tools.after_call.append(lambda name, arguments, result: order.append(("a", name)) or {**result, "a": 1})
        self.tools.after_call.append(lambda name, arguments, result: order.append(("b", result["a"])) or {**result, "b": 2})
        result = self.tools.call("set_plan", {"plan": "1. test"})
        self.assertEqual(result, {"plan": "1. test", "a": 1, "b": 2})
        self.assertEqual(order, [("a", "set_plan"), ("b", 1)])

    def test_notify_is_inert_without_an_agent(self):
        self.tools.notify("probe_event", n=1)
        self.assertEqual(self.store.events(self.session), [])

    def test_notify_inside_agent_is_journaled_and_emitted(self):
        emitted = []

        def probe():
            self.tools.notify("probe_event", n=2, secret="fixture-protected-value")
            return {"ok": True}
        self.tools.register(Tool("probe", "Probe.", {}, [], probe))
        provider = SequenceProvider([tool_message("probe", {}), {"role": "assistant", "content": "done"}])
        agent = Agent(provider, self.store, self.tools, emitted.append)
        self.tools.notify("probe_event", n=1)
        self.assertEqual(agent.run("go")["status"], "completed")
        events = [e for e in self.store.events(self.session) if e["kind"] == "probe_event"]
        self.assertEqual([e["payload"] for e in events], [{"n": 1}, {"n": 2, "secret": "[REDACTED]"}])
        probes = [e for e in emitted if e["event"] == "probe_event"]
        self.assertEqual(probes, [{"event": "probe_event", "session": self.session, "n": 1},
                                  {"event": "probe_event", "session": self.session, "n": 2, "secret": "[REDACTED]"}])
        kinds = [e["event"] for e in emitted]
        self.assertLess(kinds.index("tool_started"), kinds.index("probe_event", 1))
        self.assertLess(kinds.index("probe_event", 1), kinds.index("tool_completed"))


class LimitsAndCliTests(SeamTestCase):
    def test_limits_accept_bool_and_str_fields(self):
        @dataclass
        class Extended(Limits):
            checkpoints: bool = False
            instructions: str = "all"
        Agent(SequenceProvider([]), self.store, self.tools, limits=Extended())
        for bad in ({"max_steps": 0}, {"max_context_chars": -1}, {"max_total_tokens": 0.0}):
            with self.subTest(bad=bad), self.assertRaisesRegex(HarnessError, "positive"):
                Agent(SequenceProvider([]), self.store, self.tools, limits=Limits(**bad))
        with self.assertRaisesRegex(HarnessError, "positive"):
            Agent(SequenceProvider([]), self.store, self.tools, limits=Extended(max_steps=0))

    def test_policy_and_limits_builders(self):
        args = build_parser().parse_args(["run", "task", "--approve-writes", "--allow-host", "Example.COM",
                                          "--shell", "docker", "--docker-image", "img:1", "--allow-data-source",
                                          "coinbase", "--max-steps", "7", "--max-tokens", "900", "--no-compact"])
        policy = policy_from(args)
        self.assertEqual((policy.approve_writes, policy.read_only, policy.allowed_hosts, policy.shell_mode,
                          policy.docker_image, policy.allowed_data_sources),
                         (True, False, {"example.com"}, "docker", "img:1", {"coinbase"}))
        self.assertFalse(policy.approve("shell", "x"))  # the CLI approver refuses non-interactive input
        self.assertEqual(limits_from(args), Limits(max_steps=7, max_tool_calls=50, max_context_chars=120_000,
                                                   max_total_tokens=900, compact=False))


class TextTests(unittest.TestCase):
    def test_split_lines(self):
        self.assertEqual(split_lines("a\r\nb\rc\nd"), [("a", "\r\n"), ("b", "\r"), ("c", "\n"), ("d", "")])
        self.assertEqual(split_lines("x\x0cy\n"), [("x\x0cy", "\n")])
        self.assertEqual(split_lines(""), [])
        self.assertEqual(split_lines("\n\n"), [("", "\n"), ("", "\n")])
        self.assertEqual(split_lines("\r\r\n"), [("", "\r"), ("", "\r\n")])
        odd = "a\x0bb\x0cc\x1cd\x1de\x1ef\x85g h i"
        self.assertEqual(split_lines(odd + "\n"), [(odd, "\n")])
        self.assertEqual(lines("a\r\nb\rc\nd"), ["a", "b", "c", "d"])

    def test_random_round_trips(self):
        rng = random.Random(7)
        alphabet = "ab \t\r\n\x0b\x0c\x1c\x1d\x1e\x85  é"
        for _ in range(200):
            text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
            pairs = split_lines(text)
            self.assertEqual("".join(a + b for a, b in pairs), text)
            self.assertTrue(all(not any(c in a for c in "\r\n") for a, _ in pairs))
            self.assertTrue(all(b in ("\r\n", "\r", "\n") for _, b in pairs[:-1]))
            self.assertEqual(lines(text), [a for a, _ in pairs])


@unittest.skipUnless(os.name == "posix" and Path("/bin/sh").exists(), "the fake Docker shim needs /bin/sh")
class FakeDockerShellTests(SeamTestCase):
    def setUp(self):
        super().setUp()
        self.policy.shell_mode = "docker"
        self.log = self.root.parent / f"{self.root.name}-docker.jsonl"
        self.addCleanup(lambda: self.log.unlink(missing_ok=True))

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_shell_result_matches_previous_release(self):
        with fake_docker(self.log):
            result = self.tools.call("shell", {"command": "printf hi && printf made > out.txt"})
        self.assertEqual(result, {"exit_code": 0, "output": "hi", "truncated": False, "stopped": None})
        self.assertEqual((self.root / "out.txt").read_text(), "made")
        self.assertEqual(self.asked, [("shell", f"Mode: docker\nDirectory: {self.root.resolve()}\nTimeout: 30s\n"
                                                "Command:\nprintf hi && printf made > out.txt")])
        run, cleanup = self.calls()
        self.assertEqual(run[0], "run")
        self.assertIn("--network=none", run)
        self.assertEqual(run[-4:], ["/bin/sh", "python:3.11-slim", "-c", "printf hi && printf made > out.txt"])
        self.assertEqual(cleanup, ["rm", "-f", run[run.index("--name") + 1]])

    def test_exit_codes_stderr_and_limits(self):
        with fake_docker(self.log):
            result = self.tools.shell("echo oops >&2; exit 3")
            self.assertEqual((result["exit_code"], result["output"]), (3, "oops\n"))
            result = self.tools.shell("yes x | head -c 1200000")
            self.assertEqual((result["stopped"], result["truncated"], len(result["output"])), ("output_limit", True, 20_000))
            result = self.tools.shell("sleep 5", timeout=1)
            self.assertEqual(result["stopped"], "timeout")
        self.assertEqual([call[0] for call in self.calls()], ["run", "rm"] * 3)

    def test_denied_or_read_only_shell_never_invokes_docker(self):
        self.answer = False
        with fake_docker(self.log):
            with self.assertRaisesRegex(HarnessError, "denied"):
                self.tools.shell("echo no > denied.txt")
            self.policy.read_only = True
            with self.assertRaisesRegex(HarnessError, "read-only"):
                self.tools.shell("echo no > denied.txt")
            self.policy.read_only, self.policy.shell_mode = False, "disabled"
            with self.assertRaisesRegex(HarnessError, "--shell docker"):
                self.tools.shell("echo no")
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.root / "denied.txt").exists())

    def test_shim_commands_and_environment(self):
        before = (os.environ.get("PATH"), os.environ.get("EIRA_FAKE_DOCKER_LOG"))
        with fake_docker(self.log) as shim:
            self.assertTrue(os.access(shim, os.X_OK))
            self.assertTrue(os.environ["PATH"].startswith(str(shim.parent)))
            self.assertEqual(shutil.which("docker"), str(shim))
            codes = [subprocess.run(["docker", *args], capture_output=True).returncode
                     for args in (["image", "inspect", "img"], ["rm", "-f", "eira-x"], ["ps"], ["run", "img"])]
            self.assertEqual(codes, [0, 0, 125, 125])
        self.assertEqual(self.calls(), [["image", "inspect", "img"], ["rm", "-f", "eira-x"], ["ps"], ["run", "img"]])
        self.assertEqual((os.environ.get("PATH"), os.environ.get("EIRA_FAKE_DOCKER_LOG")), before)
        with fake_docker() as shim:
            self.assertEqual(shim, Path(__file__).resolve().parent / "fakes" / "docker")
            self.assertEqual(self.tools.shell("printf ok")["output"], "ok")
        self.assertEqual((os.environ.get("PATH"), os.environ.get("EIRA_FAKE_DOCKER_LOG")), before)


class CaptureTests(unittest.TestCase):
    def test_run_capture_bounds_data(self):
        self.assertEqual(SHELL_CAPTURE_BYTES, 1_000_000)
        with tempfile.TemporaryDirectory() as temp:
            result = _run_capture([sys.executable, "-c", "print('y' * 5000)"], temp, {}, 5, capture_limit=1000)
            self.assertEqual((len(result["data"]), result["stopped"]), (1000, "output_limit"))
            self.assertGreater(result["total"], 1000)
            result = _run_capture([sys.executable, "-c", "import sys; sys.stdout.write('ok'); sys.exit(4)"], temp, {}, 5)
            self.assertEqual(result, {"exit_code": 4, "data": b"ok", "total": 2, "stopped": None})


if __name__ == "__main__":
    unittest.main()
