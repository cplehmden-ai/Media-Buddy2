"""Safe shared music track/sidecar renaming, without touching media contents."""
from pathlib import Path
import re

SIDECAR_EXTENSIONS={'.lrc','.txt','.cue','.jpg','.jpeg','.png','.webp'}
INVALID=re.compile(r'[<>:"/\\|?*\x00-\x1f]')

def safe_name(value):
    value=INVALID.sub('_',str(value)).strip().rstrip(' .')
    return value or '_'

def move_track_group(original, destination):
    original,destination=Path(original),Path(destination)
    if original==destination:return original
    sidecars=[p for p in original.parent.iterdir() if p.is_file() and p.stem==original.stem
              and p!=original and p.suffix.lower() in SIDECAR_EXTENSIONS]
    moves=[(original,destination)]+[(p,destination.with_suffix(p.suffix)) for p in sidecars]
    targets=[dst for _,dst in moves]
    if len(set(targets))!=len(targets) or any(dst.exists() and dst!=src for src,dst in moves):
        raise FileExistsError(f'Target already exists: {destination}')
    done=[]
    created_dir = not destination.parent.exists()
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        for src,dst in moves:
            src.rename(dst);done.append((src,dst))
    except OSError:
        for src,dst in reversed(done):
            dst.rename(src)
        if created_dir:
            try: destination.parent.rmdir()
            except OSError: pass
        raise
    return destination

def destination_for_tags(path, tags, patterns):
    path=Path(path)
    title=str(tags.get('title','')).strip()
    artist=str(tags.get('artist','')).strip()
    if not title or not artist:return path
    track_text=str(tags.get('tracknumber','')).split('/')[0].strip()
    track=int(track_text) if track_text.isdigit() else None
    album=str(tags.get('album','')).strip()
    albumartist=str(tags.get('albumartist','')).strip()
    disc=str(tags.get('discnumber','')).split('/')[0].strip() or '1'
    if album and track is not None:
        compilation=albumartist.casefold() in {'various artists','various artist','various'}
        pattern=patterns.get('CompilationTrackPattern' if compilation else 'AlbumTrackPattern',
                             '{track} - {artist} - {title}' if compilation else '{track} - {title}')
    else:
        pattern=patterns.get('SingleTrackPattern','{artist} - {title}')
    name=pattern.format(artist=artist,title=title,album=album,albumartist=albumartist,
                        track=f'{track:02d}' if track is not None else '',disc=disc)
    return path.with_name(safe_name(name)+path.suffix)


def destination_for_single_artist(path, tags, patterns):
    """Place loose tracks in their tagged artist's sibling folder only.

    Album tracks are managed by album postprocessing; never move them here.
    This deliberately leaves unrelated files and the source artist directory alone.
    """
    path = Path(path)
    artist = str(tags.get('artist', '')).strip()
    album = str(tags.get('album', '')).strip()
    if not artist or album or path.parent == path.parent.parent:
        return path
    # Avoid reinterpreting arbitrary nested directories as artist folders.
    # Only a single-artist folder with a different name is eligible.
    pattern = str(patterns.get('ArtistFolderPattern', '{albumartist}') or '{albumartist}')
    albumartist = str(tags.get('albumartist', '')).strip()
    try:
        name = pattern.format(artist=artist, albumartist=artist, album='', title='', year='')
    except (KeyError, ValueError):
        name = artist
    name = safe_name(name)
    if path.parent.name.casefold() == name.casefold():
        return path
    return path.parent.parent / name / path.name
