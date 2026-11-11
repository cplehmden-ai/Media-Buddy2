"""Age certification selection and configurable V1-compatible spelling mapping."""
from __future__ import annotations
import fnmatch
import re


def country_code(value):
    if isinstance(value, dict):
        for key in ('country', 'countryCode', 'code', 'name'):
            code = country_code(value.get(key))
            if code:
                return code
        return ''
    if not isinstance(value, str):
        return ''
    code = value.strip().upper().replace('_', '-')
    aliases = {'USA':'US', 'UNITED STATES':'US', 'UNITED STATES OF AMERICA':'US',
               'UK':'GB', 'UNITED KINGDOM':'GB', 'DEUTSCHLAND':'DE', 'GERMANY':'DE'}
    code = aliases.get(code, code)
    if len(code) == 2 and code.isalpha():
        return code
    return ''


def origin_countries(series):
    countries = []
    for key in ('origin_country', 'originalCountry', 'country', 'countryOfOrigin'):
        value = series.get(key)
        values = value if isinstance(value, list) else [value]
        for item in values:
            code = country_code(item)
            if code and code not in countries:
                countries.append(code)
    return countries


def select_original(records, user_country, origins):
    """Return (original certification, source country); never invent a rating."""
    valid = []
    for record in records or []:
        if not isinstance(record, dict):
            continue
        value = next((str(record[k]).strip() for k in ('name', 'rating', 'certification', 'value')
                      if record.get(k) is not None and str(record[k]).strip()), '')
        country = country_code(record.get('country') or record.get('countryCode') or record.get('iso_3166_1'))
        if value and country:
            valid.append((country, value))
    priority = []
    for code in [user_country, *origins, 'US']:
        code = country_code(code)
        if code and code not in priority:
            priority.append(code)
    for code in priority:
        for record_country, value in valid:
            if code == record_country:
                return value, code
    return '', ''


def mapped_rating(original, rating_config):
    if not original or rating_config.get('OverwriteRating', False) is not True:
        return original
    source = original.strip().casefold()
    for item in rating_config.get('Ratings', []):
        if not isinstance(item, dict):
            continue
        target = str(item.get('SetRating') or '').strip()
        if not target:
            continue
        for pattern in item.get('ContainsRating', []):
            pattern = str(pattern).strip().casefold()
            if pattern and fnmatch.fnmatchcase(source, pattern):
                return target
    return original


def resolve_rating(records, series, config, *, provider):
    original, country = select_original(records, config.get('Country', 'DE'), origin_countries(series))
    final = mapped_rating(original, config.get('Rating', {}))
    print(f'[RATING] {provider}: country={country or "none"}, original={original!r}, final={final!r}')
    return final


def tvdb_records(series):
    return series.get('contentRatings') or []


def tmdb_records(payload):
    if not isinstance(payload, dict):
        return []
    results = payload.get('results') or []
    return [{'country': x.get('iso_3166_1'), 'certification': x.get('rating')}
            for x in results if isinstance(x, dict)]
