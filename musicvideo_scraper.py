#!/usr/bin/env python3
"""Media-Buddy V2 standalone music-video scraper (Test 53).

Kodi metadata.musicvideos.python inspired: TheAudioDB track search, optional
Last.fm enrichment, NFO and artwork. No Kodi runtime dependency.
"""
from __future__ import annotations
from linked_media_walk import linked_files
import fnmatch
import shutil
import email.utils
import time
import unicodedata
from difflib import SequenceMatcher
import html
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
import requests
from config_manager import ConfigManager, ConfigError
from api_keys import get_api_key
from translation_manager import translate_plot
from musicbrainz_artist_resolver import ArtistMBIDResolver
from artist_metadata import create_artist_nfo

AUDIODB = 'https://www.theaudiodb.com/api/v1/json/'
LASTFM = 'https://ws.audioscrobbler.com/2.0/'
MUSICBRAINZ = 'https://musicbrainz.org/ws/2/recording/'
_last_mb_call = 0.0
_mb_disabled = False
_lastfm_disabled = False
_lastfm_failures = 0

def parse_args(args):
    folder = None
    for arg in args:
        if arg.lower().startswith('--folder:') or arg.lower().startswith('--folder='):
            folder = re.split('[:=]', arg, maxsplit=1)[1].strip('"')
        elif not arg.startswith('--') and folder is None:
            folder = arg
        elif arg.startswith('--') and not arg.lower().startswith('--type:'):
            raise ValueError(f'Unknown argument: {arg}')
    if not folder:
        raise ValueError('Usage: python musicvideo_scraper.py --folder:"PATH"')
    return Path(folder).expanduser()

def identify(path, ignored):
    stem = path.stem
    for word in ignored:
        stem = re.sub(r'(?i)(?<!\w)' + re.escape(str(word)) + r'(?!\w)', ' ', stem)
    stem = re.sub(r'\s+', ' ', stem).strip()
    if ' - ' not in stem:
        return None
    artist, title = stem.split(' - ', 1)
    title = re.sub(r'\s*\[(?:official\s+)?(?:music\s+)?video\]\s*$', '', title, flags=re.I).strip()
    return (artist.strip(), title) if artist.strip() and title else None

def text(parent, tag, value):
    if value is not None and str(value).strip():
        ET.SubElement(parent, tag).text = html.unescape(str(value).strip())

def fetch(session, url, params, timeout):
    response = session.get(url, params=params, timeout=timeout)
    response.raise_for_status()
    return response.json()

def normalized(value):
    value = unicodedata.normalize('NFKD', str(value or '').casefold())
    value = ''.join(c for c in value if not unicodedata.combining(c))
    return re.sub(r'[^a-z0-9]+', ' ', value).strip()


def artist_parts(artist):
    parts = re.split(r'\s+(?:feat\.?|ft\.?|featuring|with|and|x)\s+|\s*&\s*|\s*[,;]\s*', artist, flags=re.I)
    return [p.strip() for p in parts if p.strip()]


def match_score(found_artist, found_title, artist, title):
    ta, tb = normalized(title), normalized(found_title)
    title_score = SequenceMatcher(None, ta, tb).ratio()
    if title_score < .78:
        return 0.0
    wanted = [normalized(x) for x in artist_parts(artist)]
    found = normalized(found_artist)
    hits = sum(1 for x in wanted if x and (x in found or SequenceMatcher(None, x, found).ratio() > .88))
    if not hits:
        return 0.0
    artist_score = hits / max(1, len(wanted))
    return .68 * title_score + .32 * artist_score


def get_track(session, artist, title, api, timeout, threshold=.80):
    candidates = [artist] + artist_parts(artist)
    best, best_score = None, 0.0
    for candidate in dict.fromkeys(candidates):
        try:
            data = fetch(session, f'{AUDIODB}{api}/searchtrack.php', {'s': candidate, 't': title}, timeout)
        except (requests.RequestException, ValueError) as exc:
            print(f'[WARN] TheAudioDB query failed: {exc}')
            continue
        for track in data.get('track') or []:
            score = match_score(track.get('strArtist'), track.get('strTrack'), artist, title)
            if score > best_score:
                best, best_score = track, score
        if best_score >= .99:
            break
    if best_score >= threshold:
        print(f'[MATCH] TheAudioDB ({best_score:.2f}): {artist} - {title}')
        return best
    return None


def musicbrainz_track(session, artist, title, timeout, threshold=.80, user_agent=None):
    global _last_mb_call, _mb_disabled
    if _mb_disabled:
        return None
    if not user_agent or 'configure-your-email@example.org' in user_agent:
        if not _mb_disabled:
            print('[WARN] MusicBrainz requires a real contact in MusicBrainzUserAgent (musicvideo_scraper_config.json); skipping MusicBrainz.')
        _mb_disabled = True
        return None
    headers = {'User-Agent': user_agent, 'Accept': 'application/json'}
    variants = [f'recording:"{title}" AND artistname:"{name}"' for name in artist_parts(artist)]
    variants.append(f'recording:"{title}"')
    best, best_score = None, 0.0
    for query in dict.fromkeys(variants):
        wait = 1.1 - (time.monotonic() - _last_mb_call)
        if wait > 0: time.sleep(wait)
        _last_mb_call = time.monotonic()
        try:
            response = session.get(MUSICBRAINZ, params={'query': query, 'fmt': 'json', 'limit': 25}, headers=headers, timeout=timeout)
            if response.status_code in (401, 403):
                _mb_disabled = True
                print(f'[WARN] MusicBrainz HTTP {response.status_code}; disabling MusicBrainz for this run. Check User-Agent/contact and network access.')
                return None
            if response.status_code == 429:
                _mb_disabled = True
                print('[WARN] MusicBrainz rate limited (HTTP 429); disabling MusicBrainz for this run.')
                return None
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            print(f'[WARN] MusicBrainz query failed: {exc}')
            continue
        for item in data.get('recordings') or []:
            credit = item.get('artist-credit') or []
            credited = ' '.join(str(x.get('name') or x.get('artist', {}).get('name') or '') for x in credit if isinstance(x, dict))
            score = match_score(credited, item.get('title'), artist, title)
            if score > best_score:
                best, best_score = item, score
        if best_score >= .99: break
    if not best or best_score < threshold:
        return None
    credit = best.get('artist-credit') or []
    credited = ' '.join(str(x.get('name') or x.get('artist', {}).get('name') or '') for x in credit if isinstance(x, dict))
    releases = best.get('releases') or []
    result = {'strArtist': credited or artist, 'strTrack': best.get('title') or title,
              'idTrack': best.get('id'), 'intDuration': best.get('length'),
              'strAlbum': releases[0].get('title') if releases else None,
              '_source': 'MusicBrainz'}
    print(f'[MATCH] MusicBrainz ({best_score:.2f}): {artist} - {title}')
    return result

def lastfm_track(session, artist, title, key, timeout, threshold=.80):
    """Resolve a recording with Last.fm, trying individual credited artists.

    Never accept a matching title without a credible artist match. Last.fm's
    track.getInfo returns error 6 for an unknown track; this is not an outage.
    """
    global _lastfm_disabled, _lastfm_failures
    if _lastfm_disabled or not key:
        return None
    best, best_score = None, 0.0
    candidates = list(dict.fromkeys([artist] + artist_parts(artist)))
    for candidate in candidates:
        try:
            data = fetch(session, LASTFM, {
                'method': 'track.getInfo', 'api_key': key,
                'artist': candidate, 'track': title,
                'autocorrect': 1, 'format': 'json'}, timeout)
        except (requests.RequestException, ValueError) as exc:
            _lastfm_failures += 1
            print(f'[WARN] Last.fm connection failed: {exc}')
            if _lastfm_failures >= 2:
                _lastfm_disabled = True
                print('[WARN] Last.fm disabled for this run after repeated connection errors')
                break
            continue
        if 'error' in data:
            code = data.get('error')
            if code in (10, 26, 29):
                _lastfm_disabled = True
                print(f'[WARN] Last.fm API error {code}; disabled for this run')
                break
            if code not in (6,):
                print(f'[WARN] Last.fm API error {code}: {data.get("message", "unknown")}')
            continue
        _lastfm_failures = 0
        item = data.get('track') or {}
        credited = item.get('artist') or {}
        if isinstance(credited, dict):
            credited = credited.get('name') or ''
        score = match_score(credited, item.get('name'), artist, title)
        if score > best_score:
            best, best_score = item, score
        if best_score >= .99:
            break
    if best is None or best_score < threshold:
        return None
    print(f'[MATCH] Last.fm ({best_score:.2f}): {artist} - {title}')
    return best


def lastfm_as_track(item, artist, title):
    credited = item.get('artist') or {}
    if isinstance(credited, dict):
        credited = credited.get('name') or artist
    return {'strArtist': credited or artist,
            'strTrack': item.get('name') or title,
            'strAlbum': (item.get('album') or {}).get('title'),
            'intDuration': item.get('duration'),
            '_source': 'Last.fm'}

def clean_wiki(value):
    """Remove Last.fm boilerplate and markup before translation."""
    value = re.split(r'(?i)\s*Read more (?:on|at) Last\.fm', str(value or ''), maxsplit=1)[0]
    value = re.sub(r'<[^>]*>', '', value)
    return html.unescape(value).strip()


def lastfm_album_info(session, artist, album, key, timeout):
    """Return album metadata only for a credible artist/album match."""
    if not album or not key or _lastfm_disabled:
        return {}
    for candidate in dict.fromkeys([artist] + artist_parts(artist)):
        try:
            data = fetch(session, LASTFM, {
                'method': 'album.getInfo', 'api_key': key, 'artist': candidate,
                'album': album, 'autocorrect': 1, 'format': 'json'}, timeout)
        except (requests.RequestException, ValueError) as exc:
            print(f'[WARN] Last.fm album lookup failed: {exc}')
            return {}
        item = data.get('album') or {}
        if not item:
            continue
        if normalized(item.get('name')) != normalized(album):
            continue
        credited = item.get('artist') or ''
        if not any(normalized(x) in normalized(credited) for x in artist_parts(artist)):
            continue
        return item
    return {}


def audiodb_album_info(session, track, api, timeout):
    """Use the exact album ID of the identified track; never guess an album."""
    album_id = track.get('idAlbum')
    if not album_id:
        return {}
    try:
        data = fetch(session, f'{AUDIODB}{api}/album.php', {'m': album_id}, timeout)
        albums = data.get('album') or []
        return albums[0] if albums else {}
    except (requests.RequestException, ValueError) as exc:
        print(f'[WARN] TheAudioDB album lookup failed: {exc}')
        return {}


def choose_plot(session, track, lastfm, artist, cfg, settings, keys, timeout):
    lang = str(cfg.get('Language', 'de')).lower()[:2]
    wiki = clean_wiki((lastfm.get('wiki') or {}).get('summary'))
    if wiki:
        print('[PLOT] Track description: Last.fm')
        return translate_description(wiki, 'en', lang, settings, timeout)
    local = track.get('strDescription' + lang.upper()) or ''
    english = track.get('strDescriptionEN') or ''
    if local or english:
        print('[PLOT] Track description: TheAudioDB')
        return local or translate_description(english, 'en', lang, settings, timeout)
    if not settings.get('UseAlbumDescriptionFallback', True):
        return ''
    album = track.get('strAlbum') or (lastfm.get('album') or {}).get('title')
    if not album:
        return ''
    album_info = audiodb_album_info(session, track, keys.get('TheAudioDB', ''), timeout)
    local = album_info.get('strDescription' + lang.upper()) or ''
    english = album_info.get('strDescriptionEN') or ''
    if local or english:
        print(f'[PLOT] Album description: TheAudioDB ({album})')
        desc = local or translate_description(english, 'en', lang, settings, timeout)
        return f'Album: {album}\n\n{desc}'
    item = lastfm_album_info(session, artist, album, keys.get('LastFM'), timeout)
    desc = clean_wiki((item.get('wiki') or {}).get('summary'))
    if desc:
        print(f'[PLOT] Album description: Last.fm ({album})')
        return f'Album: {album}\n\n{translate_description(desc, "en", lang, settings, timeout)}'
    return ''


def translate_description(value, source, target, settings, timeout):
    if not value or source == target or not settings.get('TranslateMissingTexts', True):
        return value
    result = translate_plot(value, source, target, timeout=timeout)
    if result.success:
        return result.text
    print(f'[WARN] Translation failed: {result.error}')
    return value


def make_nfo(track, artist, title, plot, lastfm, artist_mbids=None):
    """Kodi musicvideo NFO: mandatory title/artist; explicit empty optional fields."""
    root = ET.Element('musicvideo')
    def add(tag, value=''):
        node = ET.SubElement(root, tag)
        node.text = html.unescape(str(value).strip()) if value is not None else ''
        return node

    add('title', track.get('strTrack') or title)
    # Preserve all artists from the filename even if an API credits only one.
    for name in dict.fromkeys(artist_parts(artist) or [artist]):
        add('artist', name)
    add('album', track.get('strAlbum') or (lastfm.get('album') or {}).get('title'))
    add('userrating')
    add('plot', plot)
    for tag, value in (('genre', track.get('strGenre')),
                       ('director', track.get('strMusicVidDirector')),
                       ('studio', track.get('strMusicVidCompany'))):
        values = [x.strip() for x in re.split(r'\s*[,;/|]\s*', str(value)) if x.strip()] if value else ['']
        for item in dict.fromkeys(values):
            add(tag, item)
    add('premiered', track.get('strReleaseDate') or '')
    add('actor')
    uid = track.get('idTrack') if track.get('_source') != 'Last.fm' else None
    node = add('uniqueid', uid)
    if uid:
        node.set('type', 'musicbrainz' if track.get('_source') == 'MusicBrainz' else 'theaudiodb')
    # Media-Buddy extension: retain artist-to-MBID mapping for later editing.
    # No default attribute: Kodi does not consume these additional artist IDs.
    for artist_name, mbid in (artist_mbids or {}).items():
        if not artist_name or not mbid:
            continue
        node = add('uniqueid', mbid)
        node.set('type', 'musicbrainz_artist')
        node.set('artist', artist_name)
    return ET.ElementTree(root)


def artist_artwork(session, artist, api, timeout):
    """Retrieve artist artwork from TheAudioDB; never confuse album art with artist art."""
    for name in dict.fromkeys([artist_parts(artist)[0] if artist_parts(artist) else artist]):
        try:
            data = fetch(session, f'{AUDIODB}{api}/search.php', {'s': name}, timeout)
        except (requests.RequestException, ValueError) as exc:
            print(f'[WARN] Artist artwork lookup failed: {exc}')
            return {}
        artists = data.get('artists') or []
        candidates = [a for a in artists if normalized(a.get('strArtist')) == normalized(name)]
        if candidates:
            item = candidates[0]
            print(f'[ARTIST] Artwork source: {item.get("strArtist")}')
            return {
                'poster': item.get('strArtistThumb') or item.get('strArtistWideThumb'),
                'fanart': item.get('strArtistFanart') or item.get('strArtistFanart2'),
                'banner': item.get('strArtistBanner'),
                'clearlogo': item.get('strArtistLogo'),
                'clearart': item.get('strArtistClearart'),
                'landscape': item.get('strArtistWideThumb') or item.get('strArtistFanart'),
            }
    return {}


def resolve_fanart_key(configured_key):
    """Use the central API-key layer, not scraper configuration or other scrapers."""
    key = get_api_key('FANART_TV')
    if not key:
        print('[FANART.TV SKIP] No API key available in central API-key layer')
    return key


FANART_TV = 'https://webservice.fanart.tv/v3/music/'


def _fanart_best_url(items):
    """Prefer highly-liked Fanart.tv entries; ignore malformed/non-HTTPS URLs."""
    if not isinstance(items, list):
        return None
    def likes(item):
        try:
            return int(item.get('likes') or 0)
        except (TypeError, ValueError):
            return 0
    for item in sorted((x for x in items if isinstance(x, dict)), key=likes, reverse=True):
        url = item.get('url')
        if isinstance(url, str) and url.startswith('https://'):
            return url
    return None


def fanart_tv_artwork(session, mbid, api_key, timeout):
    """Fanart.tv music artist endpoint. Missing key/artwork is nonfatal."""
    if not api_key:
        print('[FANART.TV SKIP] No API key available')
        return {}
    if not mbid:
        print('[FANART.TV SKIP] Artist MBID unavailable')
        return {}
    print(f'[FANART.TV] Requesting artist artwork | MBID={mbid}')
    try:
        response = session.get(FANART_TV + mbid, params={'api_key': api_key}, timeout=timeout)
        if response.status_code in (400, 401, 403):
            print(f'[FANART.TV WARN] HTTP {response.status_code}: check FanartTV API key')
            return {}
        if response.status_code == 404:
            print(f'[FANART.TV] No artist artwork for MBID={mbid}')
            return {}
        response.raise_for_status()
        print(f'[FANART.TV] HTTP {response.status_code} OK | MBID={mbid}')
        data = response.json()
        if not isinstance(data, dict):
            print(f'[FANART.TV WARN] Unexpected JSON structure | MBID={mbid}')
            return {}
    except (requests.RequestException, ValueError) as exc:
        print(f'[FANART.TV WARN] MBID={mbid}: {exc}')
        return {}
    # Fanart.tv exposes artist thumbs/backgrounds, banners, logos and discography art.
    # Music artist endpoint has no canonical Kodi 'poster' field: artistthumb is used.
    mappings = {
        'poster': ('artistthumb',),
        'fanart': ('artistbackground',),
        'banner': ('musicbanner',),
        'clearlogo': ('hdmusiclogo', 'musiclogo'),
        'clearart': ('artistclearart',),
        'landscape': ('artistthumb', 'artistbackground'),
    }
    artwork = {}
    for kind, fields in mappings.items():
        for field in fields:
            url = _fanart_best_url(data.get(field))
            if url:
                artwork[kind] = url
                break
    print('[FANART.TV] MBID={} | artwork={}'.format(mbid, ', '.join(artwork) or 'none'))
    return artwork



def _artist_nfo_mbids(folder):
    """Read artist identity without changing Kodi's existing NFO."""
    nfo = folder / 'artist.nfo'
    if not nfo.is_file():
        return set()
    try:
        root = ET.parse(nfo).getroot()
    except (ET.ParseError, OSError) as exc:
        print(f'[ARTIST FOLDER WARN] Cannot read {nfo}: {exc}')
        return set()
    result = set()
    for node in root.iter():
        tag = node.tag.rsplit('}', 1)[-1].casefold()
        value = (node.text or '').strip().casefold()
        if not value:
            continue
        if tag == 'musicbrainzartistid' or (tag == 'uniqueid' and 'musicbrainz' in node.attrib.get('type', '').casefold()):
            result.add(value)
    return result


def _artist_folder(base, artist, mbid):
    """Reuse Kodi artist folders; refuse conflicting identities and ambiguous matches."""
    if not base or not base.is_dir():
        return None
    candidates = [p for p in base.iterdir() if p.is_dir()]
    wanted = str(mbid or '').casefold()
    if wanted:
        id_matches = [p for p in candidates if wanted in _artist_nfo_mbids(p)]
        if len(id_matches) == 1:
            return id_matches[0]
        if len(id_matches) > 1:
            print(f'[ARTIST FOLDER WARN] Multiple folders for MBID {mbid}; skipping central artwork')
            return None
    name_matches = [p for p in candidates if normalized(p.name) == normalized(artist)]
    if len(name_matches) > 1:
        print(f'[ARTIST FOLDER WARN] Ambiguous folder name {artist}; skipping central artwork')
        return None
    if name_matches:
        existing = name_matches[0]
        ids = _artist_nfo_mbids(existing)
        if wanted and ids and wanted not in ids:
            print(f'[ARTIST FOLDER WARN] Conflicting MBID in {existing}; skipping central artwork')
            return None
        return existing
    # Do not create new folders for provisional/unverified MBIDs.
    if not wanted:
        print(f'[ARTIST FOLDER WARN] No verified MBID for new artist {artist}; skipping central artwork')
        return None
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', artist).strip(' .')
    if not safe or safe in ('.', '..'):
        return None
    target = base / safe
    try:
        target.mkdir(exist_ok=True)
        print(f'[ARTIST FOLDER] Created {target}')
        return target
    except OSError as exc:
        print(f'[ARTIST FOLDER WARN] Cannot create {target}: {exc}')
        return None


def _artist_base(cfg):
    raw = cfg.global_config.get('Audio', {}).get('ArtistArtworkPath', '')
    if not isinstance(raw, str) or not raw.strip():
        print('[ARTIST FOLDER] No central ArtistArtworkPath configured; keeping local artwork behavior')
        return None
    base = Path(raw.strip())
    if not base.is_dir():
        print(f'[ARTIST FOLDER WARN] Central path unavailable: {base}; keeping local artwork behavior')
        return None
    print(f'[ARTIST FOLDER] Central artwork base: {base}')
    return base

def save_art(session, track, video, cfg, timeout, lastfm=None, artist=None, api='123', artist_mbids=None, fanart_key=None, artist_base=None, verified_artists=None):
    """Track thumbnails and artist-based artwork with URL reuse and safe filenames."""
    if not cfg.get('Download_Images', False):
        return 0
    options = cfg.section('MusicVideos')
    kinds = {
        'thumb': ('Image_Thumb', 'thumb.jpg'),
        'poster': ('Image_Poster', 'poster.jpg'),
        'fanart': ('Image_Fanart', 'fanart.jpg'),
        'banner': ('Image_Banner', 'banner.jpg'),
        'clearlogo': ('Image_ClearLogo', 'clearlogo.png'),
        'clearart': ('Image_ClearArt', 'clearart.png'),
        'landscape': ('Image_Landscape', 'landscape.jpg'),
        'discart': ('Image_Disc', 'discart.png'),
        'keyart': ('Image_Keyart', 'keyart.jpg'),
    }
    sources = {'thumb': track.get('strTrackThumb')}
    album = (lastfm or {}).get('album') or {}
    images = album.get('image') or []
    album_image = next((x.get('#text') for x in reversed(images)
                        if isinstance(x, dict) and str(x.get('#text', '')).startswith('https://')), None)
    sources['thumb'] = sources['thumb'] or album_image
    artist_kinds = ('poster', 'fanart', 'banner', 'clearlogo', 'clearart', 'landscape')
    if artist and any(options.get(kinds[k][0], False) for k in artist_kinds):
        primary = (artist_parts(artist) or [artist])[0]
        mbid = (artist_mbids or {}).get(primary)
        fanart = fanart_tv_artwork(session, mbid, fanart_key, timeout) if options.get('UseFanartTV', True) else {}
        # Fetch TheAudioDB only if a requested artist-art type remains unresolved.
        missing = [kind for kind in artist_kinds if options.get(kinds[kind][0], False) and not fanart.get(kind)]
        fallback = artist_artwork(session, artist, api, timeout) if missing else {}
        for kind in artist_kinds:
            if fanart.get(kind):
                sources[kind] = fanart[kind]
                print(f'[ART SOURCE] {kind}: Fanart.tv')
            elif fallback.get(kind):
                sources[kind] = fallback[kind]
                if options.get(kinds[kind][0], False):
                    print(f'[ART SOURCE] {kind}: TheAudioDB (fallback)')
    # Artist poster belongs in the shared artist folder. The video-specific
    # poster prefers track artwork, then album artwork, then artist artwork.
    video_poster_url = (track.get('strTrackThumb') or track.get('strTrack3DCase')
                        or album_image or sources.get('poster'))
    sources['poster'] = sources.get('poster') or album_image
    # Fanart is artist-centric; album-specific backgrounds are a last resort.
    count = 0
    downloaded = {}
    primary_artist = (artist_parts(artist) or [artist or ''])[0]
    primary_mbid = (artist_mbids or {}).get(primary_artist)
    central = _artist_folder(artist_base, primary_artist, primary_mbid if primary_artist in (verified_artists or set()) else None) if artist_base else None
    # Video-specific sidecars are independent of the shared artist archive.
    # A generated/custom video poster or fanart always wins; never overwrite it.
    # New music-video covers use -poster.jpg, not -thumb.jpg.
    import shutil
    overwrite = bool(cfg.section('Overwrite').get('Artwork', False))
    for kind, (setting, filename) in kinds.items():
        if not (options.get(setting, False) or (kind == 'poster' and options.get('Image_Thumb', False))):
            continue
        # The old track thumb is only a fallback for the new unified poster.
        if kind == 'thumb':
            continue
        url = sources.get(kind)
        if kind == 'poster':
            url = sources.get('poster') or sources.get('thumb')
        is_shared = central is not None and kind in artist_kinds
        shared_target = (central / ('folder.jpg' if kind == 'poster' else filename)) if is_shared else None
        video_target = video.with_name(video.stem + '-' + filename)
        targets = [shared_target] if shared_target else []
        if kind in ('poster', 'fanart'):
            targets.append(video_target)
        elif not shared_target:
            targets.append(video_target)
        # Do not replace a video-specific fanart, even if overwrite is enabled.
        for target in targets:
            if target is None:
                continue
            if target.exists() and (target == video_target and kind == 'fanart' or
                                    target == shared_target or not overwrite):
                continue
            try:
                # Keep artist artwork and video-specific artwork independent.
                # Never copy an artist poster over an available track image.
                target_url = (video_poster_url if kind == 'poster' and target == video_target else url)
                local_source = (shared_target if target == video_target and shared_target
                                and shared_target.is_file() and not (kind == 'poster' and
                                video_poster_url and video_poster_url != sources.get('poster')) else None)
                if local_source:
                    shutil.copyfile(local_source, target)
                    print(f'[ARTWORK] {target.name} (artist artwork copy)')
                elif target_url and str(target_url).startswith('https://'):
                    if target_url in downloaded and downloaded[target_url].is_file():
                        shutil.copyfile(downloaded[target_url], target)
                        print(f'[ARTWORK] {target.name} (local copy)')
                    else:
                        response = session.get(target_url, timeout=timeout, stream=True)
                        response.raise_for_status()
                        temporary = target.with_suffix(target.suffix + '.part')
                        try:
                            with temporary.open('wb') as output:
                                for chunk in response.iter_content(65536):
                                    output.write(chunk)
                            temporary.replace(target)
                            downloaded[target_url] = target
                            print(f'[ARTWORK] {target.name}')
                        finally:
                            temporary.unlink(missing_ok=True)
                            response.close()
                else:
                    print(f'[ART MISSING] {kind}: no local image or usable URL')
                    continue
                count += 1
            except (requests.RequestException, OSError) as exc:
                print(f'[WARN] Artwork {kind} failed for {video.name}: {exc}')
    return count

def main(argv, *, wizard_overrides=None, completed_paths=None):
    try:
        root = parse_args(argv)
        cfg = ConfigManager(Path(__file__).resolve().parent, 'musicvideo_scraper_config.json')
        if wizard_overrides:
            from config_manager import _merge
            cfg.effective = _merge(cfg.effective, wizard_overrides)
        if not root.is_dir(): raise ValueError(f'Folder does not exist: {root}')
        settings = cfg.section('MusicVideos')
        keys = {
            'TheAudioDB': get_api_key('THEAUDIODB'),
            'LastFM': get_api_key('LASTFM'),
            'FanartTV': get_api_key('FANART_TV'),
        }
        for provider, value in keys.items():
            if not value:
                print(f'[API KEY WARN] {provider}: no key in central API-key layer')
        timeout = cfg.network_timeout()
        ignored_dirs = {x.casefold() for x in cfg.get('FolderIgnore', [])}
        patterns = cfg.get('FileExtensions', ['*.mkv', '*.mp4'])
        files = sorted(p for p in linked_files(root) if p.is_file() and
                       not any(x.casefold() in ignored_dirs for x in p.relative_to(root).parts[:-1]) and
                       any(fnmatch.fnmatch(p.name.casefold(), x.casefold()) for x in patterns))
        print(f'Media-Buddy V2 Musicvideo Scraper | {len(files)} file(s)')
        written = skipped = errors = artworks = 0
        mb_resolver = ArtistMBIDResolver(timeout, settings.get('MusicBrainzUserAgent')) if settings.get('ResolveArtistMBID', True) else None
        try:
            return _process_musicvideos(files, cfg, settings, keys, timeout, mb_resolver, completed_paths)
        finally:
            if mb_resolver: mb_resolver.close()
    except (ValueError, ConfigError, OSError) as exc:
        print(f'[ERROR] {exc}', file=sys.stderr)
        return 2

def _process_musicvideos(files, cfg, settings, keys, timeout, mb_resolver, completed_paths=None):
        written = skipped = errors = artworks = 0
        fanart_key = resolve_fanart_key(keys.get('FanartTV')) if settings.get('UseFanartTV', True) and cfg.get('Download_Images', False) else None
        artist_base = _artist_base(cfg)
        from scraper_progress import emit
        with requests.Session() as session:
            for file_index, video in enumerate(files, 1):
                from metadata_stop import requested as metadata_stop_requested
                if metadata_stop_requested():
                    print("[ABORT] Stop after previous file requested")
                    break
                emit('musicvideo', 'Musikvideo identifizieren', file=video.name,
                     index=file_index, total=len(files))
                nfo = video.with_suffix('.nfo')
                if nfo.exists() and not cfg.section('Overwrite').get('NFO', False):
                    print(f'[SKIP NFO] {nfo.name}'); skipped += 1
                    if completed_paths is not None: completed_paths.append(video)
                    continue
                pair = identify(video, cfg.get('FileIgnoreStrings', []))
                if pair is None:
                    print(f'[WARN] No artist - title pattern: {video.name}'); skipped += 1; continue
                artist, title = pair
                try:
                    artist_mbids = {}
                    verified_artists = set()
                    if mb_resolver:
                        # Resolve every credited artist, not just the first one.
                        # Each artist uses the same song for disambiguation.
                        for artist_name in dict.fromkeys(artist_parts(artist) or [artist]):
                            try:
                                mbid_result = mb_resolver.resolve(artist_name, title)
                                if mbid_result and mbid_result.get('mbid'):
                                    artist_mbids[artist_name] = mbid_result['mbid']
                                    if mbid_result.get('match_status') in ('unique', 'track_verified'):
                                        verified_artists.add(artist_name)
                                    print(f'[ARTIST MBID] {artist_name} | {mbid_result["mbid"]} | {mbid_result.get("match_status", "unknown")}')
                            except (requests.RequestException, ValueError, OSError) as exc:
                                print(f'[MBID WARN] {artist_name} | {exc}; continuing')
                    emit('musicvideo', 'Musikvideo-Metadaten abrufen', file=video.name,
                         index=file_index, total=len(files))
                    track = get_track(session, artist, title, keys.get('TheAudioDB', ''), timeout, settings.get('MatchThreshold', .80))
                    if not track and settings.get('UseMusicBrainz', False):
                        track = musicbrainz_track(session, artist, title, timeout, settings.get('MatchThreshold', .80), settings.get('MusicBrainzUserAgent'))
                    lastfm = None
                    if settings.get('UseLastFM', True) and keys.get('LastFM'):
                        lastfm = lastfm_track(session, artist, title, keys['LastFM'], timeout,
                                              settings.get('MatchThreshold', .80))
                    if not track and lastfm:
                        track = lastfm_as_track(lastfm, artist, title)
                    if not track:
                        print(f'[WARN] No reliable match: {artist} - {title}; writing basic NFO')
                        track = {}
                    lastfm = lastfm or {}
                    plot = choose_plot(session, track, lastfm, artist, cfg, settings, keys, timeout)
                    emit('musicvideo', 'NFO erstellen', file=video.name,
                         index=file_index, total=len(files))
                    tree = make_nfo(track, artist, title, plot, lastfm, artist_mbids)
                    ET.indent(tree, space='  ')
                    tree.write(nfo, encoding='utf-8', xml_declaration=True, short_empty_elements=False)
                    written += 1
                    print(f'[NFO] {nfo.name} | {artist} - {title}')
                    if completed_paths is not None: completed_paths.append(video)
                    # Shared Kodi artist metadata: only for a track-verified artist.
                    # Existing artist.nfo is never modified, even if overwrite is on.
                    if artist_base:
                        primary = (artist_parts(artist) or [artist])[0]
                        if primary in verified_artists:
                            central = _artist_folder(artist_base, primary, artist_mbids.get(primary))
                            if central:
                                try:
                                    create_artist_nfo(session, central, primary, artist_mbids[primary],
                                        audiodb_key=keys.get('TheAudioDB', ''),
                                        lastfm_key=keys.get('LastFM', ''),
                                        language=cfg.get('Language', 'de'), settings=settings,
                                        timeout=timeout,
                                        user_agent=settings.get('MusicBrainzUserAgent'))
                                except (OSError, ValueError, requests.RequestException) as exc:
                                    print(f'[ARTIST NFO WARN] {primary}: {exc}')
                        else:
                            print(f'[ARTIST NFO] Skipped unverified artist: {primary}')
                    emit('musicvideo', 'Künstlergrafiken und Thumbnail prüfen', file=video.name,
                         index=file_index, total=len(files))
                    try: artworks += save_art(session, track, video, cfg, timeout, lastfm, artist, keys.get('TheAudioDB', ''), artist_mbids, fanart_key, artist_base, verified_artists)
                    except (OSError, requests.RequestException) as exc:
                        print(f'[WARN] Artwork failed for {video.name}: {exc}')
                except (requests.RequestException, ValueError, OSError, ET.ParseError) as exc:
                    errors += 1
                    print(f'[ERROR] {video.name}: {exc}')
        emit('musicvideo', 'Scraper abgeschlossen', index=len(files), total=len(files), percent=100)
        print(f'[SUMMARY] {written} NFO, {artworks} artworks, {skipped} skipped, {errors} errors')
        return 1 if errors else 0

if __name__ == '__main__':
    from log_manager import run_with_file_logging
    sys.exit(run_with_file_logging(main, sys.argv[1:], scraper_name='musicvideo'))
