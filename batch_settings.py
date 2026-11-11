"""Media-Buddy V2: persistent wizard settings and immutable batch snapshots.

This module is additive: the existing Recode and Optimizer JSON sections retain
exactly their existing meaning. No running job reads settings from disk again.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import tempfile
from types import MappingProxyType


class SettingsError(ValueError):
    pass


def read_settings(path):
    from configuration_store import load_raw
    try:
        return load_raw(path)
    except ValueError as exc:
        raise SettingsError(str(exc)) from exc


def write_settings(path, data):
    from configuration_store import write_atomic
    try:
        write_atomic(path, data)
    except ValueError as exc:
        raise SettingsError(str(exc)) from exc


def update_setting(path, section, key, value):
    """Save a changed GUI option immediately; preserve unrelated settings."""
    if not section or not key:
        raise SettingsError("Section and key are required")
    if not Path(path).exists():
        from configuration_bootstrap import ensure_configuration
        ensure_configuration(Path(path).parent, files=[Path(path).name])
    from configuration_store import update_values
    update_values(path, {tuple(section.split('.')) + (key,): value})


def create_batch_snapshot(settings, wizard):
    """Capture common optimizer and wizard-specific options for one batch.

    Returns independent copies; no references into mutable GUI settings remain.
    The controller should not re-read JSON during this batch.
    """
    if wizard not in ("Movies", "Series", "Videos", "MusicVideos"):
        raise SettingsError(f"Unknown wizard: {wizard}")
    options = settings.get("Wizards", {})
    if not isinstance(options, dict):
        raise SettingsError("Wizards must be an object")
    specific = options.get(wizard, {})
    if not isinstance(specific, dict):
        raise SettingsError(f"Wizards.{wizard} must be an object")
    optimizer = settings.get("Optimizer", {})
    if not isinstance(optimizer, dict):
        raise SettingsError("Optimizer must be an object")
    return {"Wizard": wizard,
            "Optimizer": copy.deepcopy(optimizer),
            "WizardOptions": copy.deepcopy(specific)}
