"""Optional, caller-independent progress notifications for every scraper.

Callbacks receive a dictionary: scraper, stage, file, index, total, percent.
percent is None when no reliable progress can be measured.
"""
from contextlib import contextmanager
from contextvars import ContextVar

_receiver = ContextVar('media_buddy_scraper_progress', default=None)

@contextmanager
def progress_receiver(callback):
    token = _receiver.set(callback)
    try:
        yield
    finally:
        _receiver.reset(token)

def emit(scraper, stage, *, file=None, index=None, total=None, percent=None, **details):
    callback = _receiver.get()
    if callback is None:
        return
    event = {'scraper': str(scraper), 'stage': str(stage),
             'file': str(file) if file is not None else None,
             'index': index, 'total': total, 'percent': percent, **details}
    try:
        callback(event)
    except Exception:
        # Progress listeners must never interrupt a scraper or batch run.
        pass
