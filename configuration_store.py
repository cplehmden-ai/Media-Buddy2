"""Shared, non-destructive JSON configuration access (Test 127d)."""
from __future__ import annotations
import copy
import json
import os
import tempfile
from pathlib import Path
from configuration_defaults import DEFAULTS

class ConfigurationError(ValueError):
    pass

_MISSING = object()

def load_raw(path):
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ConfigurationError(f"Konfiguration nicht lesbar: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigurationError(f"JSON-Wurzel muss ein Objekt sein: {path}")
    return data

def merge_defaults(defaults, existing):
    """Return effective values; never mutate either input; preserve unknown keys."""
    if not isinstance(defaults, dict) or not isinstance(existing, dict):
        raise ConfigurationError("Expected JSON objects")
    result = copy.deepcopy(defaults)
    for key, value in existing.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_defaults(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result

def effective(path):
    path = Path(path)
    defaults = DEFAULTS.get(path.name, {})
    if not path.exists():
        return copy.deepcopy(defaults)
    return merge_defaults(defaults, load_raw(path))

def get_value(path, keys, default=_MISSING):
    if isinstance(keys, str):
        keys = tuple(keys.split('.'))
    data = effective(path)
    for key in keys:
        if not isinstance(data, dict) or key not in data:
            return None if default is _MISSING else copy.deepcopy(default)
        data = data[key]
    return copy.deepcopy(data)

def write_atomic(path, data):
    path = Path(path)
    if not isinstance(data, dict):
        raise ConfigurationError("JSON-Wurzel muss ein Objekt sein")
    # Never silently replace malformed user files.
    if path.exists():
        load_raw(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                prefix='.' + path.name + '.', suffix='.tmp', delete=False) as f:
            temporary = Path(f.name)
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

def update_values(path, changes):
    """Update only explicitly supplied nested key paths, preserving all other keys."""
    path = Path(path)
    data = load_raw(path) if path.exists() else {}
    for keys, value in changes.items():
        if isinstance(keys, str):
            keys = tuple(keys.split('.'))
        if not keys or any(not isinstance(k, str) or not k for k in keys):
            raise ConfigurationError('Ungültiger Einstellungspfad')
        node = data
        for key in keys[:-1]:
            if key not in node:
                node[key] = {}
            if not isinstance(node[key], dict):
                raise ConfigurationError('Abschnitt ist kein Objekt: ' + '.'.join(keys))
            node = node[key]
        node[keys[-1]] = copy.deepcopy(value)
    if changes:
        write_atomic(path, data)
    return data
