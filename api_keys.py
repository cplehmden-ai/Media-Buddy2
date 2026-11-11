"""Media-Buddy V2: public API-key access layer (no embedded credentials)."""
from __future__ import annotations
import os

_NAMES = ('TVDB', 'TMDB', 'FANART_TV', 'LASTFM', 'THEAUDIODB')

class MissingAPIKey(RuntimeError):
    pass

def get_api_key(provider: str, required: bool = False) -> str:
    """Read from a locally compiled vault, or an explicit environment override."""
    name = provider.upper().replace('-', '_').replace('.', '_')
    if name not in _NAMES:
        raise ValueError(f'Unknown API provider: {provider}')
    value = os.environ.get('MEDIA_BUDDY_' + name + '_API_KEY', '').strip()
    if not value:
        try:
            try:
                from mb_core import get_key
            except ImportError:
                # Temporary compatibility with the working Test 129b binary.
                from api_key_vault import get_key
        except ImportError:
            value = ''
        else:
            value = str(get_key(name) or '').strip()
    if required and not value:
        raise MissingAPIKey(f'No API key available for {name}. Build mb_core or install the temporary Test 129b extension.')
    return value
