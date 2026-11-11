"""Shared, field-agnostic date normalization for explicitly designated date fields.

Never apply to arbitrary strings, titles, paths or whole XML documents.
"""
from __future__ import annotations

from datetime import date
import re


class InvalidDate(ValueError):
    """A nonempty date input is not a supported, valid calendar date."""


def normalize_date(value: str | None) -> str:
    """Return ISO YYYY-MM-DD for DD.MM.YYYY or YYYY-MM-DD.

    Empty input stays empty. Do not invent missing day/month or guess ambiguous
    slash-formatted dates. Calendar validity (including leap years) is checked.
    """
    raw = (value or '').strip()
    if not raw:
        return ''
    match = re.fullmatch(r'(\d{4})-(\d{1,2})-(\d{1,2})', raw)
    if match:
        year, month, day = map(int, match.groups())
    else:
        match = re.fullmatch(r'(\d{1,2})\.(\d{1,2})\.(\d{4})', raw)
        if not match:
            raise InvalidDate(f'Ungültiges Datumsformat: {raw!r} (TT.MM.JJJJ oder JJJJ-MM-TT)')
        day, month, year = map(int, match.groups())
    try:
        return date(year, month, day).isoformat()
    except ValueError as exc:
        raise InvalidDate(f'Ungültiges Kalenderdatum: {raw!r}') from exc
