from contextlib import redirect_stderr
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from eira_harness.cli import approve, main
from eira_harness.network import request_bytes
from eira_harness.security import HarnessError, Redactor, Workspace, approval_text, bounded_json_loads, clean_terminal
from eira_harness.store import Store
from eira_harness.tools import Policy, Toolbox, _run_bounded


class HardeningTests(unittest.TestCase):
    def test_approval_preserves_hidden_content_visibly(self):
        detail = 'line one\nline \x1b]review this\x07 two\u202e'
        rendered = approval_text(detail)
        self.assertEqual('\n'.join(json.loads(line) for line in rendered.splitlines()), detail)
        self.assertNotIn('\x1b', rendered)
        self.assertIn('review this', rendered)
        class TTY(io.StringIO):
            def isatty(self): return True
        out = io.StringIO()
        with patch('sys.stdin', TTY('y\n')), redirect_stderr(out):
            self.assertTrue(approve('fixture', detail))
        self.assertIn('review this', out.getvalue())

    def test_terminal_sanitizer_handles_large_incomplete_sequences(self):
        self.assertEqual(clean_terminal('hello' + '\x1b]' * 100000), 'hello')

    def test_json_depth_and_quoted_braces(self):
        with self.assertRaises(HarnessError): bounded_json_loads('[' * 200 + '0' + ']' * 200)
        self.assertEqual(bounded_json_loads(json.dumps({'text': '[' * 200})), {'text': '[' * 200})

    def test_protected_paths_and_redacted_edits(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root, Redactor(extra=('fixture-protected-value',)))
            try:
                box = Toolbox(Workspace(root), store, Policy(approve_writes=True), store.create('test'))
                for path in ['.config/gcloud/application_default_credentials.json', '.config/gh/hosts.yml', '.gitconfig']:
                    with self.assertRaises(HarnessError): box.call('read_file', {'path': path})
                path = root / 'settings.txt'
                path.write_text('fixture-protected-value\nmode=dev')
                result = box.read_file('settings.txt')
                self.assertFalse(result['editable'])
                self.assertIsNone(result['sha256'])
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                with self.assertRaises(HarnessError):
                    box.write_file('settings.txt', result['content'].replace('dev', 'prod'), digest)
                self.assertEqual(path.read_text(), 'fixture-protected-value\nmode=dev')
                with self.assertRaises(HarnessError):
                    box.call('backtest_sma', {'path': 'x', 'capital': 10**400})
                with patch.dict(os.environ, {'XDG_CONFIG_HOME': str(root/'config')}):
                    with self.assertRaises(HarnessError): box.workspace.path('config/app/token.txt')
            finally:
                store.close()

    def test_directory_depth_is_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            deep = root
            for _ in range(40):
                deep /= 'nested'
                deep.mkdir()
            (deep/'file.txt').write_text('hidden by depth limit')
            store = Store(root)
            try:
                box = Toolbox(Workspace(root), store, Policy(), store.create('test'))
                result = box.list_files()
                self.assertTrue(result['truncated'])
                self.assertEqual(result['files'], [])
            finally:
                store.close()

    def test_capture_is_bounded_and_timeout_stops_process(self):
        with tempfile.TemporaryDirectory() as temp:
            result = _run_bounded([sys.executable, '-c', 'print("x" * 1200000)'], temp, {}, 3)
            self.assertEqual(result['stopped'], 'output_limit')
            self.assertEqual(len(result['output']), 20000)
            result = _run_bounded([sys.executable, '-c', 'import time; time.sleep(3)'], temp, {}, .1)
            self.assertEqual(result['stopped'], 'timeout')

    def test_market_tool_needs_its_own_source_grant(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); store = Store(root)
            try:
                box = Toolbox(Workspace(root), store, Policy(approve_writes=True), store.create('test'))
                with patch('eira_harness.market_data.fetch_prices') as fetch:
                    with self.assertRaises(HarnessError): box.market_prices('coinbase', 'BTC-USD')
                    fetch.assert_not_called()
                    box.policy.allowed_data_sources.add('coinbase')
                    box.market_prices('coinbase', 'BTC-USD')
                    fetch.assert_called_once_with('coinbase', 'BTC-USD')
            finally:
                store.close()


class DeadlineTests(unittest.TestCase):
    def test_body_and_headers_have_total_deadline(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                try:
                    if self.path == '/headers':
                        # Real HTTP header reads must have the same deadline.
                        for chunk in [b'HTTP/1.0 200 OK\r\n', b'X-Fixture: yes\r\n', b'Content-Length: 0\r\n', b'\r\n']:
                            self.wfile.write(chunk); self.wfile.flush(); time.sleep(.08)
                    else:
                        self.send_response(200); self.send_header('Content-Length', '10'); self.end_headers()
                        for _ in range(10):
                            self.wfile.write(b'x'); self.wfile.flush(); time.sleep(.04)
                except (BrokenPipeError, ConnectionResetError): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            for path in ['/body', '/headers']:
                started = time.monotonic()
                with self.assertRaises(HarnessError):
                    request_bytes(f'http://127.0.0.1:{server.server_port}{path}', timeout=.15, public_only=False)
                self.assertLess(time.monotonic() - started, 1)
        finally:
            server.shutdown(); server.server_close(); thread.join()

if __name__ == '__main__': unittest.main()
