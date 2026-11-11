#!/usr/bin/env python3
"""Media-Buddy V2 movie scraper: ChaptersDB movie chapters + resilient trailer download via yt-dlp Python API."""
from __future__ import annotations
from linked_media_walk import linked_files
import re
import html
import time
import sys
from ffmpeg_manager import discover, FFmpegError
import fnmatch
import xml.etree.ElementTree as ET
from pathlib import Path
import requests
import os
from urllib.parse import urlsplit, unquote
from config_manager import ConfigManager, ConfigError
from artwork_naming import names_for
from chapter_downloader import download_movie_chapters
from translation_manager import translate_plot
from api_keys import get_api_key

TMDB_API_KEY = get_api_key("TMDB")
BASE = 'https://api.themoviedb.org/3'
TMDB_ID = re.compile(r'(?<![A-Za-z0-9])TMDB-(\d+)(?!\d)', re.I)
IMDB_ID = re.compile(r'(?<![A-Za-z0-9])(tt\d{7,})(?!\d)', re.I)
YEAR = re.compile(r'(?<!\d)[(\[]((?:19|20)\d{2})[)\]]')


def parse_args(args):
    folder = None
    kind = None
    for arg in args:
        if arg.lower().startswith('--type:') or arg.lower().startswith('--type='):
            kind = re.split('[:=]', arg, maxsplit=1)[1].lower()
        elif arg.lower().startswith('--folder:') or arg.lower().startswith('--folder='):
            folder = re.split('[:=]', arg, maxsplit=1)[1].strip('"')
        elif not arg.startswith('--') and folder is None:
            folder = arg
        else:
            raise ValueError(f'Unknown argument: {arg}')
    if kind not in (None, 'movies'):
        raise ValueError('This scraper only accepts --type:movies')
    if not folder:
        raise ValueError('Please supply a movie folder')
    return Path(folder)


def identify_filename(stem, ignored):
    tmdb = TMDB_ID.search(stem)
    imdb = IMDB_ID.search(stem)
    if tmdb:
        return 'tmdb', tmdb.group(1), None
    if imdb:
        cleaned = IMDB_ID.sub(' ', stem)
        return 'imdb', imdb.group(1).lower(), cleaned.strip()
    return 'title', None, stem


def title_and_year(raw, ignored):
    raw = TMDB_ID.sub(' ', IMDB_ID.sub(' ', raw))
    year = YEAR.search(raw)
    value = YEAR.sub(' ', raw)
    for word in ignored:
        if word:
            value = re.sub(r'(?<!\w)' + re.escape(word) + r'(?!\w)', ' ', value, flags=re.I)
    value = re.sub(r'[._]+', ' ', value)
    value = re.sub(r'\s+', ' ', value).strip(' -()[]')
    return value, year.group(1) if year else None


class TMDB:
    def __init__(self, timeout, language):
        self.timeout = timeout
        self.language = language
        self.session = requests.Session()

    def get(self, endpoint, **params):
        response = self.session.get(BASE + endpoint, params={'api_key': TMDB_API_KEY, **params}, timeout=self.timeout)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def movie(self, movie_id):
        return self.get(f'/movie/{movie_id}', language=self.language)

    def localized_movie(self, movie_id, language):
        return self.get(f'/movie/{movie_id}', language=language)

    def details(self, movie_id):
        return self.get(f'/movie/{movie_id}', language=self.language,
                        append_to_response="credits,release_dates,external_ids")

    def collection(self, collection_id):
        return self.get(f'/collection/{collection_id}', language=self.language)

    def from_imdb(self, imdb_id):
        result = self.get(f'/find/{imdb_id}', external_source='imdb_id', language=self.language)
        return (result or {}).get('movie_results', [])

    def search(self, title, year):
        params = {'query': title, 'language': self.language}
        if year:
            params['year'] = year
        result = self.get('/search/movie', **params)
        return (result or {}).get('results', [])


def select_movie(client, stem, ignored):
    mode, ident, title_source = identify_filename(stem, ignored)
    if mode == 'tmdb':
        return mode, client.movie(ident), f'TMDB-{ident}'
    if mode == 'imdb':
        matches = client.from_imdb(ident)
        if matches:
            return mode, client.movie(matches[0]['id']), ident
        if not title_source:
            return 'imdb-unresolved', None, ident
    title, year = title_and_year(title_source, ignored)
    if not title:
        return 'title-unresolved', None, stem
    results = client.search(title, year)
    if not results:
        return 'title-unresolved', None, title
    # Do not silently accept an unrelated first search result.
    from difflib import SequenceMatcher
    def score(item):
        candidate = (item.get('title') or '').casefold()
        original = (item.get('original_title') or '').casefold()
        similarity = max(SequenceMatcher(None, title.casefold(), candidate).ratio(),
                         SequenceMatcher(None, title.casefold(), original).ratio())
        if year and str(item.get('release_date', '')).startswith(year):
            similarity += 0.12
        return similarity
    best = max(results, key=score)
    if score(best) < 0.80:
        return 'title-uncertain', None, title
    return 'title', client.movie(best['id']), title


def translate_movie_text(text, source, target, enabled, timeout, field):
    """Movie translation adapter for the shared translation manager."""
    if not enabled or not text or not source or source == target:
        return text
    result = translate_plot(text, source, target, timeout=timeout)
    if result.success:
        print(f'[TRANSLATE] {field}: {source} -> {target}')
    else:
        print(f'[TRANSLATE WARN] {field}: source text retained ({result.error})')
    return result.text


def localized_movie_texts(client, details, target_language, enabled, timeout):
    """Resolve plot and tagline separately: localized, English, original language."""
    target = target_language.lower().split('-', 1)[0]
    original = str(details.get('original_language') or '').strip().lower()
    languages = []
    for code in (target, 'en', original):
        if re.fullmatch(r'[a-z]{2}', code) and code not in languages:
            languages.append(code)
    records = {target: details}
    output = dict(details)
    for field in ('overview', 'tagline'):
        for language in languages:
            if language not in records:
                try:
                    records[language] = client.localized_movie(details['id'], language)
                except requests.RequestException as exc:
                    print(f'[TEXT WARN] {field}: TMDB {language} unavailable ({exc})')
                    records[language] = None
            value = str((records[language] or {}).get(field) or '').strip()
            if value:
                output[field] = translate_movie_text(value, language, target, enabled, timeout, field)
                if language != target:
                    print(f'[TEXT FALLBACK] {field}: {language} -> {target}' + (' (translation enabled)' if enabled else ' (original text)'))
                break
        else:
            output[field] = ''
    return output


def add_text(parent, tag, value):
    if value is not None and str(value).strip():
        ET.SubElement(parent, tag).text = str(value).strip()


def movie_nfo(movie, collection=None):
    root = ET.Element('movie')
    title = movie.get('title') or movie.get('original_title')
    release = movie.get('release_date') or ''
    add_text(root, 'title', title)
    add_text(root, 'sorttitle', title)
    add_text(root, 'originaltitle', movie.get('original_title'))
    add_text(root, 'plot', movie.get('overview'))
    add_text(root, 'premiered', release)
    add_text(root, 'aired', release)
    # Always include editable set fields, even for standalone movies.
    node = ET.SubElement(root, 'set')
    ET.SubElement(node, 'name').text = str((collection or {}).get('name') or '')
    ET.SubElement(node, 'overview').text = str((collection or {}).get('overview') or '')
    ET.SubElement(root, 'collectionnumber').text = str((collection or {}).get('id') or '')
    add_text(root, 'tagline', movie.get('tagline'))
    add_text(root, 'year', release[:4])
    rating = movie.get('vote_average')
    if rating is not None:
        add_text(root, 'rating', round(float(rating), 1))
    # Kodi uses directors and writing credits, not arbitrary production crew.
    crew = (movie.get('credits') or {}).get('crew', [])
    directors = set()
    writers = set()
    for person in crew:
        name = (person.get('name') or '').strip()
        if not name:
            continue
        job = (person.get('job') or '').casefold()
        department = (person.get('department') or '').casefold()
        if job == 'director' and name not in directors:
            add_text(root, 'director', name)
            directors.add(name)
        if (job in {'writer', 'screenplay', 'story', 'screenstory', 'screen story',
                    'adaptation', 'teleplay', 'scenario'} or
                (department == 'writing' and job not in {'characters', 'original characters', 'novel', 'book'})) and name not in writers:
            add_text(root, 'credits', name)
            writers.add(name)
    runtime = movie.get('runtime')
    ET.SubElement(root, 'runtime').text = str(runtime) if runtime is not None else ''
    ET.SubElement(root, 'language').text = str(movie.get('original_language') or '')
    add_text(root, 'votes', movie.get('vote_count'))
    if movie.get('id'):
        # V1 compatibility: omit default="true"; retain TMDB uniqueid.
        ET.SubElement(root, 'uniqueid', {'type': 'tmdb'}).text = str(movie['id'])
    imdb = movie.get('imdb_id') or (movie.get('external_ids') or {}).get('imdb_id')
    if imdb:
        ET.SubElement(root, 'uniqueid', {'type': 'imdb'}).text = imdb
    # TMDB release_dates supplies the actual German certification, not the US MPAA label.
    releases = (movie.get('release_dates') or {}).get('results') or []
    german = next((r for r in releases if r.get('iso_3166_1') == 'DE'), {})
    certs = [str(r.get('certification', '')).strip() for r in german.get('release_dates', [])
             if str(r.get('certification', '')).strip()]
    if certs:
        add_text(root, 'mpaa', 'FSK ' + certs[0])
    add_text(root, 'status', movie.get('status'))
    for genre in movie.get('genres') or []:
        add_text(root, 'genre', genre.get('name'))
    for studio in movie.get('production_companies') or []:
        add_text(root, 'studio', studio.get('name'))
    for country in movie.get('production_countries') or []:
        add_text(root, 'country', country.get('name'))
    for person in (movie.get('credits') or {}).get('cast', []):
        if not person.get('name'):
            continue
        actor = ET.SubElement(root, 'actor')
        add_text(actor, 'name', person['name'])
        add_text(actor, 'role', person.get('character'))
        if person.get('profile_path'):
            add_text(actor, 'thumb', 'https://image.tmdb.org/t/p/original' + person['profile_path'])
    ET.indent(root, space='  ')
    return ET.ElementTree(root)


def read_movie_nfo_tolerant(nfo: Path) -> ET.ElementTree:
    """Read legacy NFOs with bare ampersands without rewriting existing files."""
    try:
        return ET.parse(nfo)
    except ET.ParseError as first_error:
        original = nfo.read_text(encoding='utf-8-sig')
        repaired = re.sub(
            r"&(?!#(?:[0-9]+|[xX][0-9A-Fa-f]+);|[A-Za-z_][A-Za-z0-9_.:-]*;)",
            "&amp;", original,
        )
        root = ET.fromstring(repaired)
        print(f"[WARN] movie NFO has invalid XML ({first_error}); bare '&' repaired in memory")
        return ET.ElementTree(root)


def movie_ids_from_nfo(tree):
    """Read identifiers from the already-associated, same-stem movie NFO."""
    root = tree.getroot()
    ids = {}
    for node in root.findall('uniqueid'):
        kind = (node.get('type') or '').lower()
        value = (node.text or '').strip()
        if kind == 'tmdb' and value.isdigit():
            ids['tmdb'] = value
        elif kind == 'imdb' and IMDB_ID.fullmatch(value):
            ids['imdb'] = value.lower()
    # Legacy Kodi NFOs may use these direct fields.
    for kind in ('tmdb', 'imdb'):
        if kind not in ids:
            value = (root.findtext(kind + 'id') or '').strip()
            if kind == 'tmdb' and value.isdigit():
                ids[kind] = value
            elif kind == 'imdb' and IMDB_ID.fullmatch(value):
                ids[kind] = value.lower()
    return ids


def select_movie_with_nfo(client, stem, ignored, tree):
    """Prefer NFO IDs over a title search; never alter the stored NFO."""
    if tree is not None:
        ids = movie_ids_from_nfo(tree)
        if ids.get('tmdb'):
            movie = client.movie(ids['tmdb'])
            if movie:
                return 'nfo-tmdb', movie, 'TMDB-' + ids['tmdb']
        if ids.get('imdb'):
            matches = client.from_imdb(ids['imdb'])
            if matches:
                movie = client.movie(matches[0]['id'])
                if movie:
                    return 'nfo-imdb', movie, ids['imdb']
    return select_movie(client, stem, ignored)


def write_nfo(tree, target):
    # Write completely before replacing destination to avoid partially written NFO files.
    import os
    import tempfile
    fd, temp = tempfile.mkstemp(prefix='.mb_nfo_', suffix='.tmp', dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            tree.write(stream, encoding='utf-8', xml_declaration=True, short_empty_elements=False)
        os.replace(temp, target)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)



# Same placeholder grammar and tag lookup rules as the production series scraper.
NFO_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")
NFO_TAG_QUERY = re.compile(
    r'''^([A-Za-z_][\w.-]*)(?:\s+([A-Za-z_][\w.-]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s{}"']+)))?$'''
)


def nfo_tag_value(root, expression):
    if root is None:
        return ''
    match = NFO_TAG_QUERY.fullmatch(expression.strip())
    if not match:
        return ''
    tag, attr, double, single, unquoted = match.groups()
    expected = next((v for v in (double, single, unquoted) if v is not None), '')
    for child in root:
        if child.tag.lower() != tag.lower():
            continue
        if attr and child.attrib.get(attr) != expected:
            continue
        value = (child.text or '').strip()
        if value:
            return value
    return ''


def clean_filename(value, replacement):
    if not isinstance(replacement, str) or re.search(r'[<>:"/\\|?*\x00-\x1f]', replacement):
        raise ConfigError('ReplaceChar contains forbidden filename characters')
    value = str(value or '').replace('–', '-').replace('—', '-').replace('−', '-')
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', lambda _: replacement, value)
    value = re.sub(r'\s+', ' ', value).strip(' .')
    if value.upper().split('.')[0] in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1,10)), *(f'LPT{i}' for i in range(1,10))}:
        value = '_' + value
    return value


def target_movie_parts(nfo_root, pattern, replacement):
    # Separate folder components BEFORE filename sanitation.
    components = re.split(r'[/\\]', pattern)
    if any(not c.strip() for c in components):
        raise ConfigError('Movie rename pattern has an empty path component')
    result = []
    for component in components:
        raw = NFO_PLACEHOLDER.sub(lambda m: nfo_tag_value(nfo_root, m.group(1)), component)
        raw = re.sub(r'\s+[-–—]\s+(?=[-–—])', ' ', raw)
        raw = re.sub(r'(?:\s+[-–—:]\s*)+$', '', raw)
        raw = re.sub(r'^\s*[-–—:]\s+', '', raw)
        cleaned = clean_filename(raw, replacement)
        if cleaned in ('', '.', '..'):
            raise ValueError(f'Empty/invalid component in rename pattern: {component!r}')
        result.append(cleaned)
    return result


def rename_movie(video, nfo, root, nfo_root, pattern, replacement):
    parts = target_movie_parts(nfo_root, pattern, replacement)
    # Directory-producing patterns are anchored to the scan root, not the
    # current video directory: repeated runs must be idempotent.
    # Never replace another movie, or silently suffix the user-defined title.
    target_dir = root.joinpath(*parts[:-1]) if len(parts) > 1 else video.parent
    destination = target_dir / (parts[-1] + video.suffix)
    destination_nfo = target_dir / (parts[-1] + '.nfo')
    if destination == video and destination_nfo == nfo:
        return video, nfo, False
    if (destination.exists() and destination != video) or (destination_nfo.exists() and destination_nfo != nfo):
        raise FileExistsError(f'Rename target already exists: {destination} or {destination_nfo}')
    # Never move the video without its NFO; check all conditions first.
    if not nfo.is_file():
        raise FileNotFoundError(f'NFO missing: {nfo}')
    target_dir.mkdir(parents=True, exist_ok=True)
    # Move NFO first; roll it back if video move fails.
    if destination_nfo != nfo:
        nfo.rename(destination_nfo)
    try:
        if destination != video:
            video.rename(destination)
    except OSError:
        if destination_nfo != nfo:
            destination_nfo.rename(nfo)
        raise
    return destination, destination_nfo, True


# Artwork targets follow Kodi naming conventions. The existing film artwork
# folder and the no-subfolder (long name) mode are handled separately.
ARTWORK_SPECS = {
    'poster': ('Image_Poster', 'Image_Poster', 'poster.jpg', 'movieposter'),
    'fanart': ('Image_Backdrop', 'Image_Fanart', 'fanart.jpg', 'moviebackground'),
    'landscape': ('Image_Thumb', 'Image_Thumb', 'landscape.jpg', 'moviethumb'),
    'banner': ('Image_Banner', 'Image_Banner', 'banner.jpg', 'moviebanner'),
    'clearlogo': ('Image_ClearLogo', 'Image_ClearLogo', 'clearlogo.png', 'hdmovielogo'),
    'clearart': ('Image_ClearArt', 'Image_ClearArt', 'clearart.png', 'hdmovieclearart'),
    'discart': ('Image_Disc', 'Image_Disc', 'discart.png', 'moviedisc'),
    'keyart': ('Image_Keyart', 'Image_Keyart', 'keyart.jpg', 'movieposter'),
}


def choose_fanart(items):
    if not isinstance(items, list):
        return None
    usable = [x for x in items if isinstance(x, dict) and x.get('url')]
    if not usable:
        return None
    # Prefer German, then neutral, then English, followed by community likes.
    def score(x):
        lang = (x.get('lang') or '').lower()
        return ({'de': 3, '00': 2, 'en': 1}.get(lang, 0), int(x.get('likes') or 0))
    return max(usable, key=score)['url']


def artwork_target(video, kind, filename, folder_mode):
    if folder_mode:
        return video.parent / filename
    return video.parent / (video.stem + '-' + filename)


def save_artwork(session, url, target, overwrite, timeout):
    if target.exists() and not overwrite:
        return 'existing'
    temp = target.with_name(target.name + '.mb-download')
    try:
        with session.get(url, stream=True, timeout=timeout) as response:
            response.raise_for_status()
            with temp.open('wb') as output:
                for chunk in response.iter_content(chunk_size=131072):
                    if chunk:
                        output.write(chunk)
        if not temp.stat().st_size:
            raise ValueError('Empty artwork response')
        temp.replace(target)
        return 'written'
    finally:
        temp.unlink(missing_ok=True)


def download_movie_artwork(client, movie_id, video, config, folder_mode, timeout):
    if config.get('Download_Images', False) is not True:
        return 0
    switches = config.section('Movies')
    filenames = config.section('Filenaming').get('Movie', {})
    overwrite = config.section('Overwrite').get('Artwork', False) is True
    # Only request remote artwork when at least one enabled target is missing
    # or overwriting is explicitly allowed.
    pending = {}
    for kind, (switch, name_key, default_name, _) in ARTWORK_SPECS.items():
        if switches.get(switch, False) is not True:
            continue
        profile_names = names_for(kind)
        name = profile_names[0] if profile_names else str(filenames.get(name_key, default_name))
        if not name or Path(name).name != name:
            raise ConfigError(f'Invalid artwork filename: {name!r}')
        target = artwork_target(video, kind, name, folder_mode)
        if target.exists() and not overwrite:
            for alt in profile_names:
                alt_target=artwork_target(video, kind, alt, folder_mode)
                if alt_target != target and not alt_target.exists():
                    import shutil
                    shutil.copyfile(target, alt_target)
        if not target.exists() or overwrite:
            pending[kind] = target
    if not pending:
        return 0
    sources = {}
    # fanart.tv supports movie-specific formats absent from TMDB.
    FANART_TV_API_KEY = get_api_key("FANART_TV")
    try:
        response = client.session.get(f'https://webservice.fanart.tv/v3/movies/{movie_id}',
                                      params={'api_key': FANART_TV_API_KEY}, timeout=timeout)
        if response.ok:
            data = response.json()
            for kind in pending:
                key = ARTWORK_SPECS[kind][3]
                candidates = data.get(key, [])
                if kind == 'clearlogo':
                    candidates = candidates or data.get('movielogo', [])
                if kind == 'clearart':
                    candidates = candidates or data.get('movieart', [])
                sources[kind] = choose_fanart(candidates)
        elif response.status_code != 404:
            print(f'[ART WARN] fanart.tv HTTP {response.status_code}')
    except (requests.RequestException, ValueError) as exc:
        print(f'[ART WARN] fanart.tv: {exc}')
    # TMDB fallback for poster, backdrop, logo, and still images.
    missing = [k for k in pending if not sources.get(k)]
    if missing:
        try:
            data = client.get(f'/movie/{movie_id}/images', include_image_language='de,en,null') or {}
            keys = {'poster': 'posters', 'keyart': 'posters', 'fanart': 'backdrops',
                    'landscape': 'backdrops', 'clearlogo': 'logos'}
            for kind in missing:
                images = data.get(keys.get(kind, ''), [])
                if images:
                    images = sorted(images, key=lambda x: (
                        {'de': 3, 'en': 2, None: 1}.get(x.get('iso_639_1'), 0),
                        x.get('vote_average') or 0, x.get('width') or 0), reverse=True)
                    path = images[0].get('file_path')
                    if path:
                        sources[kind] = 'https://image.tmdb.org/t/p/original' + path
        except (requests.RequestException, ValueError) as exc:
            print(f'[ART WARN] TMDB images: {exc}')
    written = 0
    for kind, target in pending.items():
        url = sources.get(kind)
        if not url:
            print(f'[ART MISSING] {kind}: no online image for TMDB-{movie_id}')
            continue
        try:
            status = save_artwork(client.session, url, target, overwrite, timeout)
            if status == 'written':
                written += 1
                for alt in names_for(kind):
                    alt_target=artwork_target(video, kind, alt, folder_mode)
                    if alt_target != target and (overwrite or not alt_target.exists()):
                        import shutil
                        shutil.copyfile(target, alt_target)
                        written += 1
                print(f'[ART WRITE] {target}')
        except (requests.RequestException, OSError, ValueError) as exc:
            print(f'[ART WARN] {target.name}: {exc}')
    return written



# Central movie-set artwork. The base directory must exist: never create an
# accidental parallel library if a network share is offline or misconfigured.
MOVIESET_SPECS = {
    # Reuse movie artwork switches and filenames, no separate movie-set selection.
    'poster': ('Image_Poster', 'poster.jpg', 'moviessetposter'),
    'fanart': ('Image_Backdrop', 'fanart.jpg', 'moviessetfanart'),
    'banner': ('Image_Banner', 'banner.jpg', 'moviessetbanner'),
    'clearlogo': ('Image_ClearLogo', 'clearlogo.png', 'moviessetlogo'),
    'clearart': ('Image_ClearArt', 'clearart.png', 'moviessetclearart'),
    'landscape': ('Image_Thumb', 'landscape.jpg', 'moviessetthumb'),
    'discart': ('Image_Disc', 'discart.png', 'moviessetdisc'),
    'keyart': ('Image_Keyart', 'keyart.jpg', 'moviessetkeyart'),
}


def movieset_base(config):
    raw = str(config.get('Movieset_Artwork', '') or '').strip()
    if not raw:
        return None
    if raw.lower().startswith('smb://'):
        parsed = urlsplit(raw)
        if not parsed.hostname or not parsed.path.strip('/'):
            raise ConfigError('Movieset_Artwork: invalid SMB URL')
        parts = [unquote(x) for x in parsed.path.strip('/').split('/')]
        if any(x in ('', '.', '..') or '/' in x or '\\' in x for x in parts):
            raise ConfigError('Movieset_Artwork: invalid SMB path')
        # On Windows UNC paths work natively. On other systems the SMB URL
        # uses the optional smbprotocol/smbclient package for remote access.
        unc = '\\\\' + parsed.hostname + '\\' + '\\'.join(parts)
        if os.name == 'nt':
            return ('local', Path(unc))
        try:
            import smbclient
        except ImportError as exc:
            raise ConfigError('SMB URL on Linux requires smbprotocol (pip install smbprotocol), '
                              'or configure an existing local CIFS mount') from exc
        return ('smb', (smbclient, unc))
    if raw.lower().startswith(('http://', 'https://')):
        raise ConfigError('Movieset_Artwork must be a filesystem or SMB path')
    if os.name != 'nt' and raw.startswith('\\\\'):
        raise ConfigError('Windows UNC path on Linux: use smb:// or a mounted CIFS path')
    return ('local', Path(raw).expanduser())


def movieset_location(base, collection_name, replacement):
    name = clean_filename(collection_name, replacement)
    if not name or name in ('.', '..'):
        raise ConfigError('Invalid movie-set name')
    mode, path = base
    if mode == 'local':
        if not path.is_dir():
            raise ConfigError(f'Movieset_Artwork base directory unavailable: {path}')
        return ('local', path / name)
    smb, unc = path
    if not smb.path.isdir(unc):
        raise ConfigError(f'Movieset_Artwork SMB base directory unavailable: {unc}')
    return ('smb', (smb, unc.rstrip('\\') + '\\' + name))


def set_file_exists(location, filename):
    mode, target = location
    if mode == 'local':
        return (target / filename).is_file()
    smb, unc = target
    return smb.path.isfile(unc + '\\' + filename)


def set_write_file(session, url, location, filename, overwrite, timeout):
    mode, target = location
    if mode == 'local':
        target.mkdir(exist_ok=True)
        return save_artwork(session, url, target / filename, overwrite, timeout)
    smb, unc = target
    if not smb.path.isdir(unc):
        smb.mkdir(unc)
    dest = unc + '\\' + filename
    if smb.path.isfile(dest) and not overwrite:
        return 'existing'
    temp = dest + '.mb-download'
    try:
        with session.get(url, stream=True, timeout=timeout) as response:
            response.raise_for_status()
            with smb.open_file(temp, mode='wb') as stream:
                for chunk in response.iter_content(chunk_size=131072):
                    if chunk:
                        stream.write(chunk)
        if overwrite and smb.path.isfile(dest):
            smb.remove(dest)
        smb.rename(temp, dest)
        return 'written'
    finally:
        if smb.path.exists(temp):
            smb.remove(temp)


def download_movieset_artwork(client, movie_id, collection_id, collection_name, config, base, replacement, timeout):
    if not base or config.get('Download_Images', False) is not True or config.get('Download_Movieset_Images', False) is not True:
        return 0
    switches = config.section('Movies')
    names = config.section('Filenaming').get('Movie', {})
    if not isinstance(names, dict):
        raise ConfigError('Filenaming.Movie must be an object')
    overwrite = config.section('Overwrite').get('Artwork', False) is True
    location = movieset_location(base, collection_name, replacement)
    pending = {}
    for kind, (switch, default, _) in MOVIESET_SPECS.items():
        if switches.get(switch, False) is not True:
            continue
        filename = str(names.get(switch, default))
        if not filename or Path(filename).name != filename or '/' in filename or '\\' in filename or filename in ('.', '..'):
            raise ConfigError(f'Invalid movie-set artwork filename: {filename!r}')
        if not set_file_exists(location, filename) or overwrite:
            pending[kind] = filename
    if not pending:
        print(f'[SET ART SKIP] {collection_name}: all enabled artwork already exists')
        return 0
    print(f'[SET ART CHECK] {collection_name} (TMDB-{collection_id}): missing or replaceable: {", ".join(pending)}')
    sources = {}
    FANART_TV_API_KEY = get_api_key("FANART_TV")
    # fanart.tv collection IDs are served by /v3/movies/{collection_id}.
    # Their artwork uses the ordinary movie field names (not moviesset*).
    # Never use /movies/{movie_id} as a fallback: that is individual film art.
    collection_fields = {
        'poster': ('movieposter',),
        'fanart': ('moviebackground',),
        'banner': ('moviebanner',),
        'clearlogo': ('hdmovielogo', 'movielogo'),
        'clearart': ('hdmovieclearart', 'movieart'),
        'landscape': ('moviethumb',),
        'discart': ('moviedisc',),
        'keyart': (),
    }
    try:
        response = client.session.get(f'https://webservice.fanart.tv/v3/movies/{collection_id}',
                                      params={'api_key': FANART_TV_API_KEY}, timeout=timeout)
        if response.ok:
            data = response.json()
            if isinstance(data, dict):
                available = sorted(k for k, v in data.items() if isinstance(v, list) and v)
                print(f'[SET ART DIAG] fanart.tv collection {collection_id}: available image fields: ' +
                      (', '.join(available) or 'none'))
                for kind in pending:
                    for field in collection_fields[kind]:
                        url = choose_fanart(data.get(field, []))
                        if url:
                            sources[kind] = url
                            break
            else:
                print(f'[SET ART WARN] fanart.tv collection {collection_id}: unexpected response')
        else:
            print(f'[SET ART DIAG] fanart.tv collection {collection_id}: HTTP {response.status_code}')
    except (requests.RequestException, ValueError) as exc:
        print(f'[SET ART WARN] fanart.tv: {exc}')
    # TMDB collection images support posters and backdrops.
    missing = [kind for kind in pending if not sources.get(kind)]
    if missing:
        try:
            data = client.get(f'/collection/{collection_id}/images', include_image_language='de,en,null') or {}
            keys = {'poster': 'posters', 'fanart': 'backdrops', 'landscape': 'backdrops', 'clearlogo': 'logos'}
            print(f'[SET ART DIAG] TMDB collection {collection_id}: available image fields: ' +
                  (', '.join(sorted(k for k, v in data.items() if isinstance(v, list) and v)) or 'none'))
            for kind in missing:
                images = data.get(keys.get(kind, ''), [])
                if images:
                    images = sorted(images, key=lambda x: (
                        {'de': 3, 'en': 2, None: 1}.get(x.get('iso_639_1'), 0),
                        x.get('vote_average') or 0, x.get('width') or 0), reverse=True)
                    path = images[0].get('file_path')
                    if path:
                        sources[kind] = 'https://image.tmdb.org/t/p/original' + path
        except (requests.RequestException, ValueError) as exc:
            print(f'[SET ART WARN] TMDB images: {exc}')
    written = 0
    for kind, filename in pending.items():
        if not sources.get(kind):
            print(f'[SET ART MISSING] {kind}: no online image for collection TMDB-{collection_id}')
            continue
        try:
            status = set_write_file(client.session, sources[kind], location, filename, overwrite, timeout)
            if status == 'written':
                written += 1
                print(f'[SET ART WRITE] {collection_name} / {filename}')
        except (requests.RequestException, OSError, ValueError) as exc:
            print(f'[SET ART WARN] {collection_name} / {filename}: {exc}')
    return written


def nfo_collection_info(tree):
    root = tree.getroot()
    number = (root.findtext('collectionnumber') or '').strip()
    name = (root.findtext('set/name') or '').strip()
    if number.isdecimal() and name:
        return int(number), name
    return None

def _ffmpeg_location(cfg):
    """Find a usable FFmpeg installation using shared configuration and PATH."""
    try:
        return str(discover().ffmpeg.parent)
    except FFmpegError as exc:
        print(f'[TRAILER WARN] FFmpeg unavailable: {exc}')
        return None


def download_movie_trailer(client, movie_id, video, cfg):
    """Return (written, errors); retain partial downloads for subsequent retries."""
    if cfg.section('Movies').get('Trailer_Download', False) is not True:
        return 0, 0
    stem = video.stem
    def completed():
        return [p for p in video.parent.iterdir() if p.is_file() and
                p.stem.casefold() == (stem + '-trailer').casefold() and
                p.suffix.casefold() in ('.webm', '.mp4', '.mkv')]
    existing = completed()
    if existing:
        print(f'[TRAILER SKIP] {video.name}: already exists ({existing[0].name})')
        return 0, 0
    try:
        import yt_dlp
    except ImportError:
        print('[TRAILER ERROR] yt-dlp not installed; run: python -m pip install -U yt-dlp')
        return 0, 1
    data = client.get(f'/movie/{movie_id}/videos', language='de-DE') or {}
    german = data.get('results') or []
    data_en = client.get(f'/movie/{movie_id}/videos', language='en-US') or {}
    english = data_en.get('results') or []
    candidates = {(v.get('site'), v.get('key')): v for v in english + german
                  if v.get('site') == 'YouTube' and v.get('key') and v.get('type') in ('Trailer', 'Teaser')}
    if not candidates:
        print(f'[TRAILER MISSING] {video.name}: no YouTube trailer on TMDB')
        return 0, 0
    def rank(v):
        lang = v.get('iso_639_1')
        return (0 if lang == 'de' else 1 if lang == 'en' else 2,
                0 if v.get('type') == 'Trailer' else 1,
                0 if v.get('official') is True else 1)
    choice = min(candidates.values(), key=rank)
    url = 'https://www.youtube.com/watch?v=' + choice['key']
    ffmpeg_location = _ffmpeg_location(cfg)
    options = {
        'outtmpl': str(video.parent / (stem + '-trailer.%(ext)s')),
        'format': 'bv*+ba/b' if ffmpeg_location else 'best[vcodec!=none][acodec!=none]',
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'retries': 5,
        'fragment_retries': 5,
        'file_access_retries': 3,
        'continuedl': True,
        'restrictfilenames': False,
        'overwrites': False,
    }
    if ffmpeg_location:
        options['ffmpeg_location'] = ffmpeg_location
        options['merge_output_format'] = 'mkv'
    else:
        print(f'[TRAILER INFO] {video.name}: FFmpeg unavailable, using combined video/audio format')
    # Recreate the downloader once after a failed transfer. This refreshes
    # YouTube's expiring stream URLs; yt-dlp may resume an existing .part file.
    for attempt in (1, 2):
        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                downloader.download([url])
            created = completed()
            if created:
                print(f'[TRAILER WRITE] {created[0]}')
                return 1, 0
            print(f'[TRAILER ERROR] {video.name}: no completed supported output file')
        except Exception as exc:
            print(f'[TRAILER WARN] {video.name}: attempt {attempt}/2: {type(exc).__name__}: {exc}')
        if attempt == 1:
            print(f'[TRAILER RETRY] {video.name}: refreshing download URL; keeping partial files')
    print(f'[TRAILER ERROR] {video.name}: download failed after retries')
    return 0, 1


def main(args, *, videos_override=None, wizard_overrides=None, completed_paths=None):
    try:
        root = parse_args(args)
        cfg = ConfigManager(Path(__file__).resolve().parent, 'movie_scraper_config.json')
        if wizard_overrides:
            from config_manager import _merge
            cfg.effective = _merge(cfg.effective, wizard_overrides)
        if not root.is_dir():
            raise ValueError(f'Folder does not exist: {root}')
        patterns = cfg.get('FileExtensions', ['*.mkv', '*.mp4', '*.stub'])
        ignored_dirs = set(x.casefold() for x in cfg.get('FolderIgnore', []))
        ignored_words = cfg.get('FileIgnoreStrings', [])
        language = cfg.get('Language', 'de')
        country = cfg.get('Country', 'DE')
        translate_missing = cfg.section('Movies').get('TranslateMissingTexts', False) is True
        overwrite = bool(cfg.section('Overwrite').get('NFO', False))
        naming = cfg.section('Filenaming').get('Rename', {})
        if not isinstance(naming, dict):
            raise ConfigError('Filenaming.Rename must be an object')
        rename_enabled = naming.get('RenameFiles', True) is True
        rename_pattern = str(naming.get('Movie') or '{Title} ({Year})\\{Title}')
        replacement = cfg.get('ReplaceChar', ' ')
        # Validate before any file operations.
        target_movie_parts(ET.fromstring('<movie><title>Example</title><year>2000</year></movie>'), rename_pattern, replacement)
        try:
            base = movieset_base(cfg) if cfg.get('Download_Movieset_Images', False) is True and cfg.get('Download_Images', False) is True else None
        except (ConfigError, OSError) as exc:
            fallback = root / 'Movieset-Fanart'
            fallback.mkdir(parents=True, exist_ok=True)
            print(f'[SET ART WARN] {exc}; fallback: {fallback}')
            base = ('local', fallback)
        if base:
            try:
                if base[0] == 'local' and not base[1].is_dir():
                    raise OSError(f'Movieset_Artwork base directory unavailable: {base[1]}')
                if base[0] == 'smb' and not base[1][0].path.isdir(base[1][1]):
                    raise OSError(f'Movieset_Artwork SMB base directory unavailable: {base[1][1]}')
            except (OSError, ConfigError) as exc:
                fallback = root / 'Movieset-Fanart'
                fallback.mkdir(parents=True, exist_ok=True)
                print(f'[SET ART WARN] {exc}; fallback: {fallback}')
                base = ('local', fallback)
        client = TMDB(cfg.network_timeout(), f'{language}-{country}')
        if base is None:
            print('[SET ART DISABLED] Filmset artwork not configured or disabled')
        else:
            print(f'[SET ART ENABLED] Central artwork base: {base[1]}')
        videos = sorted(p for p in (linked_files(root) if videos_override is None else videos_override) if p.is_file() and
                        not any(part.casefold() in ignored_dirs for part in p.relative_to(root).parts[:-1]) and
                        any(fnmatch.fnmatch(p.name.lower(), pattern.lower()) for pattern in patterns) and
                        not p.stem.lower().endswith('-trailer'))
        print(f'Media-Buddy V2 Film-Scraper | NFO, rename and artwork + movie sets | {len(videos)} file(s)')
        identified = written = skipped = failed = artwork_written = set_artwork_written = trailers_written = trailer_errors = chapters_written = chapter_errors = 0
        processed_sets = set()
        from scraper_progress import emit
        for file_index, path in enumerate(videos, 1):
            from metadata_stop import requested as metadata_stop_requested
            if metadata_stop_requested():
                print("[ABORT] Stop after previous file requested")
                break
            emit('movie', 'Filminformationen abrufen', file=path.name,
                 index=file_index, total=len(videos))
            try:
                # A movie NFO is associated ONLY by identical filename stem.
                # Check it before any online identification or filename search.
                target = path.with_suffix('.nfo')
                existing_tree = read_movie_nfo_tolerant(target) if target.is_file() else None
                mode, movie, query = select_movie_with_nfo(client, path.stem, ignored_words, existing_tree)
                if not movie:
                    if existing_tree is not None and not overwrite:
                        # An existing NFO is not an identification failure. Do not
                        # destroy it or prevent tagging of this already-known movie.
                        skipped += 1
                        if completed_paths is not None:
                            completed_paths.append(path)
                        print(f'[SKIP NFO] {path.name} -> {target.name} (exists; online identification unavailable; artwork enrichment deferred)')
                        continue
                    failed += 1
                    print(f'NOT FOUND [{mode}] {path.name} | {query}')
                    continue
                identified += 1
                if existing_tree is not None and not overwrite:
                    skipped += 1
                    tree = existing_tree
                    print(f'SKIP NFO [{mode}] {path.name} -> {target.name} (already exists)')
                else:
                    details = client.details(movie['id'])
                    if not details:
                        failed += 1
                        print(f'NOT FOUND DETAILS {path.name} | TMDB-{movie["id"]}')
                        continue
                    collection = None
                    ref = details.get('belongs_to_collection') or {}
                    if ref.get('id'):
                        collection = client.collection(ref['id'])
                        if collection is None:
                            print(f'WARNING collection TMDB-{ref["id"]} not found for {path.name}')
                    details = localized_movie_texts(client, details, language, translate_missing, cfg.network_timeout())
                    tree = movie_nfo(details, collection)
                    write_nfo(tree, target)
                    written += 1
                    print(f'OK [{mode}] {path.name} -> TMDB-{movie["id"]} | {target.name}' +
                          (f' | collection {collection["id"]}' if collection else ''))
                final_path = path
                if rename_enabled:
                    renamed_path, renamed_nfo, changed = rename_movie(
                        path, target, root, tree.getroot(), rename_pattern, replacement)
                    final_path = renamed_path
                    if changed:
                        print(f'RENAME {path} -> {renamed_path} | NFO -> {renamed_nfo.name}')
                if completed_paths is not None:
                    completed_paths.append(final_path)
                folder_mode = len(re.split(r'[/\\]', rename_pattern)) > 1 if rename_enabled else False
                artwork_written += download_movie_artwork(client, movie['id'], final_path, cfg,
                                                          folder_mode, cfg.network_timeout())
                trailer_count, trailer_error = download_movie_trailer(client, movie['id'], final_path, cfg)
                trailers_written += trailer_count
                trailer_errors += trailer_error
                if cfg.section('Movies').get('Download_Chapters', False) is True:
                    imdb_id = next((el.text for el in tree.getroot().findall('uniqueid')
                                    if el.get('type') == 'imdb' and el.text), None)
                    if not imdb_id:
                        # Older NFOs may lack the IMDb ID; query TMDB external IDs.
                        external = client.get(f'/movie/{movie["id"]}/external_ids') or {}
                        imdb_id = external.get('imdb_id')
                    chapter_translate = cfg.section('Movies').get('Translate_Chapters', False) is True
                    translation_cache = {}
                    def translate_chapter(name):
                        if name not in translation_cache:
                            translation_cache[name] = translate_movie_text(
                                name, 'en', language.lower().split('-', 1)[0],
                                chapter_translate, cfg.network_timeout(), 'chapter')
                        return translation_cache[name]
                    movie_titles = [movie.get('original_title'), movie.get('title'),
                                    tree.getroot().findtext('originaltitle'), tree.getroot().findtext('title')]
                    movie_year = str(movie.get('release_date') or '')[:4] or tree.getroot().findtext('year')
                    count, errors = download_movie_chapters(
                        imdb_id, final_path, cfg, timeout=cfg.network_timeout(),
                        titles=movie_titles, year=movie_year, translator=translate_chapter if chapter_translate else None)
                    chapters_written += count
                    chapter_errors += errors
                set_info = nfo_collection_info(tree)
                if base and not set_info:
                    print(f'[SET ART SKIP] {final_path.name}: no valid collectionnumber/set name in NFO')
                if base and set_info and set_info[0] in processed_sets:
                    print(f'[SET ART SKIP] {set_info[1]}: collection already checked in this run')
                if base and set_info and set_info[0] not in processed_sets:
                    set_artwork_written += download_movieset_artwork(
                        client, movie['id'], set_info[0], set_info[1], cfg, base, replacement, cfg.network_timeout())
                    processed_sets.add(set_info[0])
            except (requests.RequestException, OSError, ET.ParseError, ValueError) as exc:
                failed += 1
                print(f'ERROR {path.name}: {type(exc).__name__}: {exc}')
        print(f'Finished: {identified} identified, {written} NFO written, '
              f'{skipped} existing NFO skipped, {artwork_written} artwork written, '
              f'{set_artwork_written} movie-set artwork written, {trailers_written} trailers written, {trailer_errors} trailer errors, '
              f'{chapters_written} chapter files written, {chapter_errors} chapter errors, {failed} errors.')
        return 1 if failed or trailer_errors or chapter_errors else 0
    except (ValueError, ConfigError, OSError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    from log_manager import run_with_file_logging
    sys.exit(run_with_file_logging(main, sys.argv[1:], scraper_name='movie'))
