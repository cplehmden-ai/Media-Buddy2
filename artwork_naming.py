"""Shared HTPC artwork naming. No media originals are modified."""
from __future__ import annotations
import json
import shutil
from pathlib import Path

PRESETS = {
    'kodi': {'poster': 'poster.jpg', 'discart': 'discart.png', 'clearlogo': 'clearlogo.png'},
    'emby': {'poster': 'poster.jpg', 'discart': 'disc.png', 'clearlogo': 'logo.png'},
    'mediaportal1': {'poster': 'folder.jpg', 'discart': 'discart.png', 'clearlogo': 'clearlogo.png'},
}

def active_presets(config_path=None):
    config_path = Path(config_path) if config_path else Path(__file__).resolve().parent / 'Media-Buddy.json'
    try:
        value = json.loads(config_path.read_text(encoding='utf-8')).get('VideoSettings', {}).get('ArtworkNamingPresets', ['kodi'])
    except (OSError, ValueError, TypeError):
        value = ['kodi']
    selected = [name for name in value if name in PRESETS] if isinstance(value, list) else []
    return selected or ['kodi']

def names_for(kind, config_path=None):
    if kind not in PRESETS['kodi']:
        return []
    return list(dict.fromkeys(PRESETS[p][kind] for p in active_presets(config_path)))

def replicate(source, kind, overwrite=False, config_path=None):
    """Copy downloaded artwork to selected alternate names; preserve existing files."""
    source = Path(source)
    if not source.is_file():
        return 0
    count = 0
    for name in names_for(kind, config_path):
        dest = source.with_name(name)
        if dest == source or (dest.exists() and not overwrite):
            continue
        tmp = dest.with_name(dest.name + '.mb-copy-tmp')
        try:
            shutil.copyfile(source, tmp)
            tmp.replace(dest)
            count += 1
        finally:
            tmp.unlink(missing_ok=True)
    return count
