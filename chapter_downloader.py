#!/usr/bin/env python3
"""Shared ChaptersDB chapter-file downloader for movies and series."""
from __future__ import annotations
import re
import time
from pathlib import Path
from urllib.parse import quote
import unicodedata
import requests
import subprocess
from ffmpeg_manager import discover, FFmpegError
import json

API_ROOT = 'https://chaptersdb.com/api/v1'
IMDB = re.compile(r'^tt[0-9]{7,}$', re.I)
STAMP = re.compile(r'^(\d{2,}):(\d{2}):(\d{2})\.(\d{3})$')


def chapter_path(video: Path) -> Path:
    return video.with_name(video.stem + '-chapters.txt')


def _fetch_json(session, endpoint, timeout, retries=3, pause=0.5):
    url = API_ROOT + endpoint
    for attempt in range(retries):
        try:
            response = session.get(url, timeout=timeout, headers={'Accept': 'application/json'})
            if response.status_code == 404:
                return None, 'not found or not approved'
            if response.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(pause * (attempt + 1))
                continue
            if response.status_code != 200:
                try:
                    detail = response.json().get('error', '')
                except (ValueError, AttributeError):
                    detail = ''
                return None, f'HTTP {response.status_code}' + (f': {detail}' if detail else '')
            result = response.json()
            if not isinstance(result, dict):
                return None, 'invalid JSON response'
            if result.get('error'):
                return None, str(result['error'])
            return result, None
        except (requests.RequestException, ValueError) as exc:
            if attempt == retries - 1:
                return None, f'{type(exc).__name__}: {exc}'
            time.sleep(pause * (attempt + 1))
    return None, 'request failed'


def _milliseconds(value):
    match = STAMP.fullmatch(str(value or ''))
    if not match:
        return None
    h, m, s, ms = map(int, match.groups())
    if m > 59 or s > 59:
        return None
    return ((h * 60 + m) * 60 + s) * 1000 + ms


def format_chapters(entries, translate=None):
    """Sort by timestamp and format as MKVToolNix simple chapters."""
    if not isinstance(entries, list) or not entries:
        return None
    prepared = []
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        stamp = entry.get('time')
        millis = _milliseconds(stamp)
        if millis is None:
            return None
        prepared.append((millis, stamp, entry.get('name')))
    prepared.sort(key=lambda item: item[0])
    if len({item[0] for item in prepared}) != len(prepared):
        return None
    lines = []
    for index, (_, stamp, raw_name) in enumerate(prepared, 1):
        name = str(raw_name or f'Chapter {index}').replace('\r', ' ').replace('\n', ' ').strip()
        if translate is not None:
            try:
                name = str(translate(name) or name).replace('\r', ' ').replace('\n', ' ').strip()
            except Exception as exc:
                print(f'[CHAPTER TRANSLATE WARN] Chapter {index}: {exc}; original retained')
        lines.extend((f'CHAPTER{index:02d}={stamp}', f'CHAPTER{index:02d}NAME={name}'))
    return '\n'.join(lines) + '\n'


def _normalized_title(value):
    value = unicodedata.normalize('NFKD', str(value or '')).casefold()
    return ' '.join(re.findall(r'[a-z0-9]+', ''.join(ch for ch in value if not unicodedata.combining(ch))))


def _search_movie(session, titles, year, timeout):
    """Find an unambiguous ChaptersDB movie by title AND release year."""
    seen = set()
    for title in titles:
        normalized = _normalized_title(title)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        # Search a shorter phrase too: the API may tokenize queries differently.
        queries = [title]
        words = str(title).split()
        if len(words) > 3:
            queries.append(' '.join(words[:3]))
        for query in queries:
            data, error = _fetch_json(session, '/search?q=' + quote(query, safe='') + '&type=movie&limit=30', timeout)
            if error:
                if error != 'not found or not approved':
                    print(f'[CHAPTER SEARCH WARN] {query}: {error}')
                continue
            matches = []
            for item in data.get('results', []):
                if not isinstance(item, dict) or item.get('type') != 'movie':
                    continue
                if year and str(item.get('year') or '')[:4] != str(year)[:4]:
                    continue
                if _normalized_title(item.get('title')) == normalized or _normalized_title(item.get('slug')).replace(' ', '') == normalized.replace(' ', ''):
                    matches.append(item)
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                print(f'[CHAPTER SEARCH WARN] ambiguous results for {title}; skipping')
                return None
    return None


def _ffprobe_executable():
    """Use the shared cross-platform discovery, including PATH fallback."""
    try:
        return str(discover().ffprobe)
    except FFmpegError:
        return None


def has_embedded_chapters(video: Path, timeout=30, runner=None):
    """Return True/False if probed, None if unavailable or unreadable.

    Never modify the media file. For .stub files, return None without probing.
    """
    video = Path(video)
    if video.suffix.casefold() == '.stub' or not video.is_file():
        return None
    executable = _ffprobe_executable()
    if not executable:
        return None
    run = runner or subprocess.run
    try:
        result = run([executable, '-v', 'error', '-show_entries', 'chapter=start_time',
                      '-of', 'json', str(video)], capture_output=True, text=True,
                     timeout=timeout, check=False)
        if result.returncode != 0:
            return None
        payload = json.loads(result.stdout)
        chapters = payload.get('chapters')
        if not isinstance(chapters, list):
            return None
        return len(chapters) > 0
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, AttributeError):
        return None


def download_movie_chapters(imdb_id, video: Path, cfg, session=None, timeout=(15, 30), titles=(), year=None, translator=None):
    """Return (written, errors); unavailable chapters are not an error."""
    if cfg.section('Movies').get('Download_Chapters', False) is not True:
        return 0, 0
    destination = chapter_path(video)
    overwrite = cfg.section('Overwrite').get('Chapters', False) is True
    if destination.exists() and not overwrite:
        print(f'[CHAPTER SKIP] {video.name}: already exists ({destination.name})')
        return 0, 0
    # Do not fetch external chapters when the real video already contains chapters.
    # If probing is unavailable, keep the previous Test 48 behavior (download).
    embedded = has_embedded_chapters(video)
    if embedded is True:
        print(f'[CHAPTER SKIP] {video.name}: embedded chapters already present')
        return 0, 0
    if embedded is None and video.suffix.casefold() != '.stub':
        print(f'[CHAPTER PROBE WARN] {video.name}: unable to inspect embedded chapters; proceeding with ChaptersDB')
    session = session or requests.Session()
    data, error = None, 'not found or not approved'
    if imdb_id and IMDB.fullmatch(str(imdb_id)):
        data, error = _fetch_json(session, '/show/' + quote(str(imdb_id), safe=''), timeout)
    if not data:
        if error and error != 'not found or not approved':
            print(f'[CHAPTER ERROR] {video.name}: IMDb lookup: {error}')
            return 0, 1
        match = _search_movie(session, titles, year, timeout)
        if not match:
            print(f'[CHAPTER MISSING] {video.name}: no matching approved ChaptersDB movie (IMDb/title/year)')
            return 0, 0
        identifier = match.get('id') or match.get('slug')
        if not identifier:
            print(f'[CHAPTER ERROR] {video.name}: search result lacks identifier')
            return 0, 1
        data, error = _fetch_json(session, '/show/' + quote(str(identifier), safe=''), timeout)
        if error:
            print(f'[CHAPTER ERROR] {video.name}: title lookup: {error}')
            return 0, 1
        print(f'[CHAPTER MATCH] {video.name}: {match.get("title")} ({match.get("year")}) via title search')
    if data.get('type') != 'movie':
        print(f'[CHAPTER SKIP] {video.name}: ChaptersDB result is not a movie')
        return 0, 0
    returned_id = str(data.get('imdbId') or '').lower()
    if returned_id and imdb_id and returned_id != str(imdb_id).lower():
        print(f'[CHAPTER ERROR] {video.name}: IMDb ID mismatch ({returned_id})')
        return 0, 1
    if year and data.get('year') and str(data['year'])[:4] != str(year)[:4]:
        print(f'[CHAPTER ERROR] {video.name}: release year mismatch')
        return 0, 1
    choices = data.get('chapters') or []
    if not isinstance(choices, list):
        print(f'[CHAPTER ERROR] {video.name}: invalid chapter collection')
        return 0, 1
    # Highest rated first; choose the first valid set in case an entry is malformed.
    choices = sorted((x for x in choices if isinstance(x, dict)),
                     key=lambda x: (int(x.get('upvotes') or 0) - int(x.get('downvotes') or 0),
                                    int(x.get('upvotes') or 0)), reverse=True)
    for choice in choices:
        content = format_chapters(choice.get('entries'), translate=translator)
        if content:
            temp = destination.with_name(destination.name + '.tmp')
            try:
                temp.write_text(content, encoding='utf-8')
                temp.replace(destination)
            except OSError as exc:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass
                print(f'[CHAPTER ERROR] {video.name}: {exc}')
                return 0, 1
            print(f'[CHAPTER WRITE] {destination} ({len(choice["entries"])} chapters; votes {choice.get("upvotes", 0)}/{choice.get("downvotes", 0)})')
            return 1, 0
    print(f'[CHAPTER MISSING] {video.name}: no usable chapter set for {imdb_id}')
    return 0, 0


def _search_tv_show(session, titles, year, timeout):
    """Resolve a TV show only on a unique normalized title (and year if supplied)."""
    seen = set()
    for title in titles:
        normalized = _normalized_title(title)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        for query in (str(title), ' '.join(str(title).split()[:3])):
            if not query.strip():
                continue
            data, error = _fetch_json(session, '/search?q=' + quote(query, safe='') + '&type=tv&limit=30', timeout)
            if error or not data:
                continue
            matches = [r for r in data.get('results', []) if isinstance(r, dict) and r.get('type') == 'tv'
                       and (not year or not r.get('year') or str(r['year'])[:4] == str(year)[:4])
                       and (_normalized_title(r.get('title')) == normalized
                            or _normalized_title(r.get('slug')) == normalized)]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                print(f'[CHAPTER SEARCH WARN] ambiguous TV show: {title}')
                return None
    return None


def _episode_identifier(show, season, episode):
    """Find an episode in the ChaptersDB show response using SxxExx."""
    pattern = re.compile(r'(?<![A-Za-z0-9])S0*' + str(int(season)) + r'E0*' + str(int(episode)) + r'(?!\d)', re.I)
    matches = [entry for entry in (show.get('episodes') or [])
               if isinstance(entry, dict) and pattern.search(str(entry.get('title') or '') + ' ' + str(entry.get('slug') or ''))]
    if len(matches) == 1:
        return matches[0].get('id') or matches[0].get('slug') or matches[0].get('tvdbId')
    return None


def download_tv_chapters(video: Path, cfg, *, episode_tvdb_id=None, series_tvdb_id=None,
                         titles=(), year=None, season=None, episode=None, translator=None,
                         session=None, timeout=(15, 30)):
    """Download single-episode chapter text, without modifying video; return (written, errors)."""
    if cfg.section('TVShow').get('Download_Chapters', False) is not True:
        return 0, 0
    video = Path(video)
    dest = chapter_path(video)
    if dest.exists() and cfg.section('Overwrite').get('Chapters', False) is not True:
        print(f'[CHAPTER SKIP] {video.name}: already exists ({dest.name})')
        return 0, 0
    embedded = has_embedded_chapters(video)
    if embedded is True:
        print(f'[CHAPTER SKIP] {video.name}: embedded chapters already present')
        return 0, 0
    if embedded is None and video.suffix.lower() != '.stub':
        print(f'[CHAPTER PROBE WARN] {video.name}: unable to inspect embedded chapters; proceeding with ChaptersDB')
    session = session or requests.Session()
    data, error = None, None
    # Prefer the episode's TVDB ID, not the show's TVDB ID.
    if episode_tvdb_id:
        data, error = _fetch_json(session, '/chapters/' + quote(str(episode_tvdb_id), safe=''), timeout)
        if error and error != 'not found or not approved':
            print(f'[CHAPTER ERROR] {video.name}: episode lookup: {error}')
            return 0, 1
    if not data or not data.get('chapters'):
        show, show_error = None, None
        if series_tvdb_id:
            show, show_error = _fetch_json(session, '/show/' + quote(str(series_tvdb_id), safe=''), timeout)
        if not show:
            if show_error and show_error != 'not found or not approved':
                print(f'[CHAPTER ERROR] {video.name}: show lookup: {show_error}')
                return 0, 1
            match = _search_tv_show(session, titles, year, timeout)
            if match:
                show, show_error = _fetch_json(session, '/show/' + quote(str(match.get('id') or match.get('slug')), safe=''), timeout)
                if show_error:
                    print(f'[CHAPTER ERROR] {video.name}: title lookup: {show_error}')
                    return 0, 1
                print(f'[CHAPTER MATCH] {video.name}: {match.get("title")} via title search')
        if show and show.get('type') == 'tv' and season is not None and episode is not None:
            identifier = _episode_identifier(show, season, episode)
            if identifier:
                data, error = _fetch_json(session, '/chapters/' + quote(str(identifier), safe=''), timeout)
                if error and error != 'not found or not approved':
                    print(f'[CHAPTER ERROR] {video.name}: chapter lookup: {error}')
                    return 0, 1
    if not data or not isinstance(data.get('chapters'), list) or not data['chapters']:
        print(f'[CHAPTER MISSING] {video.name}: no approved ChaptersDB chapters for episode')
        return 0, 0
    choices = sorted((x for x in data['chapters'] if isinstance(x, dict)),
                     key=lambda x: (int(x.get('upvotes') or 0) - int(x.get('downvotes') or 0),
                                    int(x.get('upvotes') or 0)), reverse=True)
    for choice in choices:
        content = format_chapters(choice.get('entries'), translate=translator)
        if not content:
            continue
        temp = dest.with_name(dest.name + '.tmp')
        try:
            temp.write_text(content, encoding='utf-8')
            temp.replace(dest)
        except OSError as exc:
            temp.unlink(missing_ok=True)
            print(f'[CHAPTER ERROR] {video.name}: {exc}')
            return 0, 1
        print(f'[CHAPTER WRITE] {dest} ({len(choice["entries"])} chapters)')
        return 1, 0
    print(f'[CHAPTER MISSING] {video.name}: no usable chapter set')
    return 0, 0
