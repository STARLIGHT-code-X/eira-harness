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
from eira_harness.provider import Provider
from eira_harness.security import HarnessError


TOOLS = [{"type": "function", "function": {"name": "read_file", "description": "Read",
          "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}]


def anthropic_response(content, stop="tool_use", usage=None):
    return {"type": "message", "role": "assistant", "stop_reason": stop, "content": content,
            "usage": usage or {"input_tokens": 3, "output_tokens": 4}}


class Capture:
    def __init__(self, *responses, status=200):
        self.responses, self.status, self.bodies = list(responses), status, []

    def __call__(self, url, **kwargs):
        self.bodies.append(json.loads(kwargs["body"]))
        return self.status, {}, json.dumps(self.responses.pop(0)).encode()


def provider(**kwargs):
    return Provider("claude-test", "https://api.example/v1", "key", profile="anthropic", **kwargs)


THINKING = [
    {"type": "thinking", "thinking": "", "signature": "sig-one"},
    {"type": "text", "text": "Reading the file."},
    {"type": "redacted_thinking", "data": "opaque"},
    {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "a.txt"}},
]


class AnthropicProtocolTests(unittest.TestCase):
    def test_thinking_blocks_are_accepted_and_kept_in_order(self):
        with patch("eira_harness.provider.request_bytes", Capture(anthropic_response(THINKING))):
            message, _ = provider().complete([{"role": "user", "content": "Read a.txt"}], TOOLS)
        self.assertEqual(message["content"], "Reading the file.")
        self.assertEqual(message["tool_calls"][0]["id"], "toolu_1")
        self.assertEqual(message["anthropic_content"], THINKING)

    def test_thinking_blocks_are_replayed_verbatim(self):
        capture = Capture(anthropic_response(THINKING), anthropic_response([{"type": "text", "text": "Done."}], "end_turn"))
        history = [{"role": "system", "content": "Policy"}, {"role": "user", "content": "Read a.txt"}]
        with patch("eira_harness.provider.request_bytes", capture):
            message, _ = provider().complete(history, TOOLS)
            history += [message, {"role": "tool", "tool_call_id": "toolu_1", "content": "{\"ok\": true}"}]
            provider().complete(history, TOOLS)
        replayed = capture.bodies[1]["messages"][1]
        self.assertEqual(replayed, {"role": "assistant", "content": THINKING})
        self.assertNotIn("cache_control", json.dumps(replayed))

    def test_inconsistent_stored_blocks_fall_back_to_rebuilt_turn(self):
        stale = {"role": "assistant", "content": "", "anthropic_content": THINKING, "tool_calls": [{
            "id": "other", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]}
        capture = Capture(anthropic_response([{"type": "text", "text": "ok"}], "end_turn"))
        with patch("eira_harness.provider.request_bytes", capture):
            provider().complete([{"role": "user", "content": "x"}, stale,
                                 {"role": "tool", "tool_call_id": "other", "content": "{}"}], TOOLS)
        rebuilt = capture.bodies[0]["messages"][1]["content"]
        self.assertEqual([block["type"] for block in rebuilt], ["tool_use"])

    def test_prompt_cache_marks_system_and_newest_turn_only(self):
        capture = Capture(anthropic_response([{"type": "text", "text": "ok"}], "end_turn"))
        with patch("eira_harness.provider.request_bytes", capture):
            provider().complete([{"role": "system", "content": "Policy"}, {"role": "user", "content": "one"},
                                 {"role": "assistant", "content": "two"}, {"role": "user", "content": "three"}], TOOLS)
        body = capture.bodies[0]
        self.assertEqual(body["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(json.dumps(body).count("cache_control"), 2)
        self.assertEqual(body["messages"][-1]["content"][0]["text"], "three")

    def test_prompt_cache_can_be_disabled(self):
        capture = Capture(anthropic_response([{"type": "text", "text": "ok"}], "end_turn"))
        with patch("eira_harness.provider.request_bytes", capture):
            provider(prompt_cache=False).complete([{"role": "system", "content": "Policy"},
                                                   {"role": "user", "content": "one"}], TOOLS)
        self.assertEqual(capture.bodies[0]["system"], "Policy")
        self.assertNotIn("cache_control", json.dumps(capture.bodies[0]))

    def test_cached_tokens_count_toward_total_usage(self):
        usage = {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 100,
                 "cache_read_input_tokens": 1000}
        with patch("eira_harness.provider.request_bytes", Capture(anthropic_response([{"type": "text", "text": "ok"}], "end_turn", usage))):
            _, normalized = provider().complete([{"role": "user", "content": "x"}], TOOLS)
        self.assertEqual(normalized["total_tokens"], 1115)
        self.assertEqual(normalized["cache_read_input_tokens"], 1000)

    def test_unknown_blocks_and_truncation_fail_closed(self):
        for content, stop in [([{"type": "mystery"}], "end_turn"),
                              ([{"type": "thinking", "thinking": "x"}], "end_turn"),
                              ([{"type": "text", "text": "partial"}], "max_tokens")]:
            with self.subTest(content=content, stop=stop), \
                    patch("eira_harness.provider.request_bytes", Capture(anthropic_response(content, stop))):
                with self.assertRaises(HarnessError):
                    provider().complete([{"role": "user", "content": "x"}], TOOLS)

    def test_openai_transport_never_sends_provider_metadata(self):
        capture = Capture({"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}]})
        history = [{"role": "user", "content": "x"},
                   {"role": "assistant", "content": "y", "anthropic_content": THINKING, "refusal": None},
                   {"role": "user", "content": "z", "eira_compaction": {"messages": 4}}]
        with patch("eira_harness.provider.request_bytes", capture):
            Provider("model", "https://model.example/v1", "key").complete(history, TOOLS)
        sent = capture.bodies[0]["messages"]
        self.assertEqual(sent, [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"},
                                {"role": "user", "content": "z"}])

    def test_provider_error_names_status_and_type_but_not_body(self):
        for body, shown in [({"error": {"type": "not_found_error", "message": "model private-detail"}}, "not_found_error"),
                            ({"error": {"type": "Bad Type: private-detail", "message": "x"}}, None),
                            ({"error": "private-detail"}, None)]:
            with self.subTest(body=body), patch("eira_harness.provider.request_bytes", Capture(body, status=404)):
                with self.assertRaises(HarnessError) as raised:
                    provider().complete([{"role": "user", "content": "x"}], TOOLS)
                self.assertIn("HTTP 404", str(raised.exception))
                self.assertNotIn("private-detail", str(raised.exception))
                if shown:
                    self.assertIn(shown, str(raised.exception))

    def test_output_and_timeout_bounds_are_validated(self):
        for kwargs in [{"max_output_tokens": 10}, {"max_output_tokens": 10**6}, {"timeout": 0}, {"timeout": 10_000}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(HarnessError):
                provider(**kwargs)
        self.assertEqual(provider(max_output_tokens=4096).max_output_tokens, 4096)


def without_cache_markers(value):
    if isinstance(value, dict):
        return {k: without_cache_markers(v) for k, v in value.items() if k != "cache_control"}
    if isinstance(value, list):
        return [without_cache_markers(v) for v in value]
    return value


def plain(content):
    """Collapse a single cache-marked text block back to the string form."""
    if isinstance(content, list) and len(content) == 1 and content[0].get("type") == "text":
        return content[0]["text"]
    return content


class AnthropicEndToEndTests(unittest.TestCase):
    """CLI -> agent -> real loopback HTTP -> Anthropic wire format, across two processes' worth of runs."""

    def setUp(self):
        self.requests, self.responses = [], []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                outer.requests.append({"path": self.path, "headers": dict(self.headers),
                                       "body": json.loads(self.rfile.read(int(self.headers["Content-Length"])))})
                raw = json.dumps(outer.responses.pop(0)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"HOME": str(self.root), "XDG_CONFIG_HOME": str(self.root / "config")}, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def run_cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = main(["run", *args, "--workspace", str(self.root), "--provider", "anthropic", "--model", "claude-test",
                         "--base-url", f"http://127.0.0.1:{self.server.server_port}/v1", "--approve-writes", "--json"])
        return code, [json.loads(line) for line in out.getvalue().splitlines()]

    def test_thinking_turns_survive_tool_loop_and_resume_with_stable_prefix(self):
        (self.root / "a.txt").write_text("alpha\n")
        self.responses += [
            anthropic_response([{"type": "thinking", "thinking": "", "signature": "sig-a"},
                                {"type": "tool_use", "id": "toolu_a", "name": "edit_file",
                                 "input": {"path": "a.txt", "old_string": "alpha", "new_string": "beta"}}],
                               usage={"input_tokens": 50, "output_tokens": 20, "cache_creation_input_tokens": 900}),
            anthropic_response([{"type": "thinking", "thinking": "", "signature": "sig-b"},
                                {"type": "text", "text": "Changed alpha to beta."}], "end_turn",
                               usage={"input_tokens": 30, "output_tokens": 10, "cache_read_input_tokens": 900}),
            anthropic_response([{"type": "text", "text": "Done."}], "end_turn"),
        ]
        code, events = self.run_cli("Change alpha to beta in a.txt")
        self.assertEqual(code, 0)
        self.assertEqual((self.root / "a.txt").read_text(), "beta\n")
        session = events[0]["session"]
        self.assertEqual(events[-1]["tokens"], 1910)  # input + output + cache write/read
        code, _ = self.run_cli("Confirm you are finished.", "--session", session)
        self.assertEqual(code, 0)

        bodies = [request["body"] for request in self.requests]
        self.assertEqual({request["path"] for request in self.requests}, {"/v1/messages"})
        self.assertEqual(bodies[0]["max_tokens"], 16000)
        # Request 2 replays turn 1's thinking block verbatim ahead of its tool call.
        self.assertEqual(bodies[1]["messages"][1]["content"][0], {"type": "thinking", "thinking": "", "signature": "sig-a"})
        self.assertEqual(bodies[1]["messages"][2]["content"][0]["type"], "tool_result")
        # Each request is the previous one plus new turns: same system, tools, and message prefix.
        for before, after in zip(bodies, bodies[1:]):
            self.assertEqual(after["system"], before["system"])
            self.assertEqual(after["tools"], before["tools"])
            old, new = without_cache_markers(before["messages"]), without_cache_markers(after["messages"])
            old[-1]["content"] = plain(old[-1]["content"])
            new_prefix = [dict(m, content=plain(m["content"])) for m in new[:len(old)]]
            self.assertEqual(new_prefix, old)
        self.assertEqual(bodies[2]["messages"][3]["content"][0]["signature"], "sig-b")
        self.assertEqual(bodies[2]["messages"][-1]["content"][-1]["cache_control"], {"type": "ephemeral"})


if __name__ == "__main__":
    unittest.main()
