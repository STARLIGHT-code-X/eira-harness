import io
import os
import unittest
from unittest.mock import patch

from eira_harness.terminal import Terminal
from eira_harness.security import HarnessError


class TerminalTests(unittest.TestCase):
    def test_non_tty_is_plain_and_contains_context(self):
        output = io.StringIO()
        terminal = Terminal(output)
        terminal.banner("/tmp/project", "ollama", "local-model", "abc123", True)
        text = output.getvalue()
        self.assertIn("E I R A", text)
        self.assertIn("/tmp/project", text)
        self.assertIn("read-only", text)
        self.assertNotIn("\x1b[", text)
        panel = text.split("  /help", 1)[0]
        rows = panel.splitlines()
        self.assertTrue(rows)
        self.assertEqual(len({len(row) for row in rows}), 1)

    def test_no_color_disables_ansi_on_tty(self):
        class TTY(io.StringIO):
            def isatty(self):
                return True
        output = TTY()
        with patch.dict(os.environ, {"NO_COLOR": ""}):
            terminal = Terminal(output)
            terminal.notice("safe")
        self.assertEqual(output.getvalue(), "safe\n")

    def test_dynamic_output_is_sanitized_and_redacted(self):
        output = io.StringIO()
        with patch.dict(os.environ, {"EIRA_TEST_TOKEN": "super-secret-token"}, clear=False):
            terminal = Terminal(output)
            terminal.emit({"event": "assistant", "text": "hello\x1b[2J super-secret-token"})
        self.assertNotIn("\x1b[", output.getvalue())
        self.assertNotIn("super-secret-token", output.getvalue())
        self.assertIn("[REDACTED]", output.getvalue())

    def test_term_dumb_disables_ansi(self):
        class TTY(io.StringIO):
            def isatty(self):
                return True
        output = TTY()
        with patch.dict(os.environ, {"TERM": "dumb"}, clear=False):
            Terminal(output).notice("plain")
        self.assertNotIn("\x1b[", output.getvalue())

    def test_events_render_tool_and_failure_states(self):
        output = io.StringIO()
        terminal = Terminal(output)
        terminal.emit({"event": "model_started"})
        terminal.emit({"event": "model_completed"})
        terminal.emit({"event": "tool_started", "name": "read_file"})
        terminal.emit({"event": "tool_completed", "ok": False, "error": "denied"})
        text = output.getvalue()
        self.assertIn("thinking", text)
        self.assertIn("read_file", text)
        self.assertIn("denied", text)

    def test_slash_command_parser_and_eof(self):
        self.assertEqual(Terminal.parse_command("/resume abc"), ("resume", "abc"))
        self.assertEqual(Terminal.parse_command("plain text"), (None, "plain text"))
        with patch("sys.stdin", io.StringIO("")):
            self.assertEqual(Terminal(io.StringIO()).prompt(), "/exit")

    def test_prompt_rejects_hidden_terminal_controls(self):
        with patch("builtins.input", return_value="hello\x1b[2J"), self.assertRaisesRegex(HarnessError, "control"):
            Terminal(io.StringIO()).prompt()


if __name__ == "__main__":
    unittest.main()
