#!/usr/bin/env python3
"""Find orphaned NFOs using global Media-Buddy video extensions.

Supports Kodi 22 per-episode NFO names: <video-stem>-SxxExx.nfo.
"""
from __future__ import annotations
from linked_media_walk import linked_dirs
import re
import sys
from pathlib import Path
from config_manager import _read_json, ConfigError

EPISODE_SUFFIX = re.compile(r"-S\d{2,}E\d{2,}$", re.IGNORECASE)


def finde_fehlende_mkv(ordner_pfad, ausgabe_datei="fehlende_mkv_dateien.txt"):
    folder = Path(str(ordner_pfad).strip().strip('"')).expanduser().resolve()
    if not folder.is_dir():
        print(f"Fehler: Der Ordner '{folder}' existiert nicht.")
        return []
    config = _read_json(Path(__file__).resolve().parent / "Media-Buddy.json")
    configured = config.get("FileExtensions", [])
    if not isinstance(configured, list) or not all(isinstance(e, str) for e in configured):
        raise ConfigError("FileExtensions must be a list of strings")
    extensions = {'.' + e.lower().lstrip('*.') for e in configured if e.lstrip('*.')}
    missing = []
    for directory in sorted((folder, *list(linked_dirs(folder)))):
        files = [p for p in directory.iterdir() if p.is_file()]
        videos = {p.stem.casefold() for p in files if p.suffix.casefold() in extensions}
        for nfo in files:
            if nfo.suffix.casefold() != '.nfo' or nfo.name.casefold() in ('tvshow.nfo', 'movie.nfo'):
                continue
            stem = nfo.stem.casefold()
            # A per-episode NFO belongs to the unsuffixed multi-episode video.
            parent_stem = EPISODE_SUFFIX.sub('', stem)
            if stem not in videos and parent_stem not in videos:
                missing.append(nfo)
    if missing:
        output = Path(ausgabe_datei)
        output.write_text(f"NFO-Dateien ohne passende Videodatei ({len(missing)}):\n" +
                          "\n".join(map(str, missing)) + "\n", encoding="utf-8")
        print(f"{len(missing)} verwaiste NFO(s) gefunden: {output}")
    else:
        print("Keine verwaisten Video-NFOs gefunden.")
    return missing


if __name__ == '__main__':
    folder = sys.argv[1] if len(sys.argv) > 1 else input('Ordner: ').strip()
    try:
        finde_fehlende_mkv(folder)
    except (ConfigError, OSError) as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        raise SystemExit(2)
