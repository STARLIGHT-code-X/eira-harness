import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from eira_harness.cli import main, configure_model
from eira_harness.security import HarnessError
from eira_harness.settings import load_settings, save_settings, resolve_settings
from eira_harness.store import Store


class InteractiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'HOME': str(self.root), 'XDG_CONFIG_HOME': str(self.root / 'config')}, clear=True)
        self.env.start()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.env.stop)

    def test_settings_persist_no_authority_or_credentials(self):
        save_settings('ollama', 'local-tools', None)
        data = load_settings()
        self.assertEqual(data, {'provider': 'ollama', 'model': 'local-tools', 'base_url': None})
        path = self.root / 'config/eira/settings.json'
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        path.write_text(json.dumps({**data, 'approve_writes': True}))
        with self.assertRaises(HarnessError):
            load_settings()

    def test_explicit_provider_does_not_inherit_other_endpoint(self):
        save_settings('custom', 'local', 'http://127.0.0.1:8888/v1')
        args = resolve_settings(argparse.Namespace(provider='openai', model='chosen', base_url=None))
        self.assertIsNone(args.base_url)
        self.assertEqual(args.model, 'chosen')

    def test_environment_overrides_saved_model(self):
        save_settings('ollama', 'old-model', None)
        os.environ['EIRA_MODEL'] = 'new-model'
        args = resolve_settings(argparse.Namespace(provider=None, model=None, base_url=None))
        self.assertEqual(args.model, 'new-model')
        self.assertEqual(args.provider, 'ollama')

    def test_setup_key_is_process_only(self):
        args = argparse.Namespace(provider='openai', model='', base_url=None)
        with patch('builtins.input', side_effect=['', 'example-tools']), patch('getpass.getpass', return_value='sk-test-only-not-a-real-key-1234567890'):
            configure_model(args, MagicMock())
        raw = (self.root / 'config/eira/settings.json').read_text()
        self.assertNotIn('sk-test', raw)
        self.assertNotIn('API_KEY', raw)
        self.assertEqual(args.model, 'example-tools')

    def test_no_argument_launch_and_slash_commands_never_call_model(self):
        save_settings('ollama', 'local-tools', None)
        ui = MagicMock()
        ui.prompt.side_effect = ['/help', '/status', '/new', '/sessions', '/resume invalid', '/clear', '/exit']
        with patch('pathlib.Path.cwd', return_value=self.root), patch('sys.stdin.isatty', return_value=True), patch('eira_harness.terminal.Terminal', return_value=ui), patch('eira_harness.cli.build_provider') as provider:
            self.assertEqual(main([]), 0)
        provider.return_value.complete.assert_not_called()
        ui.help.assert_called_once()
        ui.error.assert_called_once()
        with StoreContext(self.root) as store:
            self.assertEqual(len(store.sessions()), 2)

    def test_implicit_chat_flags_and_prompt_execute_real_agent(self):
        ui = MagicMock()
        ui.prompt.side_effect = ['Explain this workspace.', '/exit']
        provider = MagicMock(model='fixture-model')
        provider.complete.return_value = ({'role': 'assistant', 'content': 'Fixture response.'}, {'total_tokens': 5})
        with patch('sys.stdin.isatty', return_value=True), patch('eira_harness.terminal.Terminal', return_value=ui), patch('eira_harness.cli.build_provider', return_value=provider):
            result = main(['--workspace', str(self.root), '--provider', 'ollama', '--model', 'fixture-model', '--read-only'])
        self.assertEqual(result, 0)
        provider.complete.assert_called_once()
        events = [call.args[0] for call in ui.emit.call_args_list]
        self.assertIn('run_completed', [event['event'] for event in events])
        self.assertTrue(next(e for e in events if e['event'] == 'run_started')['read_only'])

    def test_noninteractive_launch_fails_with_actionable_command(self):
        output = io.StringIO()
        with patch('pathlib.Path.cwd', return_value=self.root), patch('sys.stdin.isatty', return_value=False), redirect_stderr(output):
            self.assertEqual(main([]), 2)
        self.assertIn('Eira run', output.getvalue())

    def test_resume_keeps_history_without_replaying_side_effect(self):
        store = Store(self.root)
        old = store.create('previous work')
        store.append(old, {'role': 'user', 'content': 'Prior context'})
        store.close()
        ui = MagicMock()
        ui.prompt.side_effect = [f'/resume {old}', 'Continue explaining.', '/exit']
        provider = MagicMock(model='local-tools')
        provider.complete.return_value = ({'role': 'assistant', 'content': 'Continued.'}, {})
        with patch('sys.stdin.isatty', return_value=True), patch('eira_harness.terminal.Terminal', return_value=ui), patch('eira_harness.cli.build_provider', return_value=provider):
            self.assertEqual(main(['chat', '--workspace', str(self.root), '--provider', 'ollama', '--model', 'local-tools']), 0)
        messages = provider.complete.call_args.args[0]
        self.assertIn('Prior context', [m.get('content') for m in messages])


class StoreContext:
    def __init__(self, root): self.store = Store(root)
    def __enter__(self): return self.store
    def __exit__(self, *args): self.store.close()
