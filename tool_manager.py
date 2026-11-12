"""Cross-platform executable discovery for Media-Buddy V2 (Test 127e)."""
from pathlib import Path
import json
import os
import shutil
import subprocess

ROOT = Path(__file__).resolve().parent
TOOL_NAMES = ('ffmpeg', 'ffprobe', 'mkvmerge', 'mkvpropedit', 'mkvextract', 'dovi_tool', 'hdr10plus_tool', 'magick', 'exiftool', 'mpv')

def _config():
    path = ROOT / 'Media-Buddy.json'
    if not path.is_file():
        return {}
    with path.open('r', encoding='utf-8-sig') as handle:
        data = json.load(handle)
    return data if isinstance(data, dict) else {}

def _is_imagemagick_appimage(path):
    """Accept only a working ImageMagick 7 CLI AppImage, not arbitrary binaries."""
    if os.name == 'nt' or path.suffix.lower() != '.appimage':
        return False
    if not path.is_file() or not os.access(path, os.X_OK):
        return False
    try:
        result = subprocess.run(
            [str(path), '-version'], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            errors='replace', timeout=8, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    import re
    return result.returncode == 0 and bool(re.search(
        r'ImageMagick\s+7(?:\.|\s|$)', result.stdout + '\n' + result.stderr,
        flags=re.IGNORECASE,
    ))


def _candidate(value, name):
    if not value:
        return None
    value = str(value).strip()
    if not value:
        return None
    path = Path(value).expanduser()
    if path.is_dir():
        for filename in (name + '.exe', name):
            candidate = path / filename
            if candidate.is_file() and (os.name == 'nt' or os.access(candidate, os.X_OK)):
                return str(candidate.resolve())
    if path.is_file() and (os.name == 'nt' or os.access(path, os.X_OK)):
        # Do not mistake a configured ffmpeg binary for ffprobe (or vice versa).
        if path.name.lower() not in (name.lower(), name.lower() + '.exe'):
            if name != 'magick' or not _is_imagemagick_appimage(path):
                return None
        return str(path.resolve())
    # A configured bare command is allowed, but a path to another executable is not.
    if Path(value).name == value:
        return shutil.which(value)
    return None

def find_tool(name, configured=None):
    """Return (path, source) or (None, reason); never discard invalid user settings."""
    name = str(name)
    if configured is None:
        data = _config()
        configured = (data.get('Tools') or {}).get(name, '')
    if configured:
        result = _candidate(configured, name)
        if result:
            return result, 'Konfiguration'
        # A Windows-only path on Linux should not prevent system discovery.
        issue = 'Konfigurierter Pfad nicht gefunden; Fallback verwendet'
    else:
        issue = 'Nicht gefunden'
    for folder in (ROOT / 'Tools', ROOT / 'Tools' / 'bin'):
        result = _candidate(str(folder), name)
        if result:
            return result, 'Projektordner Tools/' + (' (Fallback)' if configured else '')
    # Portable ImageMagick 7: detect a valid CLI AppImage in project Tools/.
    # Only use it when no regular magick executable was found there.
    if name == 'magick' and os.name != 'nt':
        for folder in (ROOT / 'Tools', ROOT / 'Tools' / 'bin'):
            if folder.is_dir():
                for appimage in sorted(folder.glob('*.AppImage')):
                    if _is_imagemagick_appimage(appimage):
                        return str(appimage.resolve()), 'Projektordner Tools/ AppImage' + (' (Fallback)' if configured else '')
    result = shutil.which(name) or (shutil.which(name + '.exe') if os.name == 'nt' else None)
    if result:
        return result, 'System-PATH' + (' (Fallback)' if configured else '')
    return None, issue

def get_tool(name: str, configured: str | None = None) -> str:
    path, source = find_tool(name, configured)
    if path:
        return path
    raise FileNotFoundError(f'{name}: {source}. Bitte unter Einstellungen → Tools prüfen.')

def tool_version(name, path):
    if not path:
        return '–'
    # No arbitrary shell execution; short timeout for hanging tools.
    version_flags = {
        'ffmpeg': '-version', 'ffprobe': '-version',
        'exiftool': '-ver', 'magick': '-version',
    }
    arguments = [version_flags.get(name, '--version')]
    try:
        proc = subprocess.run([path, *arguments], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True,
                              errors='replace', timeout=3, check=False)
        output = (proc.stdout or proc.stderr).strip().splitlines()
        return (output[0][:120] if output else 'Version nicht ermittelbar')
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f'Versionsprüfung fehlgeschlagen: {exc}'
