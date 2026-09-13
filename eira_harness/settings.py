"""User-owned model preferences. Credentials and permissions are never persisted."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .provider import PROFILES, _model_name, _validate_endpoint
from .security import HarnessError, Redactor, atomic_write, bounded_json_loads


def settings_path() -> Path:
    base = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
    if not base.is_absolute():
        raise HarnessError('XDG_CONFIG_HOME must be an absolute path.')
    return base / 'eira' / 'settings.json'


def validate(data):
    if not isinstance(data, dict) or set(data) - {'provider', 'model', 'base_url'}:
        raise HarnessError('Eira settings may contain only provider, model, and base_url.')
    if data.get('provider') not in PROFILES:
        raise HarnessError('Saved Eira provider is invalid.')
    _model_name(data.get('model'))
    if data.get('base_url') is not None:
        _validate_endpoint(data['base_url'])
    if Redactor()(json.dumps(data)) != json.dumps(data):
        raise HarnessError('Credentials cannot be saved in model preferences.')
    return data


def load_settings():
    path = settings_path()
    if path.is_symlink() or path.parent.is_symlink():
        raise HarnessError('Eira settings must not be symlinks.')
    if not path.exists():
        return {}
    with path.open('rb') as source:
        raw = source.read(8193)
    if len(raw) > 8192:
        raise HarnessError('Eira settings exceed the size limit.')
    return validate(bounded_json_loads(raw))


def save_settings(provider, model, base_url):
    data = validate({'provider': provider, 'model': model, 'base_url': base_url})
    path = settings_path()
    if path.is_symlink() or path.parent.is_symlink():
        raise HarnessError('Eira settings must not be symlinks.')
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    atomic_write(path, json.dumps(data, indent=2) + '\n')
    os.chmod(path, 0o600)


def resolve_settings(args):
    saved = load_settings()
    selected = args.provider or os.getenv('EIRA_PROVIDER') or saved.get('provider', 'openai')
    if selected not in PROFILES:
        raise HarnessError('Choose a supported provider; run Eira providers to list them.')
    same = selected == saved.get('provider')
    args.provider = selected
    args.model = args.model or os.getenv('EIRA_MODEL') or (saved.get('model', '') if same else '')
    args.base_url = args.base_url or os.getenv('EIRA_BASE_URL') or (saved.get('base_url') if same else None)
    return args
