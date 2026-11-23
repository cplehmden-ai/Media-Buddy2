"""Parse single-image CUE sheets for unattended, gap-preserving track splitting."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import re


@dataclass(frozen=True)
class CueTrack:
    number: int
    title: str
    performer: str
    start: float
    metadata: dict[str, str]


@dataclass(frozen=True)
class CueAlbum:
    cue: Path
    image: Path
    tracks: tuple[CueTrack, ...]


def _cue_time(value: str) -> float:
    match = re.fullmatch(r'(\d+):(\d{2}):(\d{2})', value)
    if not match or int(match[2]) >= 60 or int(match[3]) >= 75:
        raise ValueError(f'Invalid CUE timestamp: {value}')
    return int(match[1]) * 60 + int(match[2]) + int(match[3]) / 75


def _value(line: str) -> str:
    value = line.strip()
    return value[1:-1] if len(value) >= 2 and value[0] == value[-1] == '"' else value


def parse_cue(path: Path) -> CueAlbum:
    raw = path.read_bytes()
    # CUE sheets from EAC are often Windows-1252, but UTF-8 is also common.
    for encoding in ('utf-8-sig', 'cp1252'):
        try:
            content = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    album = {}; entries = []; current = None; files = []
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r'^([A-Za-z0-9_]+)\s+(.*)$', line)
        if not m:
            continue
        key, value = m.group(1).upper(), m.group(2).strip()
        if key == 'FILE':
            file_match = re.match(r'^"([^"]+)"\s+\S+|^(\S+)\s+\S+', value)
            if not file_match:
                raise ValueError('Invalid CUE FILE entry')
            files.append(file_match.group(1) or file_match.group(2))
        elif key == 'TRACK':
            t = re.fullmatch(r'(\d+)\s+(\S+)', value)
            if not t or t.group(2).upper() != 'AUDIO':
                raise ValueError('Only AUDIO tracks are supported')
            current = {'number': int(t.group(1)), 'indexes': {}}
            entries.append(current)
        elif key == 'INDEX' and current is not None:
            ix = re.fullmatch(r'(\d{2})\s+(\d+:\d{2}:\d{2})', value)
            if ix:
                current['indexes'][int(ix.group(1))] = _cue_time(ix.group(2))
        elif key in ('TITLE', 'PERFORMER', 'SONGWRITER'):
            (current if current is not None else album)[key.lower()] = _value(value)
        elif key == 'REM' and current is None:
            rem = re.match(r'^(DATE|GENRE)\s+(.*)$', value, re.I)
            if rem:
                album[rem.group(1).lower()] = _value(rem.group(2))
    if len(files) != 1 or len(entries) < 2:
        raise ValueError('CUE must describe one album image with at least two tracks')
    image = path.parent / files[0]
    if not image.is_file():
        # A CUE may be renamed independently of the actual album image.
        alternatives = [p for p in path.parent.iterdir() if p.is_file() and p.suffix.lower() in ('.flac', '.wav', '.ape', '.wv', '.mp3', '.ogg', '.opus')]
        if len(alternatives) == 1:
            image = alternatives[0]
        else:
            raise ValueError(f'CUE audio image not found: {files[0]}')
    if len({e['number'] for e in entries}) != len(entries):
        raise ValueError('Duplicate CUE track number')
    tracks = []
    for i, entry in enumerate(entries):
        indexes = entry['indexes']
        if 1 not in indexes:
            raise ValueError(f"Missing INDEX 01 for track {entry['number']}")
        # Attach INDEX 00 pregap to the following track (except leading track 1),
        # so the full image is preserved without gaps or overlap.
        start = indexes.get(0, indexes[1]) if i else 0.0
        if tracks and start <= tracks[-1].start:
            raise ValueError('Non-increasing CUE track boundaries')
        title = entry.get('title', f"Track {entry['number']:02d}")
        performer = entry.get('performer', album.get('performer', ''))
        tags = {'title': title, 'track': str(entry['number']), 'tracknumber': str(entry['number']),
                'album': album.get('title', path.stem), 'artist': performer,
                'album_artist': album.get('performer', performer)}
        if album.get('date'):
            tags['date'] = album['date']
        if album.get('genre'):
            tags['genre'] = album['genre']
        tracks.append(CueTrack(entry['number'], title, performer, start, tags))
    return CueAlbum(path, image, tuple(tracks))


def find_cue_for_image(image: Path) -> CueAlbum | None:
    for path in sorted(image.parent.glob('*.cue')):
        try:
            parsed = parse_cue(path)
        except (OSError, ValueError):
            continue
        if parsed.image.resolve() == image.resolve():
            return parsed
    return None


def safe_track_name(track: CueTrack, extension: str) -> str:
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', track.title).strip(' .')
    return f'{track.number:02d} - {title or "Track"}{extension}'
