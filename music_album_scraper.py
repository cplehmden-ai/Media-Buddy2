"""Test 191: album and loose-track metadata/lyrics scraper; audio never recoded."""
from __future__ import annotations
import argparse
import difflib
import unicodedata
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET
import requests
from tool_manager import get_tool
from functools import lru_cache
from musicbrainz_rate_limit import mb_get

AUDIO = {'.flac', '.mp3', '.m4a', '.aac', '.ogg', '.opus', '.wav', '.wma', '.ape', '.aiff'}
MB = 'https://musicbrainz.org/ws/2'
LRCLIB = 'https://lrclib.net/api/get'
LRCLIB_SEARCH = 'https://lrclib.net/api/search'
APP_ID = '43E0gEWeUP4r5B1Sb2iY6l0oZX2ZrfIk5Vyy2k7hdv4'
UA = 'Media-Buddy-V2 (https://github.com/cplehmden-ai/Media-Buddy2)'
UUID_RE = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$')

# Per-run bookkeeping: only directories emptied by successful organization moves.
_MOVED_SOURCE_DIRS = set()
_MOVE_DEST_DIRS = set()


def remove_empty_moved_directories(root):
    """Remove empty former music folders bottom-up, never the collection root.

    No recursive deletion is used. Existing files, sidecars and nonempty
    directories remain untouched, including on partial/failed moves.
    """
    root = Path(root).resolve()
    candidates = set()
    for original_dir in _MOVED_SOURCE_DIRS:
        current = original_dir
        while current != root and root in current.parents:
            candidates.add(current)
            current = current.parent
    # Never clean up newly created destinations or their ancestors.
    protected = set()
    for destination_dir in _MOVE_DEST_DIRS:
        current = destination_dir
        while current != root and root in current.parents:
            protected.add(current)
            current = current.parent
    for folder in sorted(candidates - protected, key=lambda path: (-len(path.parts), str(path))):
        try:
            folder.rmdir()  # Succeeds only when truly empty.
        except OSError:
            # Nonempty, already removed or locked: all must be preserved.
            continue
        print('[EMPTY DIR REMOVED]', folder, flush=True)



def clean(value):
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', str(value)).strip(' .') or 'Unknown'


@lru_cache(maxsize=1)
def _ffprobe_executable():
    """Use the same centrally configured tool resolution as the audio recoder."""
    executable = get_tool('ffprobe')
    print('[TOOL] ffprobe:', executable, flush=True)
    return executable


def _probe(path, entries, output_format='json'):
    """Do not silently treat a missing executable or a failed probe as empty tags."""
    command = [_ffprobe_executable(), '-v', 'error', '-show_entries', entries,
               '-of', output_format, str(path)]
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=20,
                                stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print('[ERROR] FFprobe execution failed:', path, exc, flush=True)
        raise RuntimeError(f'FFprobe failed for {path}: {exc}') from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or 'unknown error').strip()
        print('[ERROR] FFprobe returned', result.returncode, 'for', path, detail, flush=True)
        raise RuntimeError(f'FFprobe returned {result.returncode} for {path}: {detail}')
    return result.stdout


def tags_of(path):
    try:
        data = json.loads(_probe(path, 'format_tags') or '{}')
    except ValueError as exc:
        print('[ERROR] Invalid FFprobe tag JSON:', path, exc, flush=True)
        raise RuntimeError(f'Invalid FFprobe metadata for {path}') from exc
    tags = data.get('format', {}).get('tags', {})
    return {str(k).lower(): str(v) for k, v in tags.items()}


def tag(tags, *names):
    return next((tags[n] for n in names if tags.get(n)), '')


class Client:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': UA, 'Accept': 'application/json', 'Lrclib-App-Id': APP_ID})
        self.lyrics_cache = {}

    def get(self, url, params=None, musicbrainz=False):
        for attempt in range(3):
            try:
                response = mb_get(self.session, url, params=params, timeout=(10, 25)) if musicbrainz else self.session.get(url, params=params, timeout=(10, 25))
                if response.status_code == 404:
                    return None  # A missing LRCLIB entry is not a network error.
                if response.status_code in (429, 503):
                    if attempt == 2:
                        print('[WARN] HTTP temporarily unavailable:', response.status_code, flush=True)
                        return None
                    retry = response.headers.get('Retry-After', '')
                    time.sleep(min(15, max(1, int(retry) if retry.isdigit() else 2 ** attempt)))
                    continue
                response.raise_for_status()
                return response.json()
            except (requests.RequestException, ValueError) as exc:
                if attempt == 2:
                    print('[WARN] HTTP:', exc, flush=True)
                else:
                    time.sleep(1 + attempt)
        return None

    def recording(self, recording_id):
        return self.get(f'{MB}/recording/{recording_id}',
                        {'fmt': 'json', 'inc': 'artist-credits+releases'}, True)

    def release_group(self, group_id):
        return self.get(f'{MB}/release-group/{group_id}', {'fmt': 'json'}, True)

    def release(self, release_id):
        return self.get(f'{MB}/release/{release_id}', {'fmt': 'json', 'inc': 'recordings+artist-credits+release-groups'}, True)

    def search(self, artist, album):
        if not album:
            return []
        def quote(s):
            return '"' + s.replace('"', '') + '"'
        # Try artist-specific results first; a folder may contain a compilation,
        # a wrong artist tag, or no artist directory at all.
        queries = []
        if artist:
            queries.append(f'release:{quote(album)} AND artist:{quote(artist)}')
        queries.append(f'release:{quote(album)}')
        base_album = re.sub(r'\s*\([^)]*(?:deluxe|edition|remaster|bonus)[^)]*\)', '', album, flags=re.I).strip()
        if base_album and base_album != album:
            if artist:
                queries.append(f'release:{quote(base_album)} AND artist:{quote(artist)}')
            queries.append(f'release:{quote(base_album)}')
        found = {}
        for query in queries:
            data = self.get(f'{MB}/release/', {'fmt': 'json', 'query': query, 'limit': 25}, True) or {}
            for item in data.get('releases') or []:
                if item.get('id'):
                    found.setdefault(item['id'], item)
        # Lucene tokenization can discard punctuation-only titles (e.g. '-').
        # Browse the artist's release groups instead of relying on search hits.
        if artist and re.fullmatch(r'\s*[+\-=×÷]\s*(?:\([^)]*\))?\s*', album):
            found.update(self.punctuation_album_releases(artist, album, found))
        return list(found.values())

    def punctuation_album_releases(self, artist, album, existing):
        """Find punctuation-titled releases through artist-linked release groups.

        The release search index may ignore a title consisting of '-' or '='.
        Limit requests to this special case and preserve ordinary search results.
        """
        identity = self.artist_search_exact(artist)
        if not identity:
            print('[ALBUM SEARCH] Artist identity ambiguous; browse skipped:', artist, flush=True)
            return {}
        base = re.sub(r'\s*\([^)]*\)\s*$', '', album).strip()
        result = {}
        groups = []
        for offset in range(0, 500, 100):
            page = self.get(f'{MB}/release-group/',
                            {'fmt': 'json', 'artist': identity['id'], 'limit': 100, 'offset': offset}, True) or {}
            batch = page.get('release-groups') or []
            groups.extend(batch)
            if len(batch) < 100:
                break
        matching = [g for g in groups if str(g.get('title', '')).strip() in (album.strip(), base)]
        print('[ALBUM SEARCH] Artist browse:', len(groups), 'groups;', len(matching),
              'exact-title groups for', repr(album), flush=True)
        for group in matching[:12]:
            for offset in range(0, 200, 100):
                page = self.get(f'{MB}/release/',
                                {'fmt': 'json', 'release-group': group['id'], 'limit': 100, 'offset': offset}, True) or {}
                batch = page.get('releases') or []
                for item in batch:
                    if item.get('id') and item['id'] not in existing:
                        result[item['id']] = item
                if len(batch) < 100:
                    break
        print('[ALBUM SEARCH] Browse added', len(result), 'release candidates', flush=True)
        return result

    def search_catalog(self, catalog):
        if not catalog or len(catalog) > 64:
            return []
        query = 'catno:"' + catalog.replace('"', '') + '"'
        data = self.get(f'{MB}/release/', {'fmt': 'json', 'query': query, 'limit': 50}, True) or {}
        return [r for r in data.get('releases') or [] if r.get('id')]

    def artist_search_exact(self, name):
        """Return a single exact MusicBrainz artist match, or None.

        Name existence is evidence for filename orientation, NOT for a recording
        or release ID. Do not select among homonymous artists.
        """
        if not name or len(name.strip()) < 2:
            return None
        quoted = '"' + name.replace('"', '') + '"'
        data = self.get(f'{MB}/artist/',
                        {'fmt': 'json', 'query': f'artist:{quoted}', 'limit': 100}, True)
        if not isinstance(data, dict):
            return None
        exact = [a for a in data.get('artists', [])
                 if a.get('id') and similarity(a.get('name', ''), name) >= .98]
        unique = {a['id']: a for a in exact}
        return next(iter(unique.values())) if len(unique) == 1 else None

    def recording_search(self, artist, title):
        """MusicBrainz search with punctuation and collaborative-artist fallbacks."""
        queries = []
        def quoted(v):
            return '"' + str(v).replace('"', '') + '"'
        artists = artist_variants(artist)
        titles = title_variants(title)
        for t in titles[:3]:
            for a in artists[:4]:
                queries.append(f'recording:{quoted(t)} AND artist:{quoted(a)}')
        # Lucene's recording query alone is useful for credits indexed differently.
        queries.append(f'recording:{quoted(titles[0])}')
        for t in titles[:4]:
            queries.append(f'recording:{quoted(t)}')
        found = {}
        for query in dict.fromkeys(queries):
            data = self.get(f'{MB}/recording/',
                            {'fmt': 'json', 'query': query, 'limit': 25}, True) or {}
            for rec in data.get('recordings') or []:
                if not rec.get('id') or title_similarity(rec.get('title', ''), title) < .84:
                    continue
                credit = artist_credit(rec)
                if artist_similarity(credit, artist) < .55:
                    continue
                found.setdefault(rec['id'], rec)
            if len(found) >= 12:
                break
        return list(found.values())

    def lyrics(self, artist, title, album='', duration=None):
        """Search broadly, but accept only strong ordered-title and artist matches."""
        key = (normalized(artist), normalized(title), normalized(album), round(duration or 0))
        if key in self.lyrics_cache:
            return self.lyrics_cache[key]
        artists = artist_variants(artist)
        titles = title_variants(title)
        print('[LYRICS INPUT] artist=', repr(artist), ' title=', repr(title), sep='', flush=True)
        candidates = {}
        def collect(data):
            items = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
            for item in items:
                if isinstance(item, dict) and item.get('id') is not None:
                    candidates[item['id']] = item

        # Do not require an album match: loose tracks often carry unrelated tags.
        for t in titles[:5]:
            for a in artists[:3]:
                collect(self.get(LRCLIB, {'artist_name': a, 'track_name': t}))
        for t in titles[:5]:
            for a in artists[:5]:
                collect(self.get(LRCLIB_SEARCH, {'artist_name': a, 'track_name': t}))
        # Always search title-only if no ACCEPTABLE candidate exists, not only
        # when the preceding endpoints returned an empty candidate list.
        def acceptable(item):
            return (title_similarity(item.get('trackName', ''), title) >= .86
                    and artist_similarity(item.get('artistName', ''), artist) >= .58
                    and (item.get('syncedLyrics') or item.get('plainLyrics')))
        if not any(acceptable(item) for item in candidates.values()):
            for t in titles[:5]:
                collect(self.get(LRCLIB_SEARCH, {'q': t}))
        ranked = []
        for item in candidates.values():
            if not acceptable(item):
                continue
            ts = title_similarity(item.get('trackName', ''), title)
            ars = artist_similarity(item.get('artistName', ''), artist)
            delta = abs((item.get('duration') or 0) - duration) if duration and item.get('duration') else 0
            score = 60*ts + 35*ars + (7 if item.get('syncedLyrics') else 3) - min(12, delta / 6)
            ranked.append((score, item))
        ranked.sort(key=lambda x: x[0], reverse=True)
        best = ranked[0][1] if ranked else {}
        if best:
            print('[LYRICS MATCH]', artist, '-', title, '=>', best.get('artistName'), '-', best.get('trackName'), flush=True)
        else:
            print('[LYRICS SEARCH] No acceptable match:', artist, '-', title,
                  'candidates=', len(candidates), flush=True)
            # Diagnostic only: show the actual provider response and the exact
            # reason for rejecting each candidate. Do not relax matching here.
            diagnostics = []
            for item in candidates.values():
                tscore = title_similarity(item.get('trackName', ''), title)
                ascore = artist_similarity(item.get('artistName', ''), artist)
                has_lyrics = bool(item.get('syncedLyrics') or item.get('plainLyrics'))
                reasons = []
                if tscore < .86:
                    reasons.append('title')
                if ascore < .58:
                    reasons.append('artist')
                if not has_lyrics:
                    reasons.append('no-lyrics')
                diagnostics.append((tscore + ascore, str(item.get('id')), item,
                                    tscore, ascore, ','.join(reasons) or 'none'))
            diagnostics.sort(key=lambda entry: (-entry[0], entry[1]))
            for _, candidate_id, item, tscore, ascore, reason in diagnostics[:20]:
                print('[LYRICS CANDIDATE] id=', candidate_id,
                      ' artist=', repr(item.get('artistName', '')),
                      ' title=', repr(item.get('trackName', '')),
                      ' title_score=', round(tscore, 3),
                      ' artist_score=', round(ascore, 3),
                      ' has_lyrics=', bool(item.get('syncedLyrics') or item.get('plainLyrics')),
                      ' rejected=', reason, sep='', flush=True)
            print('[LYRICS REFERENCE] id=1769804 in_candidates=',
                  any(str(item.get('id')) == '1769804' for item in candidates.values()),
                  flush=True)
        self.lyrics_cache[key] = best
        return best


def normalized(value):
    return re.sub(r'\s+', ' ', str(value).casefold()).strip()


def strip_download_suffixes(value):
    """Remove downloader/codec annotations, but not genuine song versions.

    Only trailing, recognisable annotations are removed. The original file
    and its existing tags remain unchanged unless the user enables renaming.
    """
    value = str(value or '').strip()
    suffix = re.compile(
        r'\s*[\(\[]\s*(?:official\s+(?:music\s+)?video|official\s+audio|'
        r'lyrics?\s+video|\d{2,4}\s*kbit(?:_?\s*|\s+)'
        r'(?:opus|aac|mp3)|\d{2,4}\s*kbps(?:\s+\w+)?)\s*[\)\]]\s*$',
        re.I)
    while True:
        cleaned = suffix.sub('', value).strip()
        if cleaned == value:
            return value
        value = cleaned


def title_variants(value):
    value = str(value or '').strip()
    cleaned = strip_download_suffixes(value)
    base = re.sub(r'\s*\((?:radio edit|single version|official video|music video)[^)]*\)\s*$', '', cleaned, flags=re.I).strip()
    variants = []
    for t in (cleaned, base, value):
        variants.extend((t, re.sub(r'\b(?:und|and)\b', '&', t, flags=re.I),
                         t.replace('&', 'und'), t.replace('&', 'and')))
    return list(dict.fromkeys(v for v in variants if v))


def artist_variants(value):
    value = str(value or '').strip()
    parts = re.split(r'\s*(?:&|,|\bfeat\.?\s*|\bft\.?\s*|\bfeaturing\s*|\band\b)\s*', value, flags=re.I)
    parts = [p.strip() for p in parts if p.strip()]
    variants = [value]
    if len(parts) > 1:
        variants.extend([parts[0], parts[-1], ' & '.join(reversed(parts)),
                         ' & '.join(parts), ' feat. '.join(parts), ' feat. '.join(reversed(parts))])
    return list(dict.fromkeys(v for v in variants if v))


def _match_tokens(value):
    value = unicodedata.normalize('NFKD', str(value or '').casefold())
    value = ''.join(c for c in value if not unicodedata.combining(c))
    return re.findall(r'[a-z0-9]+', value)


def artist_tokens(value):
    return set(t for t in _match_tokens(value) if t not in {'feat', 'featuring', 'ft', 'and', 'und', 'the', 'mit'})


def artist_similarity(a, b):
    aa, bb = artist_tokens(a), artist_tokens(b)
    if not aa or not bb:
        return 0.0
    overlap = len(aa & bb) / max(len(aa), len(bb))
    first = re.split(r'\s*(?:&|,|\bfeat\.?|\bft\.?|\bfeaturing\b|\bmit\b)\s*', str(b), maxsplit=1, flags=re.I)[0]
    primary = artist_tokens(first)
    if primary and primary.issubset(aa) and len(primary) >= 2:
        overlap = max(overlap, .62)
    return overlap


def _canonical_title(value):
    value = str(value or '').casefold().strip()
    value = re.sub(r'\s*(?:\([^)]*\)|\[[^]]*\])\s*$', '', value)
    value = re.sub(r'\b(?:und|and)\b', ' & ', value)
    return tuple(t for t in re.findall(r'[a-z0-9]+|&', unicodedata.normalize('NFKD', value)))


def title_similarity(a, b):
    aa, bb = _canonical_title(a), _canonical_title(b)
    if not aa or not bb:
        return 0.0
    if aa == bb:
        return 1.0
    return difflib.SequenceMatcher(None, aa, bb).ratio()


def artist_credit(rec):
    return artist_credit_display(rec.get('artist-credit'))

def existing_mb_ids(path, tags=None):
    """Read MusicBrainz IDs from ffprobe and Mutagen (notably MP4 freeform atoms).

    MUSICBRAINZ_TRACKID is historically a recording ID, whereas
    MUSICBRAINZ_RELEASETRACKID identifies a specific release track.
    """
    tags = tags if tags is not None else tags_of(path)
    raw = {re.sub(r'[^a-z0-9]', '', str(k).casefold()): str(v)
           for k, v in tags.items()}
    try:
        from mutagen import File as MutagenFile
        audio = MutagenFile(str(path), easy=False)
        if audio is not None and audio.tags is not None:
            from mutagen.id3 import ID3
            if isinstance(audio.tags, ID3):
                for item in audio.tags.getall('TXXX'):
                    raw[re.sub(r'[^a-z0-9]', '', item.desc.casefold())] = str(item.text[0]) if item.text else ''
            else:
                for key, value in audio.tags.items():
                    norm = re.sub(r'[^a-z0-9]', '', str(key).casefold().split(':')[-1])
                    if isinstance(value, (list, tuple)):
                        value = value[0] if value else ''
                    if isinstance(value, bytes):
                        value = value.decode('utf-8', 'replace')
                    if norm:
                        raw[norm] = str(value)
    except (ImportError, OSError, ValueError, TypeError) as exc:
        print('[INFO] MusicBrainz tag fallback:', type(exc).__name__, flush=True)

    def lookup(*keys):
        for key in keys:
            val = raw.get(re.sub(r'[^a-z0-9]', '', key.casefold()), '').strip()
            if UUID_RE.fullmatch(val):
                return val.lower()
        return ''
    return {'recording': lookup('musicbrainz_recordingid', 'musicbrainz_trackid', 'musicbrainz track id'),
            'release': lookup('musicbrainz_releaseid', 'musicbrainz_albumid', 'musicbrainz album id'),
            'release_track': lookup('musicbrainz_releasetrackid', 'musicbrainz release track id')}


def _mb_credit_diagnostic(record):
    return artist_credit(record) if isinstance(record, dict) else ''


def _mb_match_diagnostic(label, candidate_title, candidate_artist, artist, title):
    ts = title_similarity(candidate_title, title)
    ac = artist_similarity(candidate_artist, artist) if candidate_artist else None
    print('[MB ID CHECK]', label, 'title=', repr(candidate_title),
          'artist=', repr(candidate_artist), 'title_score=', round(ts, 3),
          'artist_score=', round(ac, 3) if ac is not None else '-', flush=True)
    return ts >= .84 and (ac is None or ac >= .55)


def release_track_match(release, artist, title, ids):
    """Verify a release track; ID equality takes priority over display-name formatting."""
    if not isinstance(release, dict):
        print('[MB ID CHECK] Release response unavailable', flush=True)
        return None
    media = release.get('media') or []
    print('[MB ID CHECK] Release:', release.get('id', ''),
          'title=', repr(release.get('title', '')), 'media=', len(media), flush=True)
    matches = []
    for medium in media:
        for track in medium.get('tracks') or []:
            rec = track.get('recording') or {}
            rid = rec.get('id', '').lower()
            tid = track.get('id', '').lower()
            if ids['release_track'] and tid != ids['release_track']:
                continue
            if ids['recording'] and rid != ids['recording']:
                continue
            track_title = track.get('title') or rec.get('title') or ''
            credit = artist_credit(track) or artist_credit(rec) or artist_credit(release)
            if _mb_match_diagnostic('release-track', track_title, credit, artist, title):
                matches.append((track, rec))
            else:
                # An exact recording UUID on the specified release is stronger
                # than inconsistent artist credits / edition suffixes.
                if ids['recording'] and rid == ids['recording'] and title_similarity(track_title, title) >= .65:
                    print('[MB ID CHECK] Exact recording UUID on release; accepting title variant', flush=True)
                    matches.append((track, rec))
    print('[MB ID CHECK] Release matching tracks:', len(matches), flush=True)
    return matches[0] if len(matches) == 1 else None


def direct_mb_identity(client, path, artist, title, tags):
    ids = existing_mb_ids(path, tags)
    if not any(ids.values()):
        return None
    print('[MB ID] Existing: recording=', ids['recording'] or '-',
          ' release=', ids['release'] or '-', ' release_track=', ids['release_track'] or '-', sep='', flush=True)
    if ids['release']:
        release = client.release(ids['release'])
        pair = release_track_match(release, artist, title, ids)
        if pair:
            track, rec = pair
            print('[MB ID] Verified release:', ids['release'],
                  ' recording=', rec.get('id', ''), flush=True)
            return {'recording': rec, 'release': release}
        print('[MB ID] Release not verified against track; checking recording:', ids['release'], flush=True)
    if ids['recording']:
        rec = client.recording(ids['recording'])
        if isinstance(rec, dict):
            rec_title = rec.get('title', '')
            rec_artist = artist_credit(rec)
            if _mb_match_diagnostic('recording', rec_title, rec_artist, artist, title):
                print('[MB ID] Verified recording:', ids['recording'], flush=True)
                return {'recording': rec, 'release': None}
            # Artist credit can be credited to only one collaborator; a matching
            # title + recording UUID is still stronger than a fuzzy name search.
            if title_similarity(rec_title, title) >= .84:
                print('[MB ID] Verified recording by UUID and title; artist credit differs:',
                      ids['recording'], flush=True)
                return {'recording': rec, 'release': None}
            print('[MB ID CHECK] Recording rejected: title mismatch', flush=True)
        else:
            print('[MB ID CHECK] Recording response unavailable', flush=True)
    print('[MB ID] Existing IDs could not be verified; using text search:', path.name, flush=True)
    return None


def single_track_info(path):
    tags = tags_of(path)
    artist = tag(tags, 'artist', 'album_artist', 'albumartist', 'album performer', 'album_performer', 'band')
    title = tag(tags, 'title')
    # Only parse "Artist - Title" when neither field is reliably tagged.
    match = re.match(r'^(.+?)\s+[-–]\s+(.+)$', path.stem)
    if match:
        if not artist:
            artist = match.group(1).strip()
        if not title:
            title = match.group(2).strip()
    if not title:
        title = re.sub(r'^\d+[\s._-]+', '', path.stem).strip()
    if not tag(tags, 'title'):
        title = strip_download_suffixes(title)
    return artist, title, tag(tags, 'album')


def save_lyrics(client, destination, artist, title, album, opts):
    from music_lyrics_tags import process as process_lyrics
    if not opts.get('lyrics') or not artist or not title:
        process_lyrics(destination, opts)
        return
    lrc = destination.with_suffix('.lrc')
    txt = destination.with_suffix('.txt')
    if lrc.exists() or txt.exists():
        process_lyrics(destination, opts)
        return
    data = client.lyrics(artist, title, album, media_duration(destination))
    if data.get('instrumental'):
        print('[INFO] Instrumental:', artist, '-', title, flush=True)
    elif data.get('syncedLyrics'):
        lrc.write_text(data['syncedLyrics'].rstrip() + '\n', encoding='utf-8')
        print('[LYRICS] Synced:', destination.name, flush=True)
    elif data.get('plainLyrics') and opts.get('plain_lyrics', True):
        txt.write_text(data['plainLyrics'].rstrip() + '\n', encoding='utf-8')
        print('[LYRICS] Plain:', destination.name, flush=True)
    else:
        print('[INFO] No LRCLIB lyrics:', artist, '-', title, flush=True)
    process_lyrics(destination, opts)


def media_duration(path):
    try:
        return float(_probe(path, 'format=duration', 'default=nw=1:nk=1').strip())
    except ValueError:
        print('[WARN] FFprobe did not report a numeric duration:', path, flush=True)
        return None


def recording_choice(found, duration=None):
    """Choose probable original studio recording, never confuse recording with release."""
    if not found:
        return None, None
    def score(rec):
        name = (rec.get('title') or '').casefold()
        disambig = (rec.get('disambiguation') or '').casefold()
        versions = name + ' ' + disambig
        penalty = sum(12 for word in ('live', 'remix', 'acoustic', 'instrumental',
                                      'karaoke', 'demo', 'radio edit', 'extended', 'cover')
                      if word in versions)
        releases = rec.get('releases') or []
        singles = [r for r in releases if r.get('primary-type', '').casefold() == 'single'
                   or (r.get('release-group') or {}).get('primary-type', '').casefold() == 'single']
        official = [r for r in singles if r.get('status', '').casefold() == 'official']
        dates = sorted(r.get('date', '') for r in (official or singles) if r.get('date'))
        ms = rec.get('length') or 0
        duration_bonus = max(0, 12 - abs(ms / 1000 - duration) * 2) if duration and ms else 0
        return (30 - penalty + duration_bonus + (10 if official else 5 if singles else 0),
                -(int(dates[0][:4]) if dates and dates[0][:4].isdigit() else 9999))
    ranked = sorted(found, key=lambda r: (score(r), r.get('id', '')), reverse=True)
    best = ranked[0]
    releases = best.get('releases') or []
    singles = [r for r in releases if (r.get('release-group') or {}).get('primary-type', '').casefold() == 'single'
               or r.get('primary-type', '').casefold() == 'single']
    singles.sort(key=lambda r: (r.get('status', '').casefold() != 'official', r.get('date') or '9999', r.get('id', '')))
    return best, singles[0] if singles else None


def write_tags(path, fields):
    """Mutagen writes container tags without transcoding; unsupported formats are skipped."""
    try:
        from mutagen import File as MutagenFile
        from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TALB, TRCK, TPOS, TDRC, TXXX
        from mutagen.mp4 import MP4
    except ImportError:
        print('[WARN] mutagen is not installed; skipping tag writing:', path.name, flush=True)
        return False
    try:
        audio = MutagenFile(str(path), easy=False)
        if audio is None:
            raise ValueError('unsupported audio container')
        if audio.tags is None:
            audio.add_tags()
        if isinstance(audio.tags, ID3):
            standard = {'title': ('TIT2', TIT2), 'artist': ('TPE1', TPE1),
                        'albumartist': ('TPE2', TPE2), 'album': ('TALB', TALB),
                        'tracknumber': ('TRCK', TRCK), 'discnumber': ('TPOS', TPOS),
                        'date': ('TDRC', TDRC)}
            for key, val in fields.items():
                if not val:
                    continue
                if key in standard:
                    fid, cls = standard[key]
                    audio.tags.delall(fid)
                    audio.tags.add(cls(encoding=3, text=[str(val)]))
                else:
                    desc = {'musicbrainz_releaseid': 'MusicBrainz Album Id',
                            'musicbrainz_recordingid': 'MusicBrainz Track Id',
                            'musicbrainz_releasetrackid': 'MusicBrainz Release Track Id',
                            'musicbrainz_artistid': 'MusicBrainz Artist Id',
                            'musicbrainz_albumartistid': 'MusicBrainz Album Artist Id'}.get(key, key)
                    for existing in audio.tags.getall('TXXX'):
                        if existing.desc.casefold() == desc.casefold():
                            audio.tags.delall('TXXX:' + existing.desc)
                    audio.tags.add(TXXX(encoding=3, desc=desc, text=[str(val)]))
        elif isinstance(audio, MP4):
            mapping = {'title': '\xa9nam', 'artist': '\xa9ART', 'albumartist': 'aART',
                       'album': '\xa9alb', 'date': '\xa9day'}
            for key, val in fields.items():
                if not val:
                    continue
                if key in mapping:
                    audio[mapping[key]] = [str(val)]
                elif key in ('tracknumber', 'discnumber'):
                    number, _, total = str(val).partition('/')
                    audio['trkn' if key == 'tracknumber' else 'disk'] = [(int(number), int(total or 0))]
                else:
                    audio['----:com.apple.iTunes:' + key.upper()] = [str(val).encode('utf-8')]
        elif hasattr(audio.tags, '__setitem__'):
            for key, val in fields.items():
                if val:
                    audio.tags[key.upper()] = [str(val)]
        else:
            raise ValueError('unsupported tag interface')
        audio.save()
        print('[TAGS]', path.name, flush=True)
        return True
    except Exception as exc:
        print('[WARN] Tag writing failed:', path, exc, flush=True)
        return False


def move_track_group(original, destination):
    """Move a track and its same-stem sidecars; preflight all targets, rollback on failure."""
    if original == destination:
        return original
    sidecars = [p for p in original.parent.iterdir()
                if p.is_file() and p.stem == original.stem and p != original
                and p.suffix.lower() in ('.lrc', '.txt', '.cue', '.jpg', '.jpeg', '.png', '.webp')]
    moves = [(original, destination)] + [(p, destination.with_suffix(p.suffix)) for p in sidecars]
    if any(dst.exists() and src != dst for src, dst in moves) or len(set(dst for _, dst in moves)) != len(moves):
        print('[WARN] Track/sidecar target collision; keeping original:', original, flush=True)
        return original
    done = []
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        for src, dst in moves:
            src.rename(dst)
            done.append((src, dst))
        if original.parent != destination.parent:
            _MOVED_SOURCE_DIRS.add(original.parent.resolve())
            _MOVE_DEST_DIRS.add(destination.parent.resolve())
        print('[RENAME]', original.name, '->', destination.name, 'sidecars=', len(sidecars), flush=True)
        return destination
    except OSError as exc:
        print('[WARN] Rename failed, rolling back:', original, exc, flush=True)
        for src, dst in reversed(done):
            try:
                dst.rename(src)
            except OSError as rollback_error:
                print('[ERROR] Sidecar rollback failed:', dst, rollback_error, flush=True)
        return original


def release_track_mapping(release, observed):
    expected = []
    for medium in release.get('media') or []:
        for index, item in enumerate(medium.get('tracks') or [], 1):
            expected.append((int(medium.get('position') or 1),
                             track_number(item.get('position')) or track_number(item.get('number')) or index,
                             item))
    result, used = {}, set()
    for entry in observed:
        candidates = []
        for i, (disc, number, item) in enumerate(expected):
            if i in used:
                continue
            rec = item.get('recording') or {}
            sim = similarity(entry['title'], item.get('title') or rec.get('title') or '')
            if entry['number'] is not None:
                sim += .25 if entry['number'] == number else -.5
            sim += .20 if entry['disc'] == disc else -.3
            candidates.append((sim, i, disc, number, item))
        if candidates:
            best = max(candidates, key=lambda x: x[0])
            if best[0] >= .75:
                used.add(best[1])
                result[entry['path']] = (best[2], best[3], best[4])
    return result


def release_artist_id(release):
    for item in release.get('artist-credit') or []:
        if isinstance(item, dict) and item.get('artist', {}).get('id'):
            return item['artist']['id']
    return ''


def resolve_untagged_filename(path, tags, client, lookup=True):
    """Resolve both filename orders only when artist AND title tags are absent.

    Never infer identity from a filename alone when MusicBrainz cannot
    distinguish the candidates confidently. The caller may retain its
    existing filename fallback without writing speculative tags.
    """
    if tag(tags, 'artist', 'album_artist', 'albumartist', 'album performer', 'album_performer', 'band') or tag(tags, 'title'):
        return None
    _, stem = filename_track(path)
    parts = re.split(r'\s+[-–]\s+', stem, maxsplit=1)
    if len(parts) == 2:
        parts[1] = strip_download_suffixes(parts[1])
    if len(parts) != 2 or not all(parts):
        return None
    if not lookup:
        return None
    candidates = []
    duration = media_duration(path)
    for artist, title in (tuple(parts), tuple(reversed(parts))):
        matches = client.recording_search(artist, title)
        if not matches:
            continue
        best, single = recording_choice(matches, duration)
        if not best:
            continue
        title_score = title_similarity(best.get('title', ''), title)
        artist_score = artist_similarity(artist_credit(best), artist)
        if title_score < .84 or artist_score < .70:
            continue
        candidates.append((min(title_score, artist_score), artist, title, matches, best, single))
    if not candidates:
        # A cover/indie recording may not exist in MusicBrainz, although the
        # performing artist does. Compare both halves as *artist names* only.
        # Never invent recording/release IDs or treat a title as a performer.
        left, right = parts
        left_artist = client.artist_search_exact(left)
        right_artist = client.artist_search_exact(right)
        if bool(left_artist) != bool(right_artist):
            artist, title = (left, right) if left_artist else (right, left)
            print('[FILENAME ORDER] Artist verified (recording unknown):',
                  artist, '-', title, flush=True)
            return artist, title, [], None, None
        print('[INFO] Filename order unverified:', path.name, flush=True)
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    if len(candidates) > 1 and candidates[0][0] - candidates[1][0] < .12:
        print('[WARN] Ambiguous filename artist/title order:', path.name, flush=True)
        return None
    _, artist, title, matches, best, single = candidates[0]
    print('[FILENAME ORDER] Verified:', artist, '-', title, flush=True)
    return artist, title, matches, best, single


def process_single(folder, files, opts, client):
    for path in files:
        source_tags = tags_of(path)
        artist, title, album = single_track_info(path)
        resolved = resolve_untagged_filename(path, source_tags, client, opts.get('lookup', True))
        if resolved:
            artist, title = resolved[0], resolved[1]
        elif (not tag(source_tags, 'artist', 'album_artist', 'albumartist',
                      'album performer', 'album_performer', 'band')
              and not tag(source_tags, 'title')
              and len(re.split(r'\s+[-–]\s+', filename_track(path)[1], maxsplit=1)) == 2
              and opts.get('lookup', True)):
            # Neither order could be verified. Never organize an untagged track
            # under a guessed artist name. Leave it for manual post-processing.
            print('[WARN] Unverified artist/title order; using filename fallback for this track:', path.name, flush=True)
        if not artist or not title:
            print('[WARN] Insufficient single-track identity; leaving only this track unchanged:', path, flush=True)
            continue
        print('[TRACK]', artist, '-', title, flush=True)
        selected_single_release = None
        if opts.get('lookup', True):
            identity = direct_mb_identity(client, path, artist, title, source_tags)
            if identity:
                selected_single_release = identity.get('release') if isinstance(identity, dict) else None
                # Already tagged: preserve the exact edition and existing metadata.
                # A verified ID is identification, not a reason to rewrite tags.
                print('[INFO] Existing MusicBrainz identification retained:', path.name, flush=True)
            else:
                found = resolved[2] if resolved else client.recording_search(artist, title)
                chosen, single = (resolved[3], resolved[4]) if resolved else recording_choice(found, media_duration(path))
                if chosen:
                    print('[RECORDING] Selected:', chosen['id'], 'candidates=', len(found), flush=True)
                    if single:
                        selected_single_release = single
                        print('[SINGLE] Preferred release:', single.get('id', ''), flush=True)
                    if len(found) > 1:
                        print('[INFO] Multiple recordings; probable studio version selected:', path.name, flush=True)
                    if opts.get('write_tags'):
                        # Preserve all original metadata if any MusicBrainz ID exists.
                        existing = existing_mb_ids(path, source_tags)
                        fields = {} if any(existing.values()) else {'title': chosen.get('title') or title, 'artist': artist}
                        if not existing['recording']:
                            fields['musicbrainz_recordingid'] = chosen['id']
                        if single and single.get('id') and not existing['release']:
                            fields['musicbrainz_releaseid'] = single['id']
                        if fields:
                            write_tags(path, fields)
                else:
                    print('[INFO] No confident recording match:', path.name, flush=True)
                    # An unknown cover/independent recording still has a verified
                    # performer and title. Do not leave the file entirely untagged.
                    # Only add an Artist MBID when its identity is unambiguous.
                    if opts.get('write_tags'):
                        current = tags_of(path)
                        fields = {}
                        if not tag(current, 'title'):
                            fields['title'] = title
                        if not tag(current, 'artist'):
                            fields['artist'] = artist
                        artist_id = tag(current, 'musicbrainz_artistid', 'musicbrainz artist id')
                        if not artist_id:
                            matched_artist = client.artist_search_exact(artist)
                            if matched_artist and UUID_RE.fullmatch(str(matched_artist.get('id', ''))):
                                fields['musicbrainz_artistid'] = matched_artist['id']
                        if fields:
                            print('[TAG FALLBACK] No recording match; writing:',
                                  ', '.join(fields), flush=True)
                            if write_tags(path, fields):
                                print('[TAG FALLBACK] Tags written successfully:', path.name, flush=True)
                            else:
                                print('[WARN] Basic tag fallback failed:', path.name, flush=True)
        # Kodi needs an album artist even for loose tracks grouped as Singles.
        # Preserve an existing album artist; otherwise use this track's own artist.
        # Do not borrow the performer from another track or invent an album.
        if opts.get('write_tags'):
            current_tags = tags_of(path)
            if not tag(current_tags, 'album_artist', 'albumartist', 'album performer',
                       'album_performer', 'band'):
                track_performer = tag(current_tags, 'artist') or artist
                if track_performer:
                    print('[ALBUM ARTIST FALLBACK] Writing albumartist:',
                          track_performer, 'for', path.name, flush=True)
                    if not write_tags(path, {'albumartist': track_performer}):
                        print('[WARN] Album artist fallback failed:', path.name, flush=True)
        # Each loose track is independent. Never borrow a preceding track's
        # album artist, title, number or MusicBrainz release identification.
        if opts.get('rename'):
            track_num = track_number(tag(source_tags, 'tracknumber', 'track'))
            if track_num is None:
                track_num = filename_track(path)[0]
            try:
                if track_num is not None:
                    name = clean(opts.get('filename_pattern', '{track} - {title}').format(
                        track=f'{track_num:02d}', title=title, artist=artist,
                        album=album or '', disc=tag(source_tags, 'discnumber') or '1'))
                else:
                    name = clean(f'{artist} - {title}')
                path = move_track_group(path, path.with_name(name + path.suffix))
            except (KeyError, ValueError) as exc:
                print('[WARN] Invalid single-track filename pattern:', exc, flush=True)
        save_lyrics(client, path, artist, title, album, opts)
        # Resolve artist independently from recording/release. Missing recording IDs
        # must never prevent artist metadata, NFO or artwork acquisition.
        artist_credits = []
        if opts.get('artist_artwork') or opts.get('music_nfo'):
            ids = existing_mb_ids(path)
            rec = client.recording(ids['recording']) if ids['recording'] else None
            if not rec and ids['release']:
                rec = client.release(ids['release'])
            artist_credits = resolve_track_artists(
                client, artist, (rec or {}).get('artist-credit'),
                allow_lookup=opts.get('lookup', True))
            if not artist_credits:
                print('[WARN] Artist ID unavailable; artwork/NFO deferred for this track:', artist, flush=True)
        if opts.get('organize'):
            root = Path(opts['source']).resolve()
            current = tags_of(path)
            ids = existing_mb_ids(path, current)
            release_id = (selected_single_release or {}).get('id', '')
            release = client.release(release_id) if release_id and opts.get('lookup', True) else None
            if release and str(release.get('id', '')).lower() != str(release_id).lower():
                release = None
            release_name = (release or {}).get('title') or tag(current, 'album') or album or title
            # An existing year is meaningful even when the release edition is unknown.
            year_value = tag(current, 'date', 'year', 'originaldate', 'originalyear')[:4]
            if not re.fullmatch(r'(?:19|20)\d{2}', year_value):
                year_value = str((release or {}).get('date') or '')[:4]
            if not re.fullmatch(r'(?:19|20)\d{2}', year_value):
                year_value = 'Unknown'
            values = {'artist': artist, 'albumartist': artist, 'album': release_name,
                      'year': year_value, 'disc': 1}
            artist_part = safe_folder_pattern(opts.get('artist_folder_pattern', '{albumartist}'), values)
            album_part = safe_folder_pattern(opts.get('album_folder_pattern', '{year} - {album}'), values)
            if artist_part and album_part:
                artist_dir = root / existing_artist_folder(root, artist_part)
                target = (artist_dir / album_part).resolve()
                if target.is_relative_to(root) and target != path.parent:
                    result = move_track_group(path, target / path.name)
                    if result != path:
                        path = result
                        print('[SINGLE RELEASE MOVE]', result, 'release=', release_id or 'unresolved', flush=True)
                if path.parent == target and opts.get('music_nfo'):
                    from music_artwork_nfo import _xml_if_missing, _album_fields
                    if release:
                        nfo_fields = _album_fields(release, release_name, artist)
                        # Keep tagged year when it is already known.
                        if year_value != 'Unknown':
                            nfo_fields = [(k, year_value if k == 'year' else v) for k, v in nfo_fields]
                    else:
                        nfo_fields = [('title', release_name), ('artistdesc', artist),
                                      ('year', '' if year_value == 'Unknown' else year_value)]
                    _xml_if_missing(target / 'album.nfo', 'album', nfo_fields)
                    central = opts.get('artist_artwork_root', '').strip()
                    if central:
                        _xml_if_missing(Path(central) / artist_dir.name / album_part / 'album.nfo',
                                        'album', nfo_fields)
        if artist_credits:
            from music_artwork_nfo import process_single_artist
            root = Path(opts['source']).resolve()
            for artist_name, artist_id in dict(artist_credits).items():
                # Use the actual output folder for a solo performer; use distinct
                # folders for collaborators to avoid misattributing shared artwork.
                local_artist = root / existing_artist_folder(root, clean(artist_name))
                if len(artist_credits) == 1 and artist_similarity(artist_name, artist) >= .8 and path.parent != root:
                    local_artist = path.parent.parent if opts.get('organize') and path.parent.parent != root else path.parent
                try:
                    if len(artist_credits) == 1:
                        local_artist.mkdir(parents=True, exist_ok=True)
                    process_single_artist(artist_name, artist_id, local_artist,
                        opts.get('artist_artwork_root', '').strip(), opts, client.session)
                except Exception as exc:
                    print('[WARN] Single artist artwork failed:', path, exc, flush=True)
        # Additive: create a joint NFO only for verified multi-artist credits.
        if opts.get('combined_artist_nfo') and opts.get('music_nfo') and len({aid for _, aid in artist_credits}) > 1:
            try:
                from music_artwork_nfo import process_combined_artist
                # The moved single lives in its joint artist folder; the folder may
                # have retained the user's spelling/diacritics. Resolve using the
                # existing-folder helper instead of comparing cleaned raw names.
                joint_root = Path(opts['source']).resolve()
                expected_joint = joint_root / existing_artist_folder(joint_root, clean(artist))
                # Organized singles live one level below their artist directory.
                # Only mirror the joint NFO when that actual local artist folder
                # exists; do not create folders for guest performers.
                local_joint = None
                if path.parent == expected_joint:
                    local_joint = expected_joint
                elif path.parent.parent == expected_joint and expected_joint.is_dir():
                    local_joint = expected_joint
                process_combined_artist(artist, artist_credits,
                    opts.get('artist_artwork_root', '').strip(), opts, local_joint)
            except Exception as exc:
                print('[WARN] Combined artist NFO failed:', artist, exc, flush=True)
    print('[SINGLES]', folder, 'tracks=', len(files), flush=True)
    return True


def artist_credit_display(credit):
    """Preserve MusicBrainz join phrases (e.g. ' & ', ' feat. ')."""
    return ''.join(((item.get('name') or (item.get('artist') or {}).get('name') or '')
                    + (item.get('joinphrase') or '')) if isinstance(item, dict)
                   else str(item) for item in (credit or [])).strip()


def credited_artists(credit):
    """Read MusicBrainz structured credits; punctuation is not a separator."""
    result = []
    for item in credit or []:
        if not isinstance(item, dict):
            continue
        person = item.get('artist') or {}
        aid = person.get('id')
        name = person.get('name') or item.get('name')
        if aid and name and aid not in {entry[1] for entry in result}:
            result.append((name, aid))
    return result


def album_track_artists(release):
    """Collect the performers, not the compilation album artist.

    A release-track credit takes precedence over the recording credit. A band
    with an ampersand is one artist if MusicBrainz credits it as one entity.
    """
    artists = {}
    for medium in (release or {}).get('media') or []:
        for track in medium.get('tracks') or []:
            credit = track.get('artist-credit') or (track.get('recording') or {}).get('artist-credit')
            for name, aid in credited_artists(credit):
                artists.setdefault(aid, name)
    return [(name, aid) for aid, name in artists.items()]


def resolve_track_artists(client, artist, credit=None, allow_lookup=True):
    """Preserve a known complete artist identity before considering splitting.

    A structured multi-artist credit is authoritative. With no usable credit,
    try the complete name first, then conservative collaborator splitting.
    """
    known = credited_artists(credit)
    if len(known) > 1:
        return known
    if known and artist_similarity(known[0][0], artist) >= .85:
        return known
    if allow_lookup:
        full = client.artist_search_exact(artist)
        if full:
            return [(full.get('name') or artist, full['id'])]
    if known:
        # Existing recording credit is better evidence than punctuation alone.
        return known
    parts = [v.strip() for v in re.split(
        r'\s+(?:&|feat\.?|ft\.?|featuring|with|x)\s+', artist,
        flags=re.I) if v.strip()]
    if len(parts) < 2 or not allow_lookup:
        return []
    verified = []
    for part in parts:
        match = client.artist_search_exact(part)
        if not match:
            print('[WARN] Collaborator unresolved:', part, flush=True)
            return []
        verified.append((match.get('name') or part, match['id']))
    return list({aid: (name, aid) for name, aid in verified}.values())


def tracks_from_release(release):
    result = []
    for medium in release.get('media') or []:
        for item in medium.get('tracks') or []:
            recording = item.get('recording') or {}
            credit = item.get('artist-credit') or recording.get('artist-credit') or []
            artist = artist_credit_display(credit)
            result.append({'title': item.get('title') or recording.get('title') or '',
                           'artist': artist, 'number': item.get('number') or '',
                           'length': (item.get('length') or recording.get('length') or 0) / 1000})
    return result


def normalize_name(value):
    value = str(value or '').casefold().strip()
    # MusicBrainz uses U+2212 for Ed Sheeran's minus album; local tags often use ASCII '-'.
    value = value.replace('−', '-').replace('–', '-').replace('—', '-')
    # Preserve mathematical album titles as semantic tokens, not empty strings.
    value = re.sub(r'(?<!\w)-(?=\s*(?:\(|$))', ' minus ', value)
    value = re.sub(r'(?<!\w)\+(?=\s*(?:\(|$))', ' plus ', value)
    value = re.sub(r'(?<!\w)=(?=\s*(?:\(|$))', ' equals ', value)
    value = value.replace('×', ' multiply ').replace('÷', ' divide ')
    value = unicodedata.normalize('NFKD', value)
    value = ''.join(c for c in value if not unicodedata.combining(c))
    value = re.sub(r'\([^)]*(?:deluxe|remaster|edition|version|mix|bonus)[^)]*\)', '', value)
    return re.sub(r'[^a-z0-9]+', ' ', value).strip()


def similarity(a, b):
    a, b = normalize_name(a), normalize_name(b)
    return difflib.SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def track_number(value):
    match = re.match(r'^\s*(\d{1,3})(?:\s*/\s*\d+)?\s*$', str(value or ''))
    return int(match.group(1)) if match else None


def filename_track(path):
    """Common numbered audio names, including '(01) Title' and '01. Title'."""
    match = re.match(r'^\s*(?:\((\d{1,3})\)|\[(\d{1,3})\]|(\d{1,3}))\s*[.\s_-]*\s*(.+)$', path.stem)
    if match:
        return int(next(v for v in match.groups()[:3] if v is not None)), match.group(4).strip()
    return None, path.stem


def is_disc_folder(folder):
    return bool(re.match(r'^(?:album|bonus(?:\s*(?:cd|disc|disk))?|(?:cd|disc|disk)\s*\d+|disk\s*\d+|side\s*[a-d])$', folder.name.strip(), re.I))


def disc_order(folder):
    name = folder.name.casefold()
    if name == 'album':
        return 1
    match = re.search(r'\d+', name)
    if match:
        return int(match.group())
    return 2 if 'bonus' in name else 1


def album_folder_name(folder):
    """Infer artist, album and optional year from common downloaded album folders."""
    name = re.sub(r'\s+(?:FLAC|MP3|AAC|ALAC|WAV|LOSSLESS)\s*$', '', folder.name, flags=re.I)
    name = re.sub(r'\s*\((?:19|20)\d{2}\)\s*$', '', name)
    name = re.sub(r'^\s*(?:19|20)\d{2}\s*[-–]\s*', '', name)
    name = re.sub(r'\s*\[\s*\d{2,3}\s*[-/]\s*\d{2,3}\s*\]\s*$', '', name)
    parts = re.split(r'\s+[-–]\s+', name, maxsplit=1)
    return (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else ('', name.strip())


def observed_tracks(files):
    tracks = []
    for path in files:
        t = tags_of(path)
        stem = path.stem
        parsed_number, parsed_title = filename_track(path)
        number = track_number(tag(t, 'track', 'tracknumber'))
        if number is None:
            number = parsed_number
        title = tag(t, 'title') or parsed_title
        tracks.append({'path': path, 'title': title, 'number': number,
                       'disc': track_number(tag(t, 'disc', 'discnumber')) or (disc_order(path.parent) if is_disc_folder(path.parent) else 1),
                       'artist': tag(t, 'artist')})
    return tracks


def release_score(release, observed, album, artist):
    expected = []
    for medium in release.get('media') or []:
        disc = medium.get('position') or 1
        for i, item in enumerate(medium.get('tracks') or [], 1):
            rec = item.get('recording') or {}
            expected.append({'title': item.get('title') or rec.get('title') or '',
                             'number': track_number(item.get('position')) or
                                       track_number(item.get('number')) or i,
                             'disc': disc})
    if not expected:
        return -999.0, 0, 0
    used = set()
    matched = 0
    total = 0.0
    for track in observed:
        best = (-1.0, None)
        for idx, candidate in enumerate(expected):
            if idx in used:
                continue
            title_score = similarity(track['title'], candidate['title'])
            # A numbered match must respect the number; allow disc 1 when
            # no disc tags exist, but never swap numbered tracks freely.
            if track['number'] is not None:
                if track['number'] != candidate['number']:
                    title_score -= .65
                elif title_score >= .65:
                    title_score += .22
            if track['disc'] != candidate['disc']:
                title_score -= .20
            if title_score > best[0]:
                best = (title_score, idx)
        if best[1] is not None and best[0] >= .62:
            used.add(best[1])
            matched += 1
            total += best[0]
    coverage = matched / max(1, len(observed))
    title_score = similarity(album, release.get('title', ''))
    credit = ''.join(c.get('name', '') if isinstance(c, dict) else str(c)
                     for c in release.get('artist-credit') or [])
    artist_score = similarity(artist, credit) if artist else .5
    # Missing files are allowed: the existing tracks matter more than the
    # release's total count. Count differences only break close ties.
    # Album and artist are valid independent evidence even for untagged
    # Track01/Track02 files. Track coverage differentiates editions, but may
    # not suppress an otherwise plausible album-level identification.
    score = 50 * title_score + 20 * artist_score + \
            20 * coverage + 10 * min(1, total / max(1, len(observed))) - \
            min(8, abs(len(expected) - len(observed)) * .7)
    # Identical track lists establish the album even when the edition suffix
    # (e.g. '(Deluxe)') is omitted in MusicBrainz. Format/country are irrelevant.
    # Artist and normalized album identity remain mandatory safeguards.
    if (observed and matched == len(observed) == len(expected)
            and title_score >= .95 and artist_score >= .80):
        score = max(score, 95.0)
    return score, matched, len(expected)


def album_info(folder, files):
    tagsets = [tags_of(f) for f in files]
    def most_common(values):
        values = [x.strip() for x in values if x and x.strip()]
        return max(set(values), key=lambda x: (values.count(x), len(x))) if values else ''
    artist = most_common([tag(t, 'album_artist', 'albumartist', 'album performer', 'album_performer', 'band') for t in tagsets])
    album = most_common([tag(t, 'album') for t in tagsets])
    if not album:
        album = re.sub(r'^\d{4}\s*[-–]\s*', '', folder.name)
        album = re.sub(r'\s*\[\s*\d{2,3}\s*[-/]\s*\d{2,3}\s*\]\s*$', '', album)
    if not artist:
        artist = most_common([tag(t, 'artist', 'performer', 'band') for t in tagsets])
    inferred_artist, inferred_album = album_folder_name(folder)
    if not any(tag(t, 'album') for t in tagsets):
        album = inferred_album
    if not artist:
        artist = most_common([tag(t, 'artist', 'performer', 'band') for t in tagsets])
    if not artist and inferred_artist and not re.match(r'^(?:19|20)\d{2}$', inferred_artist):
        artist = inferred_artist
    # The selected root is never an artist. Never infer artists from parent paths.
    return artist, album



def safe_folder_pattern(pattern, values):
    """Render one configurable folder component; never allow path traversal."""
    try:
        rendered = str(pattern).format(**values)
    except (KeyError, ValueError, IndexError) as exc:
        print('[WARN] Invalid folder naming pattern:', exc, flush=True)
        return None
    if not rendered.strip() or rendered.strip() in ('.', '..'):
        return None
    return clean(rendered)


def album_disc_subfolder(disc, release, opts, source_hint=""):
    """Disc policy: prefer explicit MB titles; infer bonus for a deluxe release
    only when disc 2 is materially shorter than the main disc.
    """
    if not opts.get('disc_folders') or not disc:
        return None
    media = (release or {}).get('media') or []
    if len(media) <= 1:
        return None
    mode = opts.get('disc_mode', 'automatic')
    if mode == 'flat':
        return None
    if mode == 'separate':
        return safe_folder_pattern(opts.get('disc_folder_pattern', 'CD {disc}'), {'disc': disc})
    bonus_discs = set()
    for medium in media:
        if re.search(r'\bbonus\b', medium.get('title') or '', re.I):
            bonus_discs.add(int(medium.get('position') or 1))
    if not bonus_discs and mode in ('automatic', 'bonus') and len(media) == 2:
        title = str((release or {}).get('title') or '') + ' ' + source_hint
        first, second = (len(m.get('tracks') or []) for m in media)
        explicit_bonus_edition = bool(re.search(r'\b(deluxe|expanded|bonus)\b', title, re.I))
        # MusicBrainz does not always label a bonus medium. A 12+5 release
        # such as One Deep River must still place its 12 main tracks at the
        # album root and the five extras under Bonus CD. Do not apply this
        # heuristic to similarly sized regular multi-disc albums.
        distinct_bonus_medium = first >= 10 and 0 < second <= first // 2
        if (explicit_bonus_edition and first >= 8 and 0 < second <= first // 2) or distinct_bonus_medium:
            bonus_discs.add(int(media[1].get('position') or 2))
            print('[DISC] Short supplementary second medium: treating as Bonus CD '
                  f'({first}+{second} tracks)', flush=True)
    if int(disc) in bonus_discs:
        return clean(opts.get('bonus_disc_folder', 'Bonus CD'))
    if bonus_discs and int(disc) == 1:
        return None
    if mode == 'bonus' and int(disc) == 1:
        return None
    return safe_folder_pattern(opts.get('disc_folder_pattern', 'CD {disc}'), {'disc': disc})


def artist_folder_identity(value):
    """Compare artist folder spelling variants, not arbitrary different artists.

    Unicode hyphens and whitespace between name parts are equivalent here.
    The original spelling is preserved for display and metadata.
    """
    value = unicodedata.normalize('NFKD', str(value or '')).casefold()
    value = ''.join(char for char in value if not unicodedata.combining(char))
    value = re.sub(r'[\u2010-\u2015\u2212\u00ad\u2011\-]', ' ', value)
    return ' '.join(value.split())


def existing_artist_folder(root, proposed):
    """Reuse an existing folder for the same spelling-normalized artist."""
    key = artist_folder_identity(proposed)
    if not key or not root.is_dir():
        return proposed
    candidates = sorted((p.name for p in root.iterdir()
                         if p.is_dir() and artist_folder_identity(p.name) == key),
                        key=lambda n: (n != proposed, n.casefold()))
    if candidates:
        chosen = candidates[0]
        if chosen != proposed:
            print('[ARTIST FOLDER] Reusing existing:', repr(chosen),
                  'instead of:', repr(proposed), flush=True)
        return chosen
    return proposed


def album_target_directory(root, source, album, artist, release, opts, disc):
    """Resolve a target under the selected root, without touching central artwork."""
    year = str((release or {}).get('_preferred_date') or (release or {}).get('date') or opts.get('_local_year') or '')[:4]
    values = {'album': album, 'albumartist': artist, 'artist': artist, 'year': year or 'Unknown', 'disc': disc or 1}
    target = root if opts.get('organize') else source
    if opts.get('organize'):
        for key, default in (('artist_folder_pattern', '{albumartist}'),
                             ('album_folder_pattern', '{year} - {album}')):
            part = safe_folder_pattern(opts.get(key, default), values)
            if part is None:
                return None
            if key == 'artist_folder_pattern':
                part = existing_artist_folder(root, part)
            target = target / part
    subfolder = album_disc_subfolder(disc, release, opts, source.name)
    if subfolder:
        target = target / subfolder
    target = target.resolve()
    if not target.is_relative_to(root.resolve()):
        print('[WARN] Folder target outside selected root:', target, flush=True)
        return None
    return target


def edition_suffix_from_source(source, album):
    """Keep an explicit edition label, but not technical quality suffixes."""
    hint = album_folder_name(source)[1]
    editions = re.findall(r'\(([^)]*(?:remaster(?:ed)?|deluxe|expanded|anniversary|special edition|bonus edition)[^)]*)\)', hint, re.I)
    if editions and not re.search(r'\((?:' + '|'.join(re.escape(e) for e in editions) + r')\)', album, re.I):
        return ' (' + editions[0].strip() + ')'
    return ''


def destination_conflict(moves):
    """Preflight audio and all same-stem sidecars, without changing files."""
    seen = set()
    for src, dst in moves:
        companions = [src] + [p for p in src.parent.iterdir()
                              if p.is_file() and p.stem == src.stem and p != src
                              and p.suffix.lower() in ('.lrc', '.txt', '.cue', '.jpg', '.jpeg', '.png', '.webp')]
        for item in companions:
            target = dst if item == src else dst.with_suffix(item.suffix)
            if target in seen or (target.exists() and target != item):
                return True
            seen.add(target)
    return False


def choose_album_destination(moves, source, root, album_root):
    """Resolve a whole-album collision with a neutral numbered album directory."""
    if not destination_conflict(moves):
        return moves, album_root
    # Never resolve a collision by merging tracks into an existing album.
    for index in range(2, 10000):
        alternate = album_root.with_name(f'{album_root.name} ({index})')
        if alternate.exists():
            continue
        revised = [(src, alternate / dst.relative_to(album_root)) for src, dst in moves]
        if not destination_conflict(revised):
            print('[WARN] Album destination collision; using:', alternate, 'source:', source, flush=True)
            return revised, alternate
    print('[ERROR] No free album destination after 9999 attempts:', source, flush=True)
    return None, album_root


def preserve_local_album_assets(source, target):
    """Copy local album artwork/NFO to the organized album without overwrites.

    Deliberately independent of online artwork switches. Never modify the source.
    """
    import shutil
    import xml.etree.ElementTree as ET

    if source.resolve() == target.resolve():
        return
    if not target.is_dir():
        print('[WARN] Local artwork deferred: album target not present:', target, flush=True)
        return
    # Known album-wide artwork only; do not mistake per-track artwork for a cover.
    artwork_names = {
        'folder.jpg': 'folder.jpg', 'folder.jpeg': 'folder.jpeg',
        'cover.jpg': 'folder.jpg', 'cover.jpeg': 'folder.jpeg',
        'front.jpg': 'folder.jpg', 'front.jpeg': 'folder.jpeg',
        'folder.png': 'folder.png', 'cover.png': 'folder.png',
        'front.png': 'folder.png', 'disc.png': 'disc.png',
        'cdart.png': 'cdart.png', 'back.jpg': 'back.jpg',
        'back.png': 'back.png', 'discart.png': 'discart.png',
        'booklet.jpg': 'booklet.jpg', 'booklet.png': 'booklet.png',
    }
    # Exact canonical names win over aliases regardless of directory enumeration order.
    for name in sorted(artwork_names, key=lambda n: (n != artwork_names[n], n)):
        candidates = [f for f in source.iterdir() if f.is_file() and f.name.casefold() == name]
        for item in candidates:
            dest = target / artwork_names[name]
            if dest.exists():
                print('[INFO] Existing album artwork preserved:', dest, flush=True)
                break
            try:
                shutil.copy2(item, dest)
                print('[ARTWORK LOCAL]', item, '->', dest, flush=True)
            except OSError as exc:
                print('[WARN] Could not copy album artwork:', item, exc, flush=True)
            break
    nfo = next((f for f in source.iterdir() if f.is_file() and f.name.casefold() == 'album.nfo'), None)
    if nfo and not (target / 'album.nfo').exists():
        try:
            tree = ET.parse(nfo)
            root = tree.getroot()
            if root.tag.lower() != 'album':
                print('[WARN] Local album.nfo has unexpected XML root; not copying:', nfo, flush=True)
                return
            # NFO describes content, not file-system state or codec characteristics.
            forbidden = {'path', 'filenameandpath', 'file', 'fileinfo', 'streamdetails',
                         'codec', 'bitrate', 'samplerate', 'sample_rate', 'channels',
                         'durationinseconds'}
            for parent in root.iter():
                for child in list(parent):
                    if child.tag.lower() in forbidden:
                        parent.remove(child)
            tree.write(target / 'album.nfo', encoding='utf-8', xml_declaration=True)
            print('[NFO LOCAL] Sanitized album.nfo copied:', nfo, '->', target / 'album.nfo', flush=True)
        except (OSError, ET.ParseError) as exc:
            print('[WARN] Could not copy album.nfo:', nfo, exc, flush=True)


def normalized_disc_album(value):
    """Ignore only explicit disc qualifiers, not arbitrary edition names."""
    value = str(value or '').strip()
    value = re.sub(r'\s*\((?:bonus\s*(?:cd|disc|disk)|(?:cd|disc|disk)\s*\d+)\)\s*$', '', value, flags=re.I)
    return ' '.join(value.casefold().split())


def local_album_year(files):
    dates = [tag(tags_of(f), 'date', 'year', 'originaldate', 'originalyear') for f in files]
    years = [m.group(0) for v in dates if (m := re.match(r'(?:19|20)\d{2}', v))]
    return max(set(years), key=years.count) if years else ''


def local_catalog_hints(source, album, files):
    found = []
    for value in [source.name, album] + [tag(tags_of(f), 'catalognumber', 'catalog_number', 'catalog') for f in files[:3]]:
        found.extend(re.findall(r'\[([A-Z]{2,}[A-Z0-9 -]*\d[A-Z0-9 -]*)\]', value or '', re.I))
        if value and re.fullmatch(r'[A-Z]{2,}[A-Z0-9 -]*\d[A-Z0-9 -]*', value.strip(), re.I):
            found.append(value.strip())
    return list(dict.fromkeys(found))


def process(source, opts, client, files=None):
    if files is None:
        files = sorted(p for p in source.iterdir() if p.is_file() and p.suffix.lower() in AUDIO)
    if not files:
        return False
    artist, album = album_info(source, files)
    # Capture the authoritative album tag BEFORE MusicBrainz can replace the
    # working album title. The central artwork tree must follow this tag.
    central_tagged_album = next((tag(tags_of(f), 'album').strip()
                                 for f in files if tag(tags_of(f), 'album').strip()), '')
    folder_artist = artist  # original ALBUMARTIST spelling wins for new folders
    if source.resolve() == Path(opts['source']).resolve() and not artist:
        print('[WARN] No tagged artist at music root; never using root folder name as artist', flush=True)
    # The selected input root is always a collection, never an album.
    # Even when all files are numbered or have album tags, its tracks must
    # be identified individually (and may belong to unrelated releases).
    if source.resolve() == Path(opts['source']).resolve():
        print('[INFO] Collection root: processing independent tracks:', source, flush=True)
        return process_single(source, files, opts, client)
    markers = [p for p in source.iterdir() if p.is_file() and p.suffix.lower() == '.txt' and UUID_RE.fullmatch(p.stem)]
    # In subfolders, a single album tag is not proof that every track belongs
    # to the same album. A mixed folder must never acquire a common artist.
    tagged_albums = [tag(tags_of(f), 'album').strip().casefold() for f in files]
    known_albums = {a for a in tagged_albums if a}
    tagged_release_ids = {ids['release'] for f in files
                          if (ids := existing_mb_ids(f))['release']}
    # Explicit disc folders and a UUID marker are strong structural evidence.
    # An album title like "One Deep River (Bonus CD)" is not a second album.
    explicit_disc_group = len({f.parent for f in files}) > 1 and all(
        f.parent == source or is_disc_folder(f.parent) for f in files)
    normalized_albums = {normalized_disc_album(a) for a in known_albums}
    inconsistent = len(normalized_albums) > 1 and not (explicit_disc_group and markers)
    unanimous_album = bool(known_albums) and len(normalized_albums) == 1 and all(tagged_albums)
    looks_like_album = bool(markers) or (not inconsistent and (
        explicit_disc_group or unanimous_album or (
        not known_albums and len(files) > 1 and source.parent.resolve() != Path(opts['source']).resolve()
        and all(filename_track(f)[0] is not None for f in files))))
    if explicit_disc_group:
        print('[ALBUM STRUCTURE] Disc folders:', sorted({f.parent.name for f in files}),
              'album variants:', sorted(known_albums), 'release markers:', len(markers), flush=True)
    # A lone file in an artist directory is not proof of an album, even when
    # historical tags contain an album or release UUID. Preserve genuine
    # artist/album directories (two or more levels beneath the collection root).
    depth = len(source.resolve().relative_to(Path(opts['source']).resolve()).parts)
    if len(files) == 1 and depth == 1 and not markers:
        print('[INFO] Single-track artist folder; album identification not required:', source, flush=True)
        return process_single(source, files, opts, client)
    # A deliberate Artist/Album hierarchy is stronger evidence than incomplete
    # or inconsistent tags. Keep local cover albums together even when MB has
    # no recording/release for their performances.
    if depth >= 2 and not looks_like_album:
        looks_like_album = True
        print('[ALBUM STRUCTURE] Preserving explicit artist/album directory:', source, flush=True)
    if not looks_like_album:
        print('[INFO] Mixed/loose folder: processing independent tracks:', source, flush=True)
        return process_single(source, files, opts, client)
    # Reuse a unanimous, already-tagged release ID before fuzzy searches.
    # This is especially important for finished albums and distinct editions.
    tagged_release_ids = {ids['release'] for f in files
                          if (ids := existing_mb_ids(f))['release']}
    tagged_release = None
    # A deliberate marker in the album root takes precedence over individual
    # track tags, which may refer to a single-CD edition.
    release = None
    if len(markers) == 1:
        marker_id = markers[0].stem.lower()
        print('[RELEASE MARKER] Querying explicit album ID:', marker_id, flush=True)
        release = client.release(marker_id)
        if not release or str(release.get('id', '')).lower() != marker_id:
            print('[WARN] Release marker unavailable; retaining local album identity:', source, flush=True)
            release = None
    elif len(tagged_release_ids) == 1:
        release_id = next(iter(tagged_release_ids))
        tagged_release = client.release(release_id)
        if tagged_release and tagged_release.get('id', '').lower() == release_id:
            release = tagged_release
    elif len(tagged_release_ids) > 1:
        print('[WARN] Multiple track-level release IDs; preserving album grouping:', source, flush=True)
    if release and markers and tagged_release_ids and release.get('id', '').lower() not in tagged_release_ids:
        print('[RELEASE MARKER] Album marker overrides track-level edition IDs:', source, flush=True)
    if release:
        print('[MUSIC ART] Reusing tagged MusicBrainz release:', release['id'], flush=True)
    elif len(markers) > 1:
        print('[WARN] Multiple release markers; evaluating candidates:', source, flush=True)
        candidates = [{'id': m.stem} for m in markers]
        scored = []
        observed = observed_tracks(files)
        for candidate in candidates:
            detail = client.release(candidate['id'])
            if detail:
                score, matched, expected = release_score(detail, observed, album, artist)
                scored.append((score, matched, expected, detail))
        if scored:
            scored.sort(key=lambda x: (-x[0], -x[1], x[3].get('id', '')))
            score, matched, expected, release = scored[0]
            print(f'[WARN] Multiple markers: selected best match score={score:.1f}; tracks={matched}/{len(files)}', flush=True)
    elif opts.get('lookup', True):
        candidates = []
        for catalog in local_catalog_hints(source, album, files):
            matches = client.search_catalog(catalog)
            print('[CATALOG SEARCH]', catalog, 'candidates=', len(matches), flush=True)
            candidates.extend(matches)
        candidates.extend(client.search(artist, album))
        candidates = list({c['id']: c for c in candidates if c.get('id')}.values())
        observed = observed_tracks(files)
        scored = []
        for candidate in candidates:
            detail = client.release(candidate['id'])
            if detail:
                score, matched, expected = release_score(detail, observed, album, artist)
                scored.append((score, matched, expected, detail))
        if scored:
            scored.sort(key=lambda x: (-x[0], -x[1], x[3].get('id', '')))
            score, matched, expected, release = scored[0]
            for rank, (candidate_score, candidate_matched, candidate_expected, candidate) in enumerate(scored[:8], 1):
                print(f'[MATCH CANDIDATE {rank}] {candidate.get("title", "?")!r} '
                      f'id={candidate.get("id", "?")} score={candidate_score:.1f} '
                      f'tracks={candidate_matched}/{len(files)} release_tracks={candidate_expected}', flush=True)
            print(f'[MATCH] {len(scored)} releases; best={release.get("title", "?")} '
                  f'score={score:.1f}; tracks={matched}/{len(files)}; release_tracks={expected}', flush=True)
            if matched < len(files) or score < 85 or (len(scored) > 1 and score - scored[1][0] < 5):
                print('[WARN] Album identification uncertain; selected most probable release for later review:',
                      source, flush=True)
            if matched == 0:
                print('[WARN] No track title/number confirmed; release selected by album/artist only:',
                      source, flush=True)
        else:
            print('[WARN] No MusicBrainz release candidates:', source, flush=True)
    if release and depth >= 2 and 'scored' in locals() and scored and scored[0][0] < 60:
        print('[ALBUM LOCAL] Rejecting low album score:', f'{scored[0][0]:.1f}', '(minimum 60)', flush=True)
        release = None
    if release and 'scored' in locals() and scored and scored[0][0] < 60:
        print('[WARN] Rejecting low-score release; preserving original tags:', source, flush=True)
        release = None
    if release:
        # Keep the concrete release ID but prefer the release-group's original year.
        if opts.get('year_mode', 'original') == 'original':
            group = release.get('release-group') or {}
            if group.get('id'):
                detail = client.release_group(group['id']) or {}
                first_date = detail.get('first-release-date') or group.get('first-release-date')
                if first_date and re.match(r'^\d{4}', first_date):
                    release['_preferred_date'] = first_date
                    print('[YEAR] Original release:', first_date, 'specific release:', release.get('date', ''), flush=True)
        # Existing album tags are authoritative, including edition suffixes.
        album = central_tagged_album or album or release.get('title') or ''
        credit = release.get('artist-credit') or []
        artist = artist_credit_display(credit) or artist
    compilation_names = {str(name).casefold().strip() for name in opts.get(
        'compilation_artists', ['Various Artists', 'Various Artist', 'Various'])}
    is_compilation = artist.casefold().strip() in compilation_names or (
        release is not None and artist_credit_display(release.get('artist-credit')).casefold().strip() in compilation_names)
    # Metadata scraper operates directly in the selected existing album directory.
    # It must not copy music into another tree or rewrite audio contents.
    output = source
    if release and not markers:
        # Do not persist a guessed exact edition as a verified release marker.
        if 'scored' in locals() and scored and scored[0][1] == len(files) and scored[0][0] >= 88 and \
                (len(scored) == 1 or scored[0][0] - scored[1][0] >= 5):
            (output / (release['id'] + '.txt')).touch(exist_ok=True)
        else:
            print('[INFO] Release marker deferred: edition not sufficiently verified', flush=True)
    # Include newly generated release markers in later relocation.
    markers = [p for p in source.iterdir() if p.is_file() and p.suffix.lower() == '.txt' and UUID_RE.fullmatch(p.stem)]
    observed = observed_tracks(files)
    mapping = release_track_mapping(release, observed) if release else {}
    # A valid explicit release marker identifies the album, not an exact local
    # inventory. Missing, duplicated or bonus tracks must never discard its
    # album metadata. Only confidently matched tracks receive release-specific
    # track identifiers; unmatched files keep their existing track/disc tags.
    if release and markers and len(mapping) < len(files):
        print('[RELEASE MARKER] Keeping album release despite unmatched local tracks:',
              len(mapping), '/', len(files), 'matched; unmatched tracks preserved', flush=True)
    media_counts = {int(m.get('position') or 1): len(m.get('tracks') or [])
                    for m in (release or {}).get('media') or []}
    pending_moves = []
    root = Path(opts['source']).resolve()
    # Existing artist/album trees are valid targets for metadata enrichment.
    # This is a structural check, not a license to move or overwrite tracks.
    already_organized = source.resolve() != root and len(source.resolve().relative_to(root).parts) >= 2
    existing_album_root = source.parent if is_disc_folder(source) else source
    if already_organized:
        print('[MUSIC ART] Existing organized album:', existing_album_root, flush=True)
    # Preserve an explicit source edition only in the directory name, never in
    # MusicBrainz metadata or FLAC tags. Technical [24-96] labels are excluded.
    local_year = local_album_year(files)
    if not local_year:
        year_match = re.match(r'^((?:19|20)\d{2})\s*[-–]\s*', existing_album_root.name)
        if year_match:
            local_year = year_match.group(1)
    local_album_fallback = not release and bool(artist) and looks_like_album
    if local_album_fallback:
        print('[ALBUM LOCAL] Using tags/structure without MusicBrainz release:',
              'artist=', artist, 'album=', album, 'year=', local_year, flush=True)
    target_opts = dict(opts)
    target_opts['_local_year'] = local_year
    folder_album = album + edition_suffix_from_source(source, album)
    for idx, original in enumerate(files, 1):
        t = tags_of(original)
        matched = mapping.get(original)
        disc, number, mbtrack = matched if matched else (None, None, None)
        rec = (mbtrack or {}).get('recording') or {}
        title = (mbtrack or {}).get('title') or rec.get('title') or tag(t, 'title') or filename_track(original)[1]
        song_artist = tag(t, 'artist') or artist
        if already_organized and not release and opts.get('write_tags'):
            local_fields = {}
            if not tag(t, 'artist'): local_fields['artist'] = artist
            if not tag(t, 'album'): local_fields['album'] = album
            if not tag(t, 'title'): local_fields['title'] = title
            if not tag(t, 'tracknumber', 'track') and filename_track(original)[0] is not None:
                local_fields['tracknumber'] = str(filename_track(original)[0])
            if not tag(t, 'date', 'year') and local_year: local_fields['date'] = local_year
            if local_fields:
                print('[ALBUM LOCAL TAGS] Filling missing values from album context:', original.name, local_fields, flush=True)
                write_tags(original, local_fields)
        fields = {}
        if release:
            fields = {'title': title, 'artist': song_artist, 'albumartist': artist,
                      'album': album, 'date': str(release.get('_preferred_date') or release.get('date') or '')[:4],
                      'musicbrainz_albumartistid': release_artist_id(release)}
            if matched:
                fields['musicbrainz_releaseid'] = release.get('id', '')
            if matched:
                fields.update({'tracknumber': f'{number}/{media_counts.get(disc, number)}',
                               'discnumber': f'{disc}/{len(media_counts)}',
                               'musicbrainz_recordingid': rec.get('id', ''),
                               'musicbrainz_releasetrackid': mbtrack.get('id', '')})
            else:
                print('[WARN] Track not matched; preserving existing track/disc tags:', original, flush=True)
        if opts.get('write_tags') and fields:
            # Never replace an existing album performer just because a release
            # was identified; it may represent a deliberate compilation credit.
            existing_albumartist = tag(t, 'album_artist', 'albumartist',
                                       'album performer', 'album_performer', 'band')
            if existing_albumartist:
                fields['albumartist'] = existing_albumartist
            write_tags(original, fields)
        if opts.get('write_tags'):
            updated_tags = tags_of(original)
            if not tag(updated_tags, 'album_artist', 'albumartist',
                       'album performer', 'album_performer', 'band'):
                performer = tag(updated_tags, 'artist') or song_artist
                if performer:
                    print('[ALBUM ARTIST FALLBACK] Writing albumartist:',
                          performer, 'for', original.name, flush=True)
                    if not write_tags(original, {'albumartist': performer}):
                        print('[WARN] Album artist fallback failed:', original.name, flush=True)
        destination = original
        if opts.get('rename') and title:
            parsed_number, _ = filename_track(original)
            track_num = number or track_number(tag(t, 'track', 'tracknumber')) or parsed_number or idx
            try:
                destname = clean(opts.get(
                    'compilation_filename_pattern' if is_compilation else 'filename_pattern',
                    '{track} - {artist} - {title}' if is_compilation else '{track} - {title}').format(
                    track=f'{track_num:02d}', title=title, artist=song_artist,
                    album=album, disc=disc or 1)) + original.suffix
                destination = move_track_group(original, original.parent / destname)
            except (KeyError, ValueError) as exc:
                print('[WARN] Invalid filename pattern:', exc, flush=True)
        save_lyrics(client, destination, song_artist, title, album, opts)
        if (opts.get('organize') or opts.get('disc_folders')) and not already_organized:
            if not local_album_fallback and (not release or not matched):
                print('[WARN] Folder organization skipped: unverified album/track:', original, flush=True)
                continue
            local_disc = disc
            if local_album_fallback and explicit_disc_group:
                local_disc = disc_order(original.parent)
            target_dir = album_target_directory(Path(opts['source']).resolve(), source, folder_album,
                                                folder_artist or artist, release, target_opts, local_disc)
            if local_album_fallback and explicit_disc_group and opts.get('disc_folders'):
                if original.parent.name.casefold().startswith('bonus'):
                    target_dir = target_dir / clean(opts.get('bonus_disc_folder', 'Bonus CD')) if target_dir else None
                elif original.parent.name.casefold() != 'album':
                    target_dir = target_dir / clean(original.parent.name) if target_dir else None
            if target_dir is not None:
                pending_moves.append((destination, target_dir / destination.name))
    # Preflight the complete album, including LRC and other sidecars.
    # A collision never silently drops an album: allocate a neutral (2), (3)...
    # suffix to the album folder, not to tags or individual track filenames.
    moved_album_tracks = False
    resolved_album_root = None
    if pending_moves:
        first_dir = pending_moves[0][1].parent
        subfolder = album_disc_subfolder(
            (mapping.get(files[0]) or (1,))[0], release, opts, source.name)
        album_root = first_dir.parent if subfolder and first_dir.name == subfolder else first_dir
        selected, resolved_album_root = choose_album_destination(
            pending_moves, source, Path(opts['source']).resolve(), album_root)
        if selected is not None:
            pending_moves = selected
            for src, dst in pending_moves:
                if src != dst:
                    result = move_track_group(src, dst)
                    if result == dst and dst.exists():
                        moved_album_tracks = True
    # Release marker belongs to the album root (never to CD/Bonus CD).
    # Move only after successful album organization, never overwrite a marker.
    if opts.get('organize') and moved_album_tracks and (release or local_album_fallback):
        album_root = resolved_album_root
        if album_root is not None:
            # With bonus discs, disc 1 is the album root. With regular multi-disc
            # releases, strip the CD 1 component from the computed destination.
            if album_root.name == clean(opts.get('bonus_disc_folder', 'Bonus CD')) or \
                    album_root.name == safe_folder_pattern(opts.get('disc_folder_pattern', 'CD {disc}'), {'disc': 1}):
                album_root = album_root.parent
            for marker in markers:
                if not marker.is_file():
                    continue
                dest = album_root / marker.name
                if dest == marker:
                    continue
                if dest.exists():
                    print('[WARN] Release marker target already exists; keeping original:', dest, flush=True)
                    continue
                # Only relocate the marker if at least one audio file actually
                # arrived at the new album directory.
                if not any(dst.exists() and (dst.parent == album_root or album_root in dst.parents)
                           for _, dst in pending_moves):
                    print('[WARN] Release marker kept: album move did not complete:', marker, flush=True)
                    continue
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    marker.rename(dest)
                    print('[MOVE] Release marker:', marker, '->', dest, flush=True)
                except OSError as exc:
                    print('[WARN] Could not move release marker:', marker, exc, flush=True)
    # Local album assets belong to the same edition as the source audio.
    # Only copy after the album has safely arrived; never merge editions.
    if opts.get('organize') and moved_album_tracks and resolved_album_root is not None:
        preserve_local_album_assets(source, resolved_album_root)
    # Artwork/NFO also enrich existing organized albums without requiring moves.
    if already_organized and resolved_album_root is None:
        resolved_album_root = existing_album_root
    # A strong album match is sufficient to enrich an incomplete album, even
    # when several indistinguishable *editions* share its track listing.
    # Keep the exact-edition marker rules above unchanged.
    album_score = scored[0][0] if 'scored' in locals() and scored else None
    verified_for_art = bool(release) and (album_score is None or album_score >= 60)
    if verified_for_art:
        print('[ALBUM CONFIDENCE] Metadata candidate accepted:',
              f'score={album_score:.1f}' if album_score is not None else 'tagged/marked release',
              'track matches=', len(mapping), '/', len(files), flush=True)
    # Embedded cover fallback must also work for albums without a verified
    # MusicBrainz release; otherwise these are precisely the albums left bare.
    if resolved_album_root is not None and opts.get('album_artwork'):
        from music_artwork_nfo import _extract_embedded_album_cover, _copy_if_missing
        cover_names = ('folder.jpg', 'folder.jpeg', 'folder.png')
        if not any((resolved_album_root / name).is_file() for name in cover_names):
            target_audio = ([dst for _, dst in pending_moves if dst.exists()] if moved_album_tracks
                            else [f for f in files if f.is_file()])
            embedded = _extract_embedded_album_cover(target_audio, resolved_album_root)
            central_root = opts.get('artist_artwork_root', '').strip()
            if embedded is not None and central_root:
                central_album = Path(central_root) / resolved_album_root.parent.name / clean(central_tagged_album or album)
                if not any((central_album / name).exists() for name in cover_names):
                    _copy_if_missing(embedded, central_album / 'folder.jpg')
    if verified_for_art and resolved_album_root is not None and any(
            opts.get(key) for key in ('album_artwork', 'artist_artwork', 'music_nfo')):
        from music_artwork_nfo import process_artwork
        target_audio = ([dst for _, dst in pending_moves if dst.exists()] if moved_album_tracks
                        else [f for f in files if f.is_file()])
        try:
            process_artwork(release, album, folder_artist or artist,
                            resolved_album_root, resolved_album_root.parent,
                            opts.get('artist_artwork_root', '').strip(), opts,
                            target_audio, client.session, central_album_name=central_tagged_album or album)
        except Exception as exc:
            print('[WARN] Artwork/NFO generation failed:', source, exc, flush=True)
        # A compilation's album artist (Various Artists) is not the artist
        # of its tracks. Enrich every credited performer centrally, without
        # creating local solo folders beside a compilation or overwriting art.
        if opts.get('artist_artwork') or opts.get('music_nfo'):
            from music_artwork_nfo import process_single_artist
            central_root = opts.get('artist_artwork_root', '').strip()
            credited = album_track_artists(release)
            print('[ALBUM ARTISTS] Distinct credited performers:', len(credited),
                  'album:', album, flush=True)
            for artist_name, artist_id in credited:
                # Deliberately nonexistent local path: compilation performers
                # belong only to the central Artist_Artwork directory.
                local_stub = Path(opts['source']).resolve() / '__central_only__' / clean(artist_name)
                try:
                    process_single_artist(artist_name, artist_id, local_stub,
                                          central_root, opts, client.session)
                except Exception as exc:
                    print('[WARN] Album track artist artwork failed:', artist_name, exc, flush=True)
            # Compilation performers remain central only; do not touch local
            # Various Artists metadata or the existing track organization.
            if opts.get('combined_artist_nfo') and opts.get('music_nfo'):
                from music_artwork_nfo import process_combined_artist
                for medium in release.get('media') or []:
                    for track in medium.get('tracks') or []:
                        track_credit = track.get('artist-credit') or []
                        recording_credit = (track.get('recording') or {}).get('artist-credit') or []
                        credit = (recording_credit if len(credited_artists(recording_credit)) >
                                  len(credited_artists(track_credit)) else track_credit)
                        members = credited_artists(credit)
                        if len(members) < 2:
                            continue
                        display = artist_credit_display(credit)
                        if not display:
                            continue
                        try:
                            process_combined_artist(display, members, central_root, opts)
                        except Exception as exc:
                            print('[WARN] Combined compilation artist NFO failed:', display, exc, flush=True)
    elif local_album_fallback and resolved_album_root is not None and opts.get('music_nfo'):
        from music_artwork_nfo import _xml_if_missing
        _xml_if_missing(resolved_album_root / 'album.nfo', 'album',
                        [('title', album), ('artistdesc', folder_artist or artist), ('year', local_year)], placeholder=True)
        _xml_if_missing(resolved_album_root.parent / 'artist.nfo', 'artist',
                        [('name', folder_artist or artist)], placeholder=True)
        central_root = opts.get('artist_artwork_root', '').strip()
        if central_root:
            central_artist = Path(central_root) / resolved_album_root.parent.name
            _xml_if_missing(central_artist / clean(central_tagged_album or album) / 'album.nfo', 'album',
                            [('title', album), ('artistdesc', folder_artist or artist), ('year', local_year)], placeholder=True)
            _xml_if_missing(central_artist / 'artist.nfo', 'artist', [('name', folder_artist or artist)], placeholder=True)
        print('[ALBUM LOCAL NFO] Created from existing local metadata:', resolved_album_root, flush=True)
    elif any(opts.get(key) for key in ('album_artwork', 'artist_artwork', 'music_nfo')):
        print('[WARN] Artwork/NFO skipped: album not safely organized or release unverified:', source, flush=True)
    # Artist metadata must not depend on release/track confidence or on a
    # successful album move. Run also for local fallback and uncertain albums.
    if (opts.get('artist_artwork') or opts.get('music_nfo')) and (folder_artist or artist):
        from music_artwork_nfo import process_single_artist
        primary_name = folder_artist or artist
        primary_id = release_artist_id(release) if release else ''
        if not primary_id and opts.get('lookup', True):
            exact_artist = client.artist_search_exact(primary_name)
            primary_id = (exact_artist or {}).get('id', '')
        if primary_id:
            primary_local = (resolved_album_root.parent if resolved_album_root is not None
                             else source.parent if depth >= 2 else source)
            try:
                process_single_artist(primary_name, primary_id, primary_local,
                                      opts.get('artist_artwork_root', '').strip(),
                                      opts, client.session)
                print('[ARTIST INDEPENDENT] Enriched:', primary_name, primary_id, flush=True)
            except Exception as exc:
                print('[WARN] Independent artist enrichment failed:', primary_name, exc, flush=True)
        else:
            print('[WARN] Artist MusicBrainz identity unresolved:', primary_name, flush=True)
    # Apply the configured folder pattern to albums already in an artist/album
    # hierarchy as well. Rename the complete directory, never individual tracks,
    # so artwork, NFO, markers and sidecars remain together. Never merge folders.
    if already_organized and opts.get('organize') and resolved_album_root is not None:
        artist_dir = resolved_album_root.parent
        if artist_dir.parent.resolve() == root and resolved_album_root.is_dir():
            year = str(local_year or (release or {}).get('_preferred_date') or
                       (release or {}).get('date') or '')[:4]
            values = {'year': year or 'Unknown', 'album': folder_album,
                      'artist': folder_artist or artist, 'albumartist': folder_artist or artist,
                      'disc': 1}
            pattern = opts.get('album_folder_pattern', '{year} - {album}')
            desired = safe_folder_pattern(pattern, values)
            if desired and desired != resolved_album_root.name:
                destination = artist_dir / desired
                if destination.exists():
                    print('[WARN] Existing album rename skipped; destination exists:', destination, flush=True)
                else:
                    try:
                        resolved_album_root.rename(destination)
                        print('[ALBUM FOLDER] Renamed according to configuration:',
                              resolved_album_root, '->', destination, flush=True)
                        resolved_album_root = destination
                    except OSError as exc:
                        print('[WARN] Existing album rename failed:', resolved_album_root, exc, flush=True)
        else:
            print('[INFO] Album folder rename skipped: unexpected directory hierarchy:',
                  resolved_album_root, flush=True)
    print('[ALBUM]', resolved_album_root or source,
          'release=', release.get('id') if release else 'unresolved', flush=True)
    return True


def run(opts):
    global AUDIO
    # Single-track mode is deliberately independent of folder/album discovery.
    selected = opts.get('single_track')
    if selected:
        track = Path(selected).expanduser().resolve()
        if not track.is_file() or track.suffix.lower() not in AUDIO:
            raise ValueError('Selected audio track does not exist or has unsupported format')
        opts = dict(opts)
        # A managed album already has an authoritative NFO and artist parent.
        # Never generate a nested Artist folder while rescraping one track.
        if (track.parent / 'album.nfo').is_file():
            opts['artist_artwork'] = False
            opts['music_nfo'] = False
            opts['combined_artist_nfo'] = False
            opts['album_artwork'] = False
            opts['embed_album_cover'] = False
        # The single-track file normally lives *inside* its artist directory.
        # The regular singles pipeline interprets 'source' as the collection
        # root, so passing track.parent would create Artist/Artist artwork.
        # Only step up one level when the folder actually matches the tagged
        # performer; otherwise retain the original source directory.
        artist_name, _, _ = single_track_info(track)
        source_root = track.parent
        if artist_name and artist_similarity(track.parent.name, artist_name) >= .8:
            source_root = track.parent.parent
        opts['source'] = str(source_root)
        print('[SCRAPER SINGLE TRACK ROOT]', source_root, 'artist_folder=', track.parent, flush=True)
        # Single track: no file moves or renames. Missing artist metadata and
        # artwork use the regular additive, no-overwrite scraper pipeline.
        for flag in ('rename', 'organize', 'disc_folders'):
            opts[flag] = False
        _ffprobe_executable()
        print('[SCRAPER SINGLE TRACK]', track, 'artist_artwork=', bool(opts.get('artist_artwork')),
              'music_nfo=', bool(opts.get('music_nfo')), 'album_artwork=', bool(opts.get('album_artwork')), flush=True)
        process_single(track.parent, [track], opts, Client())
        print('[RESULT] Single track processed:', track, flush=True)
        return 0
    source = Path(opts['source']).expanduser().resolve()
    _MOVED_SOURCE_DIRS.clear()
    _MOVE_DEST_DIRS.clear()
    print('[SCRAPER SOURCE]', source, flush=True)
    if not source.is_dir():
        raise ValueError('Input folder does not exist')
    AUDIO = {str(x).lower() for x in opts.get('audio_extensions', sorted(AUDIO))}
    # Fail clearly before processing any album if the configured tool is missing.
    _ffprobe_executable()
    client = Client()
    count = errors = 0
    audio_extensions = {str(x).lower() for x in opts.get('audio_extensions', sorted(AUDIO))}
    audio_folders = [p for p in [source, *sorted(source.rglob('*'))] if p.is_dir()
                     and any(x.is_file() and x.suffix.lower() in audio_extensions for x in p.iterdir())]
    # Group multi-disc album subdirectories before considering individual folders.
    grouped = {}
    consumed = set()
    for parent in [source, *sorted(p for p in source.rglob('*') if p.is_dir())]:
        children = [c for c in parent.iterdir() if c.is_dir() and is_disc_folder(c)
                    and any(f.is_file() and f.suffix.lower() in audio_extensions for f in c.iterdir())]
        parent_tracks = [f for f in parent.iterdir() if f.is_file() and f.suffix.lower() in audio_extensions]
        if children and (len(children) >= 2 or parent_tracks or any(
                p.is_file() and UUID_RE.fullmatch(p.stem) for p in parent.glob('*.txt'))):
            collected = sorted(f for c in children for f in c.iterdir()
                               if f.is_file() and f.suffix.lower() in AUDIO)
            collected += sorted(f for f in parent.iterdir() if f.is_file() and f.suffix.lower() in AUDIO)
            grouped[parent] = collected
            consumed.update(children)
            consumed.add(parent)
    folders = [(p, grouped[p]) for p in sorted(grouped)]
    folders += [(p, None) for p in audio_folders if p not in consumed]
    folders.sort(key=lambda pair: str(pair[0]))
    for idx, (folder, grouped_files) in enumerate(folders, 1):
        if os.environ.get('MB_METADATA_STOP_FILE') and Path(os.environ['MB_METADATA_STOP_FILE']).exists():
            print('[STOP] User requested stop', flush=True)
            break
        print('[MB_METADATA_PROGRESS] ' + json.dumps({'current': idx - 1, 'total': len(folders), 'file': str(folder)}, ensure_ascii=False), flush=True)
        try:
            if process(folder, opts, client, grouped_files):
                count += 1
        except Exception as exc:
            errors += 1
            import traceback
            print('[ERROR]', folder, type(exc).__name__, exc, flush=True)
            traceback.print_exc()
        print('[MB_METADATA_PROGRESS] ' + json.dumps({'current': idx, 'total': len(folders), 'file': str(folder)}, ensure_ascii=False), flush=True)
    if opts.get('organize'):
        remove_empty_moved_directories(source)
    print(f'[RESULT] Music folders: {count}; errors: {errors}', flush=True)
    return 0 if not errors else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--options', required=True, help='JSON options')
    args = parser.parse_args()
    from log_manager import run_with_file_logging
    raise SystemExit(run_with_file_logging(lambda _argv: run(json.loads(args.options)), [], scraper_name='music'))
