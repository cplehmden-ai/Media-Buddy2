"""Recognize explicit cutlisted series parts and append encoded MKVs safely."""
from __future__ import annotations
import json
import re
import subprocess
from pathlib import Path
from recode_file import _read_cutlist

_PART = re.compile(r'^(?P<episode>(?:s\d{1,4}e\d{1,4}|\d{1,4}x\d{1,4}))(?P<letter>[a-z])$', re.I)

def cutlist_for(path: Path):
    """Return validated cutlist path; raises on malformed/ambiguous lists."""
    found, _ = _read_cutlist(path, None, {'Enabled': True})
    return found

def classify(files, source):
    """Return complete groups, unsafe fragments, and normal videos.

    All lettered files are withheld from normal processing when they could
    represent a split episode. Never silently publish an incomplete group.
    """
    buckets = {}
    normal = []
    for path in files:
        match = _PART.fullmatch(path.stem)
        if not match:
            normal.append(path)
            continue
        key = (path.parent.relative_to(source), match.group('episode').casefold())
        buckets.setdefault(key, []).append((match.group('letter').lower(), path))
    groups, unsafe = [], []
    for (relative_dir, episode), entries in buckets.items():
        entries.sort(key=lambda item: item[0])
        letters = [letter for letter, _ in entries]
        paths = [path for _, path in entries]
        expected = [chr(97 + i) for i in range(len(entries))]
        try:
            valid_cuts = all(cutlist_for(path) is not None for path in paths)
        except Exception as exc:
            unsafe.append((paths, f'Ungültige Schnittliste: {exc}'))
            continue
        if len(paths) >= 2 and letters == expected and valid_cuts:
            groups.append((relative_dir, episode, paths))
        else:
            unsafe.append((paths, f'Keine vollständige Gruppe mit Schnittlisten (Teile: {letters})'))
    return groups, unsafe, normal

def _track_signature(path, mkvmerge):
    proc = subprocess.run([str(mkvmerge), '-J', str(path)], capture_output=True, text=True, encoding='utf-8', errors='replace')
    if proc.returncode != 0:
        raise RuntimeError(f'mkvmerge konnte Tracks nicht prüfen: {path}: {proc.stderr[-500:]}')
    info = json.loads(proc.stdout)
    return [(t.get('type'), t.get('codec'), t.get('properties', {}).get('audio_channels'),
             t.get('properties', {}).get('pixel_dimensions'), t.get('properties', {}).get('default_duration'))
            for t in info.get('tracks', [])]

def append_parts(parts, destination, mkvmerge, report, immediate_requested=lambda: False):
    """Append encoded parts; publish only complete output, never overwrite."""
    if destination.exists():
        raise FileExistsError(f'Zieldatei existiert: {destination}')
    signature = _track_signature(parts[0], mkvmerge)
    for part in parts[1:]:
        if _track_signature(part, mkvmerge) != signature:
            raise ValueError(f'Inkompatible Trackstruktur beim Zusammenfügen: {part}')
    partial = destination.with_name('.' + destination.name + '.joining.partial.mkv')
    if partial.exists():
        raise FileExistsError(f'Temporäre Zieldatei existiert bereits: {partial}')
    args = [str(mkvmerge), '--gui-mode', '--output', str(partial), str(parts[0])]
    for part in parts[1:]:
        args += ['+', str(part)]
    proc = None
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, text=True, encoding='utf-8', errors='replace')
        for line in proc.stdout:
            if immediate_requested():
                proc.kill()
                break
            if line.strip() and not line.startswith('#GUI#progress'):
                report('mkvmerge: ' + line.strip())
        rc = proc.wait()
        if immediate_requested():
            raise InterruptedError('Sofortabbruch beim Zusammenfügen')
        if rc not in (0, 1) or not partial.is_file() or not partial.stat().st_size:
            raise RuntimeError(f'mkvmerge fehlgeschlagen: Rückgabewert {rc}')
        if destination.exists():
            raise FileExistsError(f'Zieldatei inzwischen vorhanden: {destination}')
        partial.rename(destination)
        return rc
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill(); proc.wait()
        partial.unlink(missing_ok=True)
