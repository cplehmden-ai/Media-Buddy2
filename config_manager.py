"""Media-Buddy V2: shared defaults and scraper-specific overrides.

Configuration files are read-only; a missing override inherits the global value.
Explicit False is never treated as missing.
"""
from __future__ import annotations

import json
from pathlib import Path


class ConfigError(ValueError):
    pass


def _read_json(path: Path) -> dict:
    if not path.is_file():
        raise ConfigError(f"Missing configuration file: {path}")
    try:
        result = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Cannot load {path}: {exc}") from exc
    if not isinstance(result, dict):
        raise ConfigError(f"Configuration root must be an object: {path}")
    return result


def _merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


class ConfigManager:
    def __init__(self, directory: str | Path, module_file: str = "series_scraper_config.json"):
        directory = Path(directory)
        from configuration_bootstrap import ensure_configuration
        ensure_configuration(directory, files=["Media-Buddy.json", module_file])
        from configuration_store import effective
        self.global_config = effective(directory / "Media-Buddy.json")
        self.module_config = effective(directory / module_file)
        self.effective = _merge(self.global_config, self.module_config)

    def get(self, key: str, default=None):
        return self.effective.get(key, default)

    def section(self, key: str) -> dict:
        value = self.get(key, {})
        if not isinstance(value, dict):
            raise ConfigError(f"Expected object for section {key!r}")
        return value

    def network_timeout(self, *, image: bool = False) -> tuple[float, float]:
        network = self.section("Network")
        connect = network.get("ConnectTimeout", 15)
        read = (self.section("Image").get("Timeout", 45)
                if image else network.get("ReadTimeout", 30))
        for label, value in (("ConnectTimeout", connect), ("ReadTimeout", read)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 3600:
                raise ConfigError(f"Invalid {label}: {value!r} (expected 0 < seconds <= 3600)")
        return (float(connect), float(read))
