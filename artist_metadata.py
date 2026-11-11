"""Shared, non-destructive Kodi artist.nfo creation for music and music videos.

The caller supplies a song-verified MusicBrainz ID. This module NEVER guesses
an identity and NEVER modifies an existing artist.nfo.
"""
from __future__ import annotations

import html
import os
from pathlib import Path
import uuid
import xml.etree.ElementTree as ET

import requests
from translation_manager import normalize_language, translate_plot

AUDIODB = 'https://www.theaudiodb.com/api/v1/json/'
LASTFM = 'https://ws.audioscrobbler.com/2.0/'
MB = 'https://musicbrainz.org/ws/2/artist/'


def _fetch(session, url, params, timeout):
    try:
        response = session.get(url, params=params, timeout=timeout)
        response.raise_for_status()
        return response.json() or {}
    except (requests.RequestException, ValueError, TypeError) as exc:
        print(f'[ARTIST NFO WARN] Metadata request failed: {exc}')
        return {}


def _description(data, language, settings, timeout):
    lang = normalize_language(language) or 'de'
    native = str(data.get('strBiography' + lang.upper()) or '').strip()
    if native:
        return html.unescape(native)
    english = str(data.get('strBiographyEN') or '').strip()
    if not english:
        # Other AudioDB languages are a fallback, but we cannot safely infer
        # the language of arbitrary text without its field suffix.
        for key, value in data.items():
            if key.startswith('strBiography') and value:
                source = normalize_language(key[len('strBiography'):])
                if source:
                    english = str(value).strip()
                    source_lang = source
                    break
        else:
            return ''
    else:
        source_lang = 'en'
    if source_lang != lang and settings.get('TranslateMissingTexts', True):
        result = translate_plot(english, source_lang, lang, timeout=timeout)
        if result.success:
            print(f'[ARTIST NFO] Biography translated {source_lang} -> {lang}')
            return result.text
        print(f'[ARTIST NFO WARN] Translation failed: {result.error}; using original')
    return html.unescape(english)


def create_artist_nfo(session, folder, artist, mbid, *, audiodb_key='', lastfm_key='',
                      language='de', settings=None, timeout=(15, 30), user_agent=None):
    """Create only when absent and identity verified by caller. Return bool."""
    settings = settings or {}
    folder = Path(folder)
    target = folder / 'artist.nfo'
    if target.exists():
        print(f'[ARTIST NFO] Existing artist.nfo protected: {folder.name}')
        return False
    if not mbid or not artist or not folder.is_dir():
        print(f'[ARTIST NFO WARN] Missing verified artist identity or directory: {artist}')
        return False

    # Query by MBID first. Reject a name-only response that names a different artist.
    mb = _fetch(session, MB + str(mbid), {'fmt': 'json'}, timeout)
    if mb.get('id') and str(mb['id']).casefold() != str(mbid).casefold():
        print(f'[ARTIST NFO WARN] MusicBrainz identity mismatch for {artist}')
        return False
    audiodb = {}
    if audiodb_key:
        data = _fetch(session, f'{AUDIODB}{audiodb_key}/artist-mb.php', {'i': mbid}, timeout)
        audiodb = next(iter(data.get('artists') or []), {}) or {}
        if not audiodb:
            data = _fetch(session, f'{AUDIODB}{audiodb_key}/search.php', {'s': artist}, timeout)
            for item in data.get('artists') or []:
                if str(item.get('strArtist', '')).casefold() == artist.casefold() and str(item.get('strMusicBrainzID') or '').casefold() == str(mbid).casefold():
                    audiodb = item
                    break
    biography = _description(audiodb, language, settings, timeout)
    if not biography and lastfm_key:
        data = _fetch(session, LASTFM, {'method': 'artist.getinfo', 'mbid': mbid,
                                      'api_key': lastfm_key, 'format': 'json',
                                      'lang': normalize_language(language) or 'de'}, timeout)
        info = data.get('artist') or {}
        # Last.fm often returns English even with lang=de; do not pretend we know
        # its language. Request English explicitly for a known translation source.
        if info.get('bio'):
            bio = info['bio'] or {}
            biography = str(bio.get('content') or bio.get('summary') or '').strip()
            if biography:
                # Last.fm language is not guaranteed; use English endpoint to
                # obtain a known source when a translation is requested.
                lang = normalize_language(language) or 'de'
                if lang != 'en' and settings.get('TranslateMissingTexts', True):
                    en = _fetch(session, LASTFM, {'method': 'artist.getinfo', 'mbid': mbid,
                                                  'api_key': lastfm_key, 'format': 'json', 'lang': 'en'}, timeout)
                    en_bio = (en.get('artist') or {}).get('bio') or {}
                    source = str(en_bio.get('content') or en_bio.get('summary') or '').strip()
                    if source:
                        result = translate_plot(source, 'en', lang, timeout=timeout)
                        if result.success:
                            biography = result.text
                            print(f'[ARTIST NFO] Last.fm biography translated en -> {lang}')
                        else:
                            print(f'[ARTIST NFO WARN] Translation failed: {result.error}')

    root = ET.Element('artist')
    def add(tag, value):
        if value is not None and str(value).strip():
            ET.SubElement(root, tag).text = html.unescape(str(value).strip())
    add('name', mb.get('name') or artist)
    add('musicBrainzArtistID', mbid)
    add('sortname', mb.get('sort-name'))
    add('type', mb.get('type') or audiodb.get('strArtistType'))
    add('gender', mb.get('gender'))
    add('disambiguation', mb.get('disambiguation'))
    life = mb.get('life-span') or {}
    if life.get('begin'):
        add('formed' if (mb.get('type') or '').casefold() == 'group' else 'born', life['begin'])
    if life.get('end'):
        add('disbanded' if (mb.get('type') or '').casefold() == 'group' else 'died', life['end'])
    add('biography', biography)
    tree = ET.ElementTree(root)
    ET.indent(tree, space='  ')
    # Exclusive create prevents overwriting an NFO created concurrently by Kodi.
    try:
        with target.open('xb') as stream:
            tree.write(stream, encoding='utf-8', xml_declaration=True)
        print(f'[ARTIST NFO] Created {target} | biography={"yes" if biography else "no"}')
        return True
    except FileExistsError:
        print(f'[ARTIST NFO] Existing artist.nfo protected: {folder.name}')
        return False
    except OSError as exc:
        print(f'[ARTIST NFO WARN] Cannot write {target}: {exc}')
        return False
