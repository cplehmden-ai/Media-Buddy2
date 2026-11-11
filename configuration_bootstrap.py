"""Create only missing configuration files, preserving all existing files."""
from __future__ import annotations
import copy
from pathlib import Path
from configuration_defaults import DEFAULTS
from batch_settings import read_settings, write_settings, SettingsError


def ensure_configuration(directory, *, files=None):
    directory = Path(directory)
    created = []
    selected = tuple(files) if files is not None else tuple(DEFAULTS)
    # Validate all existing files before creating anything.
    for name in selected:
        if name not in DEFAULTS:
            raise SettingsError(f"Unknown configuration: {name}")
        path = directory / name
        if path.exists():
            if not path.is_file():
                raise SettingsError(f"Configuration is not a file: {path}")
            read_settings(path)
    for name in selected:
        path = directory / name
        if path.exists():
            continue
        try:
            # Exclusive create prevents accidental overwrite if another process created it.
            with path.open('x', encoding='utf-8') as stream:
                import json
                json.dump(copy.deepcopy(DEFAULTS[name]), stream, ensure_ascii=False, indent=2)
                stream.write('\n')
            created.append(name)
        except FileExistsError:
            read_settings(path)
        except OSError as exc:
            raise SettingsError(f"Cannot create {path}: {exc}") from exc
    return created
