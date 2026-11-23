"""Optional music artwork and portable Kodi NFO generation (Test 193c).

Existing artwork and unmarked NFOs are protected; marked placeholders may be upgraded.
"""
from __future__ import annotations
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET
import requests
from datetime import datetime
from version import FILE_VERSION
from musicbrainz_rate_limit import mb_get


def _log(*parts):
    print('[MUSIC ART]', *parts, flush=True)


def _copy_if_missing(source, target):
    if target.exists() or not source.is_file():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    _log('Copied', source, '->', target)
    return True


def _download(url, target, session):
    if target.exists() or not isinstance(url, str) or not url.startswith('https://'):
        return False
    try:
        with session.get(url, timeout=(8, 25), stream=True, allow_redirects=True) as response:
            response.raise_for_status()
            if not response.url.startswith('https://'):
                return False
            content_type = response.headers.get('Content-Type', '').lower()
            if not content_type.startswith('image/'):
                _log('Rejected non-image response:', response.url)
                return False
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_name(target.name + '.download')
            try:
                with temp.open('wb') as stream:
                    total = 0
                    for chunk in response.iter_content(65536):
                        total += len(chunk)
                        if total > 20 * 1024 * 1024:
                            raise ValueError('Artwork exceeds 20 MB')
                        stream.write(chunk)
                if not target.exists():
                    temp.rename(target)
                    _log('Downloaded', target)
                    return True
            finally:
                temp.unlink(missing_ok=True)
    except (requests.RequestException, OSError, ValueError) as exc:
        _log('WARNING: download failed:', target, exc)
    return False


PLACEHOLDER_MARKER = '<!-- Media-Buddy: AUTO_PLACEHOLDER -->'


def _xml_if_missing(path, root, fields, placeholder=False):
    """Protect regular NFOs; upgrade only explicitly marked auto-placeholders.

    An incomplete generated NFO remains replaceable. Unmarked files, including
    old test files and manually edited files, are never overwritten.
    """
    path = Path(path)
    existing = None
    if path.exists():
        try:
            existing = path.read_text(encoding='utf-8-sig')
        except OSError as exc:
            _log('WARNING: cannot read existing NFO:', path, exc)
            return
        if PLACEHOLDER_MARKER not in existing:
            return
        try:
            if ET.fromstring(existing).tag != root:
                _log('WARNING: unexpected NFO root; keeping:', path)
                return
        except ET.ParseError:
            _log('WARNING: invalid existing NFO; keeping:', path)
            return
    root_el = ET.Element(root)
    for key, value in fields:
        if value is not None and str(value).strip():
            ET.SubElement(root_el, key).text = str(value).strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(root_el, space='  ')
    body = ET.tostring(root_el, encoding='unicode', short_empty_elements=False)
    stamp = datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %z')
    header = '<?xml version="1.0" encoding="UTF-8"?>\n'
    header += f'<!-- Created on {stamp} by Media-Buddy V2 v{FILE_VERSION} -->\n'
    if placeholder:
        header += PLACEHOLDER_MARKER + '\n'
    content = header + body + '\n'
    if existing == content:
        return
    # A replaceable NFO is written atomically so an interrupted run cannot
    # leave a truncated file. Ordinary NFOs are not touched.
    import os
    import tempfile
    temp = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', newline='\n',
                                         dir=path.parent, prefix='.mb_nfo_',
                                         suffix='.tmp', delete=False) as stream:
            temp = Path(stream.name)
            stream.write(content)
        os.replace(temp, path)
        _log('Updated placeholder NFO:' if existing is not None else 'Created portable NFO:', path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def _album_fields(release, album, artist):
    group = release.get('release-group') or {}
    date = release.get('_preferred_date') or release.get('date') or ''
    return [('title', album), ('musicbrainzalbumid', release.get('id')),
            ('musicbrainzreleasegroupid', group.get('id')),
            ('artistdesc', artist), ('year', str(date)[:4]),
            ('releasedate', date), ('label', (release.get('label-info') or [{}])[0].get('label', {}).get('name') if release.get('label-info') else ''),
            ('releasetype', group.get('primary-type'))]


def _artist_id(release):
    for item in release.get('artist-credit') or []:
        if isinstance(item, dict) and item.get('artist', {}).get('id'):
            return item['artist']['id']
    return ''


def _artist_data(session, artist_id, artist_name):
    if not artist_id:
        return {}
    try:
        r = mb_get(session, 'https://musicbrainz.org/ws/2/artist/' + artist_id,
                        params={'fmt': 'json', 'inc': 'genres'}, timeout=(8, 20))
        r.raise_for_status()
        return r.json()
    except (requests.RequestException, ValueError) as exc:
        _log('WARNING: artist metadata unavailable:', artist_name, exc)
        return {}


def _fanart_urls(session, artist_id):
    if not artist_id:
        return {}
    try:
        from api_keys import get_api_key
        key = get_api_key('FANART_TV')
    except (ImportError, AttributeError, OSError):
        key = ''
    if not key:
        _log('Fanart.tv key unavailable; skipping Fanart.tv')
        return {}
    try:
        r = session.get('https://webservice.fanart.tv/v3/music/' + artist_id,
                        params={'api_key': key}, timeout=(8, 20))
        if r.status_code == 404:
            return {}
        r.raise_for_status()
        data = r.json()
        result = {}
        for name, field in [('folder.jpg', 'artistthumb'), ('fanart.jpg', 'artistbackground'),
                            ('banner.jpg', 'musicbanner'), ('clearlogo.png', 'hdmusiclogo')]:
            choices = data.get(field) or []
            if name == 'clearlogo.png' and not choices:
                choices = data.get('musiclogo') or []
            if choices:
                result[name] = choices[0].get('url')
        return result
    except (requests.RequestException, ValueError, TypeError) as exc:
        _log('WARNING: Fanart.tv unavailable:', artist_id, exc)
        return {}


def _audiodb_artist(session, artist_id):
    """TheAudioDB details, including biography; one request per artist."""
    if not artist_id:
        return {}
    try:
        from api_keys import get_api_key
        key = get_api_key('THEAUDIODB') or '123'
    except (ImportError, AttributeError, OSError):
        key = '123'
    try:
        r = session.get('https://www.theaudiodb.com/api/v1/json/' + key + '/artist-mb.php',
                        params={'i': artist_id}, timeout=(8, 20))
        r.raise_for_status()
        artists = r.json().get('artists') or []
        return artists[0] if artists and isinstance(artists[0], dict) else {}
    except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
        _log('WARNING: TheAudioDB unavailable:', artist_id, exc)
        return {}


def _audiodb_urls(session, artist_id, data=None):
    item = data if data is not None else _audiodb_artist(session, artist_id)
    return {'folder.jpg': item.get('strArtistThumb'), 'fanart.jpg': item.get('strArtistFanart'),
            'banner.jpg': item.get('strArtistBanner'), 'clearlogo.png': item.get('strArtistLogo')}


def _artist_nfo_fields(artist, artist_id, mb, audiodb):
    """Kodi artist fields; never manufacture dates from unrelated releases."""
    mb = mb or {}
    audiodb = audiodb or {}
    kind = (mb.get('type') or '').casefold()
    life = mb.get('life-span') or {}
    begin = life.get('begin') or ''
    end = life.get('end') or ''
    if kind == 'person':
        dates = [('born', begin or audiodb.get('intBornYear')),
                 ('died', end or audiodb.get('intDiedYear'))]
    elif kind == 'group':
        dates = [('formed', begin or audiodb.get('intFormedYear')),
                 ('disbanded', end or audiodb.get('strDisbanded'))]
    else:
        dates = []
    biography = (audiodb.get('strBiographyDE') or '').strip()
    if not biography:
        english = (audiodb.get('strBiographyEN') or audiodb.get('strBiography') or '').strip()
        if english:
            biography = english
            try:
                from translation_manager import translate_plot
                result = translate_plot(english, 'en', 'de')
                if result.success and result.text.strip() and result.text.strip() != english:
                    biography = result.text.strip()
                    _log('Translated English biography:', artist)
                else:
                    _log('WARNING: biography translation failed or unchanged; using English:', artist, result.error or 'Unchanged output')
            except Exception as exc:
                _log('WARNING: biography translation unavailable; using English:', artist, exc)
    fields = [('name', artist), ('sortname', mb.get('sort-name')),
              ('musicBrainzArtistID', artist_id), ('type', mb.get('type')),
              ('gender', mb.get('gender')), ('disambiguation', mb.get('disambiguation'))]
    fields.extend(dates)
    fields.extend([('genre', genre.get('name')) for genre in (mb.get('genres') or [])
                   if isinstance(genre, dict) and genre.get('name')])
    fields.extend([('style', audiodb.get('strStyle')),
                   ('biography', biography)])
    return fields

def _cover_url(session, release_id):
    if not release_id:
        return None
    try:
        r = session.get('https://coverartarchive.org/release/' + release_id,
                        timeout=(8, 20))
        if r.status_code == 404:
            return None
        r.raise_for_status()
        images = r.json().get('images') or []
        front = next((i for i in images if i.get('front')), images[0] if images else {})
        return (front.get('thumbnails') or {}).get('large') or front.get('image')
    except (requests.RequestException, ValueError, TypeError) as exc:
        _log('WARNING: Cover Art Archive unavailable:', release_id, exc)
        return None


def _embedded_cover_candidates(path):
    """Read artwork without ever saving/changing the audio file."""
    from mutagen import File
    from mutagen.flac import Picture
    from mutagen.id3 import ID3, ID3NoHeaderError
    import base64

    audio = File(str(path))
    if audio is None:
        return []
    pictures = []
    for pic in getattr(audio, 'pictures', ()):
        pictures.append((int(pic.type), bytes(pic.data)))
    tags = getattr(audio, 'tags', None)
    if tags is not None:
        if hasattr(tags, 'getall'):
            for pic in tags.getall('APIC'):
                pictures.append((int(pic.type), bytes(pic.data)))
        if hasattr(tags, 'get'):
            for pic in tags.get('covr', ()):
                pictures.append((3, bytes(pic)))
            for value in tags.get('metadata_block_picture', ()):
                try:
                    pic = Picture(base64.b64decode(value))
                    pictures.append((int(pic.type), bytes(pic.data)))
                except (ValueError, TypeError) as exc:
                    _log('WARNING: invalid embedded picture:', path, exc)
    return [(kind, data) for kind, data in pictures if data]


def _write_embedded_jpeg(data, target):
    """Export a real JPEG, never write PNG bytes into a .jpg file."""
    if data.startswith(b'\xff\xd8\xff'):
        jpeg = data
    else:
        # PySide6 is already a required application dependency; Qt handles
        # PNG, WebP and other supported image formats without Pillow.
        from PySide6.QtCore import QBuffer, QIODevice
        from PySide6.QtGui import QImage
        picture = QImage.fromData(data)
        if picture.isNull():
            return False
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not picture.save(buffer, 'JPEG', 95):
            return False
        jpeg = bytes(buffer.data())
    if len(jpeg) > 20 * 1024 * 1024:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation: never overwrite a cover produced by another process.
    try:
        with target.open('xb') as output:
            output.write(jpeg)
    except FileExistsError:
        return False
    return True


def _extract_embedded_album_cover(audio_paths, local_album):
    """Best-effort fallback: front cover first, otherwise the sole image.

    If several images exist but none is explicitly marked front, take the
    first image as a pragmatic fallback; postprocessing can correct it.
    """
    candidates = []
    for path in sorted((Path(p) for p in audio_paths), key=lambda p: str(p).casefold()):
        if not path.is_file():
            continue
        try:
            candidates.extend((kind, data, path) for kind, data in _embedded_cover_candidates(path))
        except Exception as exc:
            _log('WARNING: embedded artwork unreadable:', path, exc)
    candidates.sort(key=lambda item: (item[0] != 3,))
    for kind, data, source in candidates:
        try:
            if _write_embedded_jpeg(data, local_album / 'folder.jpg'):
                _log('Extracted embedded album cover:', source, '->', local_album / 'folder.jpg')
                return local_album / 'folder.jpg'
        except Exception as exc:
            _log('WARNING: embedded artwork export failed:', source, exc)
    return None


def _embed_cover(audio_paths, cover):
    """Only change *output* files; mutagen handles FLAC/MP3/MP4 artwork."""
    if not cover.exists():
        return
    try:
        from mutagen.flac import FLAC, Picture
        from mutagen.id3 import ID3, APIC, ID3NoHeaderError
        from mutagen.mp4 import MP4, MP4Cover
    except ImportError:
        _log('WARNING: mutagen unavailable; cannot embed artwork')
        return
    data = cover.read_bytes()
    mime = 'image/png' if data.startswith(b'\x89PNG') else 'image/jpeg'
    for path in audio_paths:
        try:
            suffix = path.suffix.lower()
            if suffix == '.flac':
                obj = FLAC(path)
                if not obj.pictures:
                    pic = Picture(); pic.data = data; pic.mime = mime; pic.type = 3
                    obj.add_picture(pic); obj.save()
            elif suffix == '.mp3':
                try:
                    obj = ID3(path)
                except ID3NoHeaderError:
                    obj = ID3()
                if not obj.getall('APIC'):
                    obj.add(APIC(encoding=3, mime=mime, type=3, desc='Cover', data=data))
                    obj.save(path)
            elif suffix in ('.m4a', '.mp4'):
                obj = MP4(path)
                if not obj.tags or not obj.tags.get('covr'):
                    obj['covr'] = [MP4Cover(data, imageformat=MP4Cover.FORMAT_PNG if mime == 'image/png' else MP4Cover.FORMAT_JPEG)]
                    obj.save()
        except Exception as exc:
            _log('WARNING: could not embed cover:', path, exc)


def process_artwork(release, album, artist, local_album, local_artist, central_root, opts, audio_paths, session=None, central_album_name=None):
    """One acquisition pipeline feeding local and optional central Kodi destinations."""
    session = session or requests.Session()
    session.headers.update({'User-Agent': 'Media-Buddy-V2 (https://github.com/cplehmden-ai/Media-Buddy2)'})
    central_artist = Path(central_root) / local_artist.name if central_root else None
    # Central Kodi album directories are named ONLY after the album tag.
    # No year, inferred edition or local folder-name components may be added.
    # Sanitize only characters forbidden in Windows paths (also safe on Linux).
    from music_album_scraper import clean
    central_name = clean(central_album_name if central_album_name else album)
    central_album = central_artist / central_name if central_artist else None
    album_targets = [local_album] + ([central_album] if central_album else [])
    artist_targets = ([local_artist] if local_artist.is_dir() else []) + ([central_artist] if central_artist else [])
    want_cover = opts.get('album_artwork', False)
    want_artist = opts.get('artist_artwork', False)
    want_nfo = opts.get('music_nfo', False)
    if not (want_cover or want_artist or want_nfo):
        return
    for target in album_targets + artist_targets:
        if target is not None and target.exists() and not target.is_dir():
            _log('WARNING: artwork target is not a directory:', target)
            return
    if want_cover:
        local_cover = next((local_album / n for n in ('folder.jpg', 'folder.jpeg', 'folder.png')
                            if (local_album / n).is_file()), None)
        if not local_cover:
            _download(_cover_url(session, release.get('id')), local_album / 'folder.jpg', session)
            local_cover = local_album / 'folder.jpg' if (local_album / 'folder.jpg').exists() else None
        if not local_cover:
            local_cover = _extract_embedded_album_cover(audio_paths, local_album)
        if local_cover:
            for target in album_targets[1:]:
                if not any((target / n).exists() for n in ('folder.jpg', 'folder.jpeg', 'folder.png')):
                    _copy_if_missing(local_cover, target / 'folder.jpg')
            if opts.get('embed_album_cover', False):
                _embed_cover([p for p in audio_paths if p.is_file()], local_cover)
    artist_id = _artist_id(release)
    artist_extra = _audiodb_artist(session, artist_id) if (want_artist or want_nfo) else {}
    if want_artist:
        urls = _fanart_urls(session, artist_id)
        fallback = _audiodb_urls(session, artist_id, artist_extra) if len(urls) < 4 else {}
        for filename in ('folder.jpg', 'fanart.jpg', 'banner.jpg', 'clearlogo.png'):
            existing = next((folder / filename for folder in artist_targets if folder and (folder / filename).is_file()), None)
            if not existing:
                destination_artist = local_artist if local_artist.is_dir() else central_artist
                if destination_artist:
                    _download(urls.get(filename) or fallback.get(filename), destination_artist / filename, session)
                existing = destination_artist / filename if destination_artist and (destination_artist / filename).exists() else None
            if existing:
                for folder in artist_targets:
                    if folder:
                        _copy_if_missing(existing, folder / filename)
    if want_nfo:
        fields = _album_fields(release, album, artist)
        for target in album_targets:
            if target:
                _xml_if_missing(target / 'album.nfo', 'album', fields)
        artist_data = _artist_data(session, artist_id, artist)
        artist_fields = _artist_nfo_fields(artist, artist_id, artist_data, artist_extra)
        for target in artist_targets:
            if target:
                _xml_if_missing(target / 'artist.nfo', 'artist', artist_fields)


def process_single_artist(artist, artist_id, local_artist, central_root, opts, session=None):
    """Enrich an identified loose track's artist, never invent an album."""
    if not artist_id or not (opts.get('artist_artwork') or opts.get('music_nfo')):
        return
    session = session or requests.Session()
    session.headers.update({'User-Agent': 'Media-Buddy-V2 (https://github.com/cplehmden-ai/Media-Buddy2)'})
    central_artist = Path(central_root) / local_artist.name if central_root else None
    if central_artist and Path(central_root).is_dir():
        from music_album_scraper import existing_artist_folder
        central_artist = Path(central_root) / existing_artist_folder(Path(central_root), local_artist.name)
    targets = ([local_artist] if local_artist.is_dir() else []) + ([central_artist] if central_artist else [])
    extra = _audiodb_artist(session, artist_id)
    if opts.get('artist_artwork'):
        urls = _fanart_urls(session, artist_id)
        fallback = _audiodb_urls(session, artist_id, extra) if len(urls) < 4 else {}
        for filename in ('folder.jpg', 'fanart.jpg', 'banner.jpg', 'clearlogo.png'):
            existing = next((folder / filename for folder in targets if (folder / filename).is_file()), None)
            if not existing:
                destination_artist = local_artist if local_artist.is_dir() else central_artist
                if destination_artist:
                    _download(urls.get(filename) or fallback.get(filename), destination_artist / filename, session)
                existing = destination_artist / filename if destination_artist and (destination_artist / filename).is_file() else None
            if existing:
                for target in targets:
                    _copy_if_missing(existing, target / filename)
    if opts.get('music_nfo'):
        info = _artist_data(session, artist_id, artist)
        fields = _artist_nfo_fields(artist, artist_id, info, extra)
        for target in targets:
            _xml_if_missing(target / 'artist.nfo', 'artist', fields)


def process_combined_artist(display_name, members, central_root, opts, local_artist=None):
    """Additive Kodi NFO for a verified collaboration; never alter existing NFOs.

    Member biographies come exclusively from the already-generated central
    individual NFOs. No additional API calls and no synthetic MusicBrainz ID.
    """
    if not opts.get('combined_artist_nfo') or not opts.get('music_nfo') or not central_root:
        return False
    from music_album_scraper import existing_artist_folder, clean
    if len({aid for _, aid in members if aid}) < 2 or not display_name.strip():
        return False
    root = Path(central_root)
    if not root.is_dir():
        _log('WARNING: combined artist root unavailable:', root)
        return False
    combined_name = existing_artist_folder(root, clean(display_name))
    destination = root / combined_name
    if destination.exists() and not destination.is_dir():
        return False
    # The caller must already have verified that this is a collaboration,
    # not a single MusicBrainz band whose name happens to contain '&'.
    sections = []
    for name, _aid in members:
        folder = root / existing_artist_folder(root, clean(name))
        biography = ''
        source = folder / 'artist.nfo'
        if source.is_file():
            try:
                biography = (ET.parse(source).getroot().findtext('biography') or '').strip()
            except (ET.ParseError, OSError) as exc:
                _log('WARNING: member biography unavailable:', source, exc)
        sections.append(name + ('\n\n' + biography if biography else ''))
    fields = [('name', display_name), ('biography', '\n\n'.join(sections))]
    # Never write musicBrainzArtistID for a constructed artist.
    _xml_if_missing(destination / 'artist.nfo', 'artist', fields)
    if local_artist is not None and Path(local_artist).is_dir():
        local = Path(local_artist)
        if local.name.casefold() == clean(display_name).casefold():
            _xml_if_missing(local / 'artist.nfo', 'artist', fields)
    _log('Combined artist NFO checked:', display_name)
    return True
