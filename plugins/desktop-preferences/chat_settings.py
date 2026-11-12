"""Shared desktop/phone preference collection settings."""
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile


def settings_path(path=None):
    return Path(path) if path else Path.home() / '.artificial-life' / 'preference-settings.json'


def read_settings(path=None):
    target = settings_path(path)
    if not target.exists():
        return {'schema': 'preference-settings/v1', 'deep_personal_reading': False}
    value = json.loads(target.read_text(encoding='utf-8'))
    if not isinstance(value, dict) or value.get('schema') != 'preference-settings/v1' or type(value.get('deep_personal_reading')) is not bool:
        raise ValueError('Invalid preference settings.')
    return value


def update_settings(enabled, path=None):
    if type(enabled) is not bool:
        raise ValueError('deep_personal_reading must be boolean.')
    target = settings_path(path)
    value = {**read_settings(target), 'deep_personal_reading': enabled}
    target.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile('w', encoding='utf-8', dir=target.parent, delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return value
