"""Matroska tags for Media-Buddy V2; never modifies source-side NFO files.

Only call on processed output files. Requires mkvpropedit from MKVToolNix.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET

EXTENSIONS = {'.mkv', '.mk3d'}
EPISODE_END = re.compile(rb'</episodedetails\s*>', re.I)


@dataclass(frozen=True)
class TagResult:
    file: Path
    status: str  # tagged, skipped, error
    message: str = ''


def _text(root: ET.Element, name: str) -> str:
    el = root.find(name)
    return (el.text or '').strip() if el is not None and el.text else ''


def _id(root: ET.Element, kind: str) -> str:
    for el in root.findall('uniqueid'):
        if (el.get('type') or '').lower() == kind:
            return (el.text or '').strip()
    if kind == 'imdb':
        return _text(root, 'id')
    return ''


def _nfo(path: Path, *, legacy_first: bool = False) -> ET.Element:
    if not legacy_first:
        return ET.parse(path).getroot()
    # Legacy multi-episode NFO has multiple top-level roots. Stop after the
    # first closing episodedetails; no subsequent malformed XML is examined.
    data = path.read_bytes()
    match = EPISODE_END.search(data)
    if not match:
        return ET.fromstring(data)
    start = re.search(rb'<episodedetails(?:\s[^>]*)?>', data, re.I)
    if not start or start.start() >= match.start():
        raise ValueError('First episodedetails element not found')
    return ET.fromstring(data[start.start():match.end()])


def _simple(tag: ET.Element, name: str, value: str) -> None:
    if not value:
        return
    item = ET.SubElement(tag, 'Simple')
    ET.SubElement(item, 'Name').text = name
    ET.SubElement(item, 'String').text = value
    ET.SubElement(item, 'TagLanguage').text = 'und'
    ET.SubElement(item, 'DefaultLanguage').text = '1'


def _tag(tags: ET.Element, level: int, fields: list[tuple[str, str]]) -> None:
    tag = ET.SubElement(tags, 'Tag')
    target = ET.SubElement(tag, 'Targets')
    ET.SubElement(target, 'TargetTypeValue').text = str(level)
    for name, value in fields:
        _simple(tag, name, value)


def _fields_movie(root: ET.Element) -> list[tuple[str, str]]:
    return [('TITLE', _text(root, 'title')),
            ('TMDB', _id(root, 'tmdb')),
            ('IMDB', _id(root, 'imdb')),
            ('DATE_RELEASED', _text(root, 'premiered') or _text(root, 'releasedate')),
            ('LAW_RATING', _text(root, 'mpaa')),
            ('SUMMARY', _text(root, 'plot'))]


def build_movie_tags(movie_nfo: Path) -> ET.ElementTree:
    root = _nfo(Path(movie_nfo))
    if root.tag != 'movie':
        raise ValueError(f'Expected <movie> in {movie_nfo}, got <{root.tag}>')
    tags = ET.Element('Tags')
    _tag(tags, 50, _fields_movie(root))
    return ET.ElementTree(tags)


def build_series_tags(show_nfo: Path, episode_nfo: Path) -> ET.ElementTree:
    show = _nfo(Path(show_nfo))
    episode = _nfo(Path(episode_nfo), legacy_first=True)
    if show.tag != 'tvshow' or episode.tag != 'episodedetails':
        raise ValueError('Expected <tvshow> and <episodedetails> NFO roots')
    tags = ET.Element('Tags')
    _tag(tags, 70, [('TITLE', _text(show, 'title')),
                    ('TVDB', _id(show, 'tvdb')),
                    ('TMDB', _id(show, 'tmdb')),
                    ('IMDB', _id(show, 'imdb')),
                    ('DATE_RELEASED', _text(show, 'premiered')),
                    ('NETWORK', _text(show, 'studio')),
                    ('LAW_RATING', _text(show, 'mpaa')),
                    ('SUMMARY', _text(show, 'plot'))])
    _tag(tags, 60, [('PART_NUMBER', _text(episode, 'season'))])
    _tag(tags, 50, [('PART_NUMBER', _text(episode, 'episode')),
                    ('DATE_RELEASED', _text(episode, 'aired') or _text(episode, 'premiered')),
                    ('TITLE', _text(episode, 'title')),
                    ('SUMMARY', _text(episode, 'plot'))])
    return ET.ElementTree(tags)


def _movie_nfo(video: Path) -> Path:
    candidates = [video.with_suffix('.nfo'), video.parent / 'movie.nfo']
    for path in candidates:
        if path.is_file():
            return path
    raise FileNotFoundError(f'No movie NFO for {video}')


def _episode_nfo(video: Path) -> Path:
    direct = video.with_suffix('.nfo')
    if direct.is_file():
        return direct
    # Kodi 22 multi-episode convention: filename-SxxExx.nfo
    matches = sorted(video.parent.glob(video.stem + '-S??E??.nfo'))
    if matches:
        return matches[0]
    raise FileNotFoundError(f'No episode NFO for {video}')


def _show_nfo(video: Path) -> Path:
    for parent in video.parents:
        candidate = parent / 'tvshow.nfo'
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f'No tvshow.nfo for {video}')


def _run(video: Path, tree: ET.ElementTree, *, mkvpropedit: str,
         dry_run: bool = False) -> TagResult:
    if dry_run:
        return TagResult(video, 'tagged', 'dry-run; no file modified')
    from tool_manager import find_tool
    executable = (find_tool('mkvpropedit', mkvpropedit)[0] if mkvpropedit != 'mkvpropedit'
                  else find_tool('mkvpropedit')[0])
    if executable is None:
        return TagResult(video, 'error', f'mkvpropedit not found: {mkvpropedit}')
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix='.xml', prefix='media_buddy_tags_',
                                         delete=False) as f:
            temp_path = Path(f.name)
        tree.write(temp_path, encoding='utf-8', xml_declaration=True)
        # V1-proven order: tags, short pause, tags again, then segment title.
        # The final separate header edit is important for MediaInfo visibility
        # with some FFmpeg-generated Matroska containers.
        tag_command = [executable, '--parse-mode', 'full', str(video),
                       '--tags', f'all:{temp_path}']
        title_command = [executable, '--parse-mode', 'full', str(video),
                         '--edit', 'info', '--set', f'title={video.stem}']
        for step, command in ((1, tag_command), (2, tag_command),
                              (3, title_command)):
            result = subprocess.run(command, capture_output=True, text=True,
                                    errors='replace')
            if result.returncode:
                detail = (result.stderr or result.stdout or '').strip()
                return TagResult(video, 'error',
                                 f'mkvpropedit step {step}/3 failed: {detail}')
            if step == 1:
                time.sleep(0.05)
        # Match V1's brief pause before removing the temporary XML.
        time.sleep(0.05)
        return TagResult(video, 'tagged',
                         'mkvpropedit 3/3 successful (tags twice, segment title)')
    except OSError as exc:
        return TagResult(video, 'error', str(exc))
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def tag_movies(video: str | Path, *, mkvpropedit: str = 'mkvpropedit',
               dry_run: bool = False) -> TagResult:
    video = Path(video)
    if video.suffix.lower() not in EXTENSIONS:
        return TagResult(video, 'skipped', 'Not a Matroska file extension')
    try:
        return _run(video, build_movie_tags(_movie_nfo(video)),
                    mkvpropedit=mkvpropedit, dry_run=dry_run)
    except (OSError, ValueError, ET.ParseError) as exc:
        return TagResult(video, 'error', str(exc))


def tag_series(video: str | Path, *, mkvpropedit: str = 'mkvpropedit',
               dry_run: bool = False) -> TagResult:
    video = Path(video)
    if video.suffix.lower() not in EXTENSIONS:
        return TagResult(video, 'skipped', 'Not a Matroska file extension')
    try:
        return _run(video, build_series_tags(_show_nfo(video), _episode_nfo(video)),
                    mkvpropedit=mkvpropedit, dry_run=dry_run)
    except (OSError, ValueError, ET.ParseError) as exc:
        return TagResult(video, 'error', str(exc))


def build_musicvideo_tags(musicvideo_nfo: Path) -> ET.ElementTree:
    root = _nfo(Path(musicvideo_nfo))
    if root.tag != 'musicvideo':
        raise ValueError(f'Expected <musicvideo> in {musicvideo_nfo}, got <{root.tag}>')
    tags = ET.Element('Tags')
    artists = [el.text.strip() for el in root.findall('artist') if el.text and el.text.strip()]
    fields = [('TITLE', _text(root, 'title')),
              ('ARTIST', ', '.join(artists)),
              ('ALBUM', _text(root, 'album')),
              ('DATE_RELEASED', _text(root, 'premiered') or _text(root, 'year')),
              ('GENRE', _text(root, 'genre')),
              ('SUMMARY', _text(root, 'plot'))]
    _tag(tags, 50, fields)
    return ET.ElementTree(tags)

def tag_musicvideos(video: str | Path, *, mkvpropedit: str = 'mkvpropedit',
                    dry_run: bool = False) -> TagResult:
    video = Path(video)
    if video.suffix.lower() not in EXTENSIONS:
        return TagResult(video, 'skipped', 'Not a Matroska file extension')
    try:
        return _run(video, build_musicvideo_tags(video.with_suffix('.nfo')),
                    mkvpropedit=mkvpropedit, dry_run=dry_run)
    except (OSError, ValueError, ET.ParseError) as exc:
        return TagResult(video, 'error', str(exc))
