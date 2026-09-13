import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from eira_harness.agent import Agent, Limits
from eira_harness.demo import DemoProvider, sample_csv
from eira_harness.network import fetch_public, validate_url
from eira_harness.security import HarnessError, Redactor, Workspace, clean_terminal, is_public_ip
from eira_harness.store import Store
from eira_harness.tools import Policy, Tool, Toolbox


def tool_message(name, args, call_id="call_1"):
    return {"role": "assistant", "content": None, "tool_calls": [{"id": call_id,
            "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


class SequenceProvider:
    model = "test-fixture"

    def __init__(self, messages):
        self.responses = iter(messages)
        self.seen = []

    def complete(self, messages, tools):
        self.seen.append(messages)
        return next(self.responses), {"total_tokens": 10}


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = Workspace(self.root)
        self.store = Store(self.root)
        self.session = self.store.create("test")
        self.tools = Toolbox(self.workspace, self.store, Policy(), self.session)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_paths_cannot_escape_or_read_secrets(self):
        for name in ["../secret", "/etc/passwd", ".env", ".env.local", ".eira/state.db",
                     ".git/config", ".ssh/id_rsa", "key.pem"]:
            with self.subTest(name=name), self.assertRaises(HarnessError):
                self.workspace.path(name)

    def test_symlinks_and_hardlinks_blocked(self):
        (self.root / "link").symlink_to("/etc/passwd")
        with self.assertRaises(HarnessError):
            self.workspace.read("link")
        (self.root / "a").write_text("text")
        os.link(self.root / "a", self.root / "b")
        with self.assertRaises(HarnessError):
            self.workspace.read("b")

    def test_default_policy_denies_writes(self):
        with self.assertRaises(HarnessError):
            self.tools.call("write_file", {"path": "x.txt", "content": "hello", "expected_sha256": "new"})
        self.assertFalse((self.root / "x.txt").exists())

    def test_reviewed_write_and_stale_content_guard(self):
        approvals = []
        self.tools.policy.approve = lambda name, detail: approvals.append(detail) or True
        self.tools.write_file("x.txt", "hello\n", "new")
        self.assertIn("+hello", approvals[0])
        read = self.tools.read_file("x.txt")
        self.tools.write_file("x.txt", "world\n", read["sha256"])
        with self.assertRaises(HarnessError):
            self.tools.write_file("x.txt", "stale", read["sha256"])
        self.assertEqual((self.root / "x.txt").read_text(), "world\n")

    def test_file_changes_during_approval_cancel_edit(self):
        (self.root / "x.txt").write_text("before")
        def change(name, detail):
            (self.root / "x.txt").write_text("concurrent edit")
            return True
        self.tools.policy.approve = change
        with self.assertRaises(HarnessError):
            self.tools.write_file("x.txt", "after", hashlib.sha256(b"before").hexdigest())
        self.assertEqual((self.root / "x.txt").read_text(), "concurrent edit")

    def test_read_only_overrides_autoapproval(self):
        self.tools.policy = Policy(approve=lambda *x: True, approve_writes=True, read_only=True, shell_mode="host")
        for name, args in [("write_file", {"path": "x", "content": "x", "expected_sha256": "new"}),
                           ("remember", {"key": "key", "value": "value"}),
                           ("shell", {"command": "true"})]:
            with self.subTest(name=name), self.assertRaises(HarnessError):
                self.tools.call(name, args)

    def test_schema_errors_do_not_execute_tools(self):
        for name, args in [("unknown", {}), ("read_file", {}), ("read_file", {"path": 3}),
                           ("read_file", {"path": "x", "extra": True}),
                           ("shell", {"command": "true", "timeout": True})]:
            with self.subTest(name=name, args=args), self.assertRaises(HarnessError):
                self.tools.call(name, args)

    def test_shell_cannot_be_autoapproved_by_write_flag(self):
        self.tools.policy = Policy(approve_writes=True, shell_mode="host")
        with self.assertRaises(HarnessError):
            self.tools.shell("touch should-not-exist")
        self.assertFalse((self.root / "should-not-exist").exists())

    def test_host_shell_timeout_and_output_capture(self):
        self.tools.policy = Policy(approve=lambda *x: True, shell_mode="host")
        output = self.tools.shell("printf hello")
        self.assertEqual(output["output"], "hello")
        self.assertEqual(output["exit_code"], 0)
        result = self.tools.shell("sleep 5", timeout=1)
        self.assertEqual(result["stopped"], "timeout")
        self.assertNotEqual(result["exit_code"], 0)

    def test_search_reports_lines(self):
        (self.root / "code.py").write_text("one\nneedle\nthree\n")
        result = self.tools.search_files("needle")
        self.assertEqual(result["matches"][0]["line"], 2)
        self.assertNotIn(".eira/state.db", self.tools.list_files()["files"])

    def test_secret_and_terminal_hygiene(self):
        with patch.dict(os.environ, {"EIRA_API_KEY": "secret-test-value"}):
            store = Store(self.root, Redactor())
            store.append(self.session, {"role": "user", "content": "secret-test-value"})
            self.assertNotIn("secret-test-value", str(store.messages(self.session)))
            store.close()
        self.assertEqual(clean_terminal("\x1b[31mhello\x1b[0m\u202e"), "hello")

    def test_memory_survives_store_reopen(self):
        self.store.remember("project", "research")
        other = Store(self.root)
        self.assertEqual(other.memories()["project"], "research")
        other.close()
        self.store.forget("project")
        self.assertEqual(self.store.memories(), {})

    def test_offline_demo_exercises_agent_and_journal(self):
        (self.root / "synthetic.csv").write_text(sample_csv())
        result = Agent(DemoProvider(), self.store, self.tools).run("Run the synthetic demo")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["tools"], 3)
        events = self.store.events(self.session)
        self.assertEqual(events[-1]["kind"], "run_completed")
        self.assertEqual(sum(e["kind"] == "tool_completed" for e in events), 3)

    def test_tool_error_is_returned_to_model(self):
        provider = SequenceProvider([tool_message("read_file", {"path": "missing"}),
                                     {"role": "assistant", "content": "File missing."}])
        Agent(provider, self.store, self.tools).run("Read missing")
        result = json.loads(provider.seen[-1][-1]["content"])
        self.assertFalse(result["ok"])

    def test_crash_recovery_does_not_replay_write(self):
        self.store.append(self.session, tool_message("write_file", {
            "path": "should-not-exist", "content": "oops", "expected_sha256": "new"}))
        provider = SequenceProvider([{"role": "assistant", "content": "Recovered."}])
        self.tools.policy.approve_writes = True
        Agent(provider, self.store, self.tools).run("Resume")
        self.assertFalse((self.root / "should-not-exist").exists())
        self.assertIn("outcome unknown", provider.seen[0][2]["content"])

    def test_tool_budget_closes_all_pending_calls(self):
        message = tool_message("list_files", {})
        message["tool_calls"] += tool_message("write_file", {
            "path": "unwanted", "content": "oops", "expected_sha256": "new"}, "call_2")["tool_calls"]
        self.tools.policy.approve_writes = True
        result = Agent(SequenceProvider([message]), self.store, self.tools,
                       limits=Limits(max_tool_calls=1)).run("Test budget")
        self.assertEqual(result["status"], "stopped")
        self.assertFalse((self.root / "unwanted").exists())
        self.assertEqual(sum(m["role"] == "tool" for m in self.store.messages(self.session)), 2)

    def test_context_limit_preserves_history(self):
        with self.assertRaises(HarnessError):
            Agent(SequenceProvider([]), self.store, self.tools,
                  limits=Limits(max_context_chars=10)).run("Keep this prompt")
        self.assertEqual(self.store.messages(self.session)[-1]["content"], "Keep this prompt")

    def test_lock_prevents_concurrent_session_use(self):
        with self.store.lock(self.session):
            with self.assertRaises(HarnessError):
                with self.store.lock(self.session):
                    self.fail("Concurrent lock was allowed")

    def test_token_budget_stops_before_tool_side_effect(self):
        provider = SequenceProvider([tool_message("write_file", {
            "path": "x", "content": "x", "expected_sha256": "new"})])
        self.tools.policy.approve_writes = True
        result = Agent(provider, self.store, self.tools, limits=Limits(max_total_tokens=5)).run("Test")
        self.assertEqual(result["status"], "stopped")
        self.assertFalse((self.root / "x").exists())


class NetworkBoundaryTests(unittest.TestCase):
    def test_private_ip_classification(self):
        for address in ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fc00::1", "224.0.0.1"]:
            self.assertFalse(is_public_ip(address), address)
        self.assertTrue(is_public_ip("1.1.1.1"))

    def test_url_credentials_and_unsafe_protocols_blocked(self):
        for url in ["http://example.com", "file:///etc/passwd", "https://u:p@example.com", "https://example.com:444"]:
            with self.subTest(url=url), self.assertRaises(HarnessError):
                validate_url(url)

    def test_dns_private_address_blocked_before_connect(self):
        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]), \
                patch("socket.create_connection") as connect:
            with self.assertRaises(HarnessError):
                fetch_public("https://example.com")
            connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
