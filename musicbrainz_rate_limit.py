"""Process-wide, thread-safe MusicBrainz request pacing (Test 198h).

All production MusicBrainz HTTP calls must pass through mb_get. The lock
covers the reservation of a request slot, not the network operation.
"""
from __future__ import annotations
import threading
import time
from urllib.parse import urlsplit

_MIN_INTERVAL = 1.2
_lock = threading.Lock()
_last_start = 0.0


def mb_get(session, url, **kwargs):
    """Start MusicBrainz requests at least 1.2 seconds apart, including retries."""
    global _last_start
    if urlsplit(str(url)).hostname != 'musicbrainz.org':
        return session.get(url, **kwargs)
    with _lock:
        wait = _MIN_INTERVAL - (time.monotonic() - _last_start) if _last_start else 0
        if wait > 0:
            print(f'[MUSICBRAINZ RATE] Waiting {wait:.2f}s before request', flush=True)
            time.sleep(wait)
        _last_start = time.monotonic()
    return session.get(url, **kwargs)
