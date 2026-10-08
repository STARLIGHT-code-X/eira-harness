"""Cross-feature tests: the real CLI, a loopback model, and several 0.5 features together."""
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from eira_harness.cli import main
from tests.fakes import fake_docker


def tool_call(name, arguments, call_id):
    return {"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}}],
        "usage": {"total_tokens": 5}}


def answer(text):
    return {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
            "usage": {"total_tokens": 5}}


class LoopbackModel:
    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                outer.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                raw = json.dumps(outer.responses.pop(0)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def results(self):
        """Tool results the model received, decoded, in order."""
        last = self.requests[-1]["messages"]
        return [json.loads(m["content"]) for m in last if m["role"] == "tool"]


class FeatureIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"HOME": str(self.root / "home"), "XDG_CONFIG_HOME": str(self.root / "cfg")}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.temp.cleanup)
        self.ws = self.root / "ws"
        self.ws.mkdir()

    def cli(self, model, *args):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = main([*args, "--workspace", str(self.ws), "--provider", "custom", "--model", "m", "--base-url", model.url])
        return code, [json.loads(line) for line in out.getvalue().splitlines() if line.startswith("{")]

    def plain(self, *args):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = main([*args, "--workspace", str(self.ws)])
        return code, out.getvalue()

    def test_patch_guard_checkpoint_and_rewind_work_together(self):
        (self.ws / "app.py").write_text("def total(xs):\n    return sum(xs)\n")
        (self.ws / "util.py").write_text("VALUE = 1\n")
        good = ("*** Begin Patch\n*** Update File: app.py\n@@\n def total(xs):\n-    return sum(xs)\n+    return sum(xs) + 0\n"
                "*** Update File: util.py\n@@\n-VALUE = 1\n+VALUE = 2\n*** Add File: new.py\n+NEW = True\n*** End Patch\n")
        # A multi-file patch whose second file would stop parsing: the syntax guard refuses all of it.
        broken = ("*** Begin Patch\n*** Update File: app.py\n@@\n-    return sum(xs) + 0\n+    return sum(xs) + 1\n"
                  "*** Update File: util.py\n@@\n-VALUE = 2\n+VALUE = (\n*** End Patch\n")
        model = LoopbackModel(tool_call("apply_patch", {"input": good}, "p1"), tool_call("apply_patch", {"input": broken}, "p2"),
                              answer("Patched."))
        self.addCleanup(model.close)
        code, events = self.cli(model, "run", "Refactor", "--approve-writes", "--json")
        self.assertEqual(code, 0)
        first, second = model.results()
        self.assertTrue(first["ok"], first)
        self.assertFalse(second["ok"])
        self.assertIn("Syntax check failed for util.py", second["error"])
        self.assertEqual((self.ws / "app.py").read_text(), "def total(xs):\n    return sum(xs) + 0\n")
        self.assertEqual((self.ws / "util.py").read_text(), "VALUE = 2\n")
        self.assertTrue((self.ws / "new.py").exists())
        session = events[0]["session"]

        code, listing = self.plain("checkpoints", "--session", session, "--json")
        self.assertEqual(code, 0)
        checkpoints = json.loads(listing)
        self.assertTrue(checkpoints)
        target = checkpoints[0]["id"] if isinstance(checkpoints, list) else checkpoints["checkpoints"][0]["id"]
        code, diff = self.plain("diff", target, "--session", session)
        self.assertEqual(code, 0)
        self.assertIn("+VALUE = 2", diff)

        code, _ = self.plain("rewind", target, "--code", "--yes", "--session", session)
        self.assertEqual(code, 0)
        self.assertEqual((self.ws / "app.py").read_text(), "def total(xs):\n    return sum(xs)\n")
        self.assertEqual((self.ws / "util.py").read_text(), "VALUE = 1\n")
        self.assertFalse((self.ws / "new.py").exists())

    def test_sandboxed_shell_changes_are_rewindable_and_shell_routed_patches_need_write_approval(self):
        (self.ws / "data.txt").write_text("keep me\n")
        patch_command = "apply_patch <<'EOF'\n*** Begin Patch\n*** Add File: routed.txt\n+x\n*** End Patch\nEOF"
        model = LoopbackModel(tool_call("shell", {"command": "rm data.txt"}, "s1"),
                              tool_call("shell", {"command": patch_command}, "s2"), answer("Done."))
        self.addCleanup(model.close)
        with fake_docker(None):
            code, events = self.cli(model, "run", "Clean up", "--shell", "docker", "--shell-approval", "sandboxed", "--json")
        self.assertEqual(code, 0)
        removed, routed = model.results()
        self.assertTrue(removed["ok"], removed)
        self.assertFalse((self.ws / "data.txt").exists())
        # Shell-routed patches are file writes: without --approve-writes, and with no human, they are denied.
        self.assertFalse(routed["ok"])
        self.assertFalse((self.ws / "routed.txt").exists())
        session = events[0]["session"]
        code, listing = self.plain("checkpoints", "--session", session, "--json")
        checkpoints = json.loads(listing)
        target = checkpoints[0]["id"] if isinstance(checkpoints, list) else checkpoints["checkpoints"][0]["id"]
        self.assertEqual(self.plain("rewind", target, "--code", "--yes", "--session", session)[0], 0)
        self.assertEqual((self.ws / "data.txt").read_text(), "keep me\n")

    def test_subdirectory_guidance_arrives_with_apply_patch_results(self):
        (self.ws / "services" / "pay").mkdir(parents=True)
        (self.ws / "services" / "pay" / "AGENTS.md").write_text("Money is integer cents.\n")
        (self.ws / "services" / "pay" / "fee.py").write_text("FEE = 1\n")
        change = "*** Begin Patch\n*** Update File: services/pay/fee.py\n@@\n-FEE = 1\n+FEE = 2\n*** End Patch\n"
        model = LoopbackModel(tool_call("apply_patch", {"input": change}, "p1"), answer("ok"))
        self.addCleanup(model.close)
        code, _ = self.cli(model, "run", "Raise the fee", "--approve-writes", "--json")
        self.assertEqual(code, 0)
        (result,) = model.results()
        self.assertIn("Money is integer cents.", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
