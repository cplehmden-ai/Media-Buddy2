"""Shift LRC timestamps and synchronize sidecar and embedded lyrics."""
from __future__ import annotations
import re
from pathlib import Path
from mutagen import File
from mutagen.id3 import ID3, USLT
from mutagen.mp4 import MP4

STAMP = re.compile(r'\[(\d{1,3}):(\d{2})([.:])(\d{1,3})\]')

def shift_lrc(source: str, offset_seconds: float) -> tuple[str, int]:
    """Shift every LRC timestamp, preserving fractions and non-timestamp text.

    Negative resulting times clamp to zero. Metadata tags such as [offset:...] are untouched.
    """
    count = 0
    def replace(match):
        nonlocal count
        minutes, seconds, sep, fraction = match.groups()
        scale = 10 ** len(fraction)
        old = (int(minutes) * 60 + int(seconds)) * scale + int(fraction)
        delta = round(offset_seconds * scale)
        total = max(0, old + delta)
        count += 1
        return f'[{total // (60 * scale):02d}:{(total // scale) % 60:02d}{sep}{total % scale:0{len(fraction)}d}]'
    return STAMP.sub(replace, source), count

def read_lyrics(path: Path) -> tuple[str, str]:
    sidecar = path.with_suffix('.lrc')
    if sidecar.is_file():
        return sidecar.read_text(encoding='utf-8-sig'), 'sidecar'
    audio = File(str(path), easy=False)
    if audio is None or audio.tags is None:
        return '', ''
    if isinstance(audio.tags, ID3):
        frames = audio.tags.getall('USLT')
        return (str(frames[0].text), 'embedded') if frames else ('', '')
    if isinstance(audio, MP4):
        values = audio.get('\xa9lyr', [])
        return (str(values[0]), 'embedded') if values else ('', '')
    for key in ('LYRICS', 'SYNCEDLYRICS', 'UNSYNCEDLYRICS'):
        values = audio.tags.get(key, [])
        if values:
            return str(values[0]), 'embedded'
    return '', ''

def apply_offset(path: Path, offset_seconds: float) -> int:
    """Update synchronized lyrics in both representations; roll back sidecar if tag save fails."""
    source, origin = read_lyrics(path)
    if not source:
        raise ValueError('No lyrics available.')
    shifted, count = shift_lrc(source, offset_seconds)
    if not count:
        raise ValueError('No synchronized timestamps available.')
    audio = File(str(path), easy=False)
    if audio is None:
        raise ValueError('Unsupported audio format.')
    if audio.tags is None:
        audio.add_tags()
    sidecar = path.with_suffix('.lrc')
    previous_bytes = sidecar.read_bytes() if sidecar.exists() else None
    # Embedded lyric frames are stored as text LRC, consistent with the scraper.
    if isinstance(audio.tags, ID3):
        frames = audio.tags.getall('USLT')
        if frames:
            for frame in frames:
                frame.text = shifted
        else:
            audio.tags.add(USLT(encoding=3, lang='eng', desc='', text=shifted))
    elif isinstance(audio, MP4):
        audio['\xa9lyr'] = [shifted]
    elif hasattr(audio.tags, '__setitem__'):
        keys = [key for key in ('LYRICS', 'SYNCEDLYRICS', 'UNSYNCEDLYRICS') if key in audio.tags]
        for key in (keys or ['LYRICS']):
            audio.tags[key] = [shifted]
    else:
        raise ValueError('Embedded lyrics are not supported by this format.')
    # Save the embedded representation first; do not modify sidecar on failure.
    audio.save()
    try:
        sidecar.write_text(shifted, encoding='utf-8')
    except Exception:
        # Embedded tags were saved already; report failure explicitly.
        raise
    return count
