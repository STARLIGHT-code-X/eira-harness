from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from eira_harness.cli import main
from eira_harness.provider import Provider
from eira_harness.security import HarnessError


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.responses = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                outer.requests.append({"path": self.path,
                    "body": json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                    "authorization": self.headers.get("Authorization")})
                status, body = outer.responses.pop(0)
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def good(self, content="done"):
        return {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
                "usage": {"total_tokens": 12}}

    def test_real_http_adapter_payload_and_response(self):
        self.responses.append((200, self.good()))
        response, usage = Provider("test-model", self.url, "test-credential").complete(
            [{"role": "user", "content": "Hello"}], [])
        self.assertEqual(response["content"], "done")
        self.assertEqual(usage["total_tokens"], 12)
        self.assertEqual(self.requests[0]["path"], "/v1/chat/completions")
        self.assertEqual(self.requests[0]["authorization"], "Bearer test-credential")
        self.assertEqual(self.requests[0]["body"]["model"], "test-model")

    def test_tool_call_wire_format(self):
        body = self.good()
        body["choices"][0]["message"] = {"role": "assistant", "content": None,
            "tool_calls": [{"id": "id1", "type": "function", "function": {
                "name": "read_file", "arguments": '{"path":"x"}'}}]}
        self.responses.append((200, body))
        response, _ = Provider("test-model", self.url).complete([], [])
        self.assertEqual(response["tool_calls"][0]["function"]["name"], "read_file")

    def test_cli_to_http_to_tool_to_final_answer(self):
        first = self.good()
        first["choices"][0]["message"] = {"role": "assistant", "content": None,
            "tool_calls": [{"id": "read1", "type": "function", "function": {
                "name": "read_file", "arguments": '{"path":"input.txt"}'}}]}
        self.responses.extend([(200, first), (200, self.good("Verified file contents."))])
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "input.txt").write_text("ground-truth contents")
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                code = main(["run", "Read input.txt", "--workspace", root,
                             "--provider", "custom", "--model", "test-model", "--base-url", self.url, "--read-only", "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(len(self.requests), 2)
            tool_result = self.requests[1]["body"]["messages"][-1]
            self.assertEqual(tool_result["role"], "tool")
            self.assertEqual(tool_result["tool_call_id"], "read1")
            self.assertEqual(json.loads(tool_result["content"])["result"]["content"], "ground-truth contents")
            self.assertEqual(json.loads(out.getvalue().splitlines()[-1])["event"], "run_completed")

    def test_bounded_rate_limit_retry(self):
        self.responses.extend([(429, {}), (200, self.good())])
        with patch("eira_harness.provider.time.sleep"):
            response, _ = Provider("test-model", self.url).complete([], [])
        self.assertEqual(response["content"], "done")
        self.assertEqual(len(self.requests), 2)

    def test_error_does_not_echo_sensitive_body(self):
        self.responses.append((401, {"error": "private-token"}))
        with self.assertRaises(HarnessError) as ctx:
            Provider("test-model", self.url).complete([], [])
        self.assertNotIn("private-token", str(ctx.exception))
        self.assertEqual(len(self.requests), 1)

    def test_malformed_or_truncated_responses_are_rejected(self):
        truncated = self.good()
        truncated["choices"][0]["finish_reason"] = "length"
        for body in [{}, {"choices": []}, truncated,
                     {"choices": [{"message": {"role": "assistant", "tool_calls": [{}]}}]}]:
            self.responses.append((200, body))
            with self.subTest(body=body), self.assertRaises(HarnessError):
                Provider("test-model", self.url).complete([], [])

    def test_insecure_remote_endpoint_is_rejected(self):
        for url in ["http://example.com/v1", "https://user:pass@example.com/v1", "https://example.com/v1?key=x"]:
            with self.subTest(url=url), self.assertRaises(HarnessError):
                Provider("test", url)


class CLITests(unittest.TestCase):
    def invoke(self, args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(args)
        return code, out.getvalue(), err.getvalue()

    def test_demo_trace_and_session_listing(self):
        with tempfile.TemporaryDirectory() as root:
            code, output, _ = self.invoke(["demo", "--workspace", root, "--json"])
            self.assertEqual(code, 0)
            events = [json.loads(line) for line in output.splitlines()]
            self.assertEqual(events[-1]["event"], "run_completed")
            session = events[0]["session"]
            code, output, _ = self.invoke(["trace", session, "--workspace", root])
            self.assertEqual(code, 0)
            trace = json.loads(output)
            self.assertTrue(trace["messages"])
            self.assertTrue(trace["system"].startswith("You are Eira"))
            code, output, _ = self.invoke(["sessions", "--workspace", root])
            self.assertEqual(json.loads(output)[0]["id"], session)

    def test_backtest_report_does_not_overwrite_existing_file(self):
        with tempfile.TemporaryDirectory() as root:
            self.invoke(["demo", "--workspace", root])
            args = ["backtest", "synthetic.csv", "--workspace", root, "--output", "report.md"]
            self.assertEqual(self.invoke(args)[0], 0)
            self.assertIn("Eira backtest", (Path(root) / "report.md").read_text())
            self.assertEqual(self.invoke(args)[0], 2)

    def test_init_preserves_project_guidance(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "EIRA.md").write_text("Existing guidance")
            self.assertEqual(self.invoke(["init", "--workspace", root])[0], 0)
            self.assertEqual((Path(root) / "EIRA.md").read_text(), "Existing guidance")

    def test_missing_model_has_actionable_error(self):
        with tempfile.TemporaryDirectory() as root, patch.dict("os.environ", {}, clear=True):
            code, _, error = self.invoke(["run", "hello", "--workspace", root])
            self.assertEqual(code, 2)
            self.assertIn("EIRA_MODEL", error)


if __name__ == "__main__":
    unittest.main()
