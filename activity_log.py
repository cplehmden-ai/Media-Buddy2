"""One readable activity log per Media-Buddy application run."""
from datetime import datetime
from pathlib import Path
_handle = None

def start(root):
    global _handle
    path = Path(root) / 'Log' / 'Media-Buddy.log'
    path.parent.mkdir(parents=True, exist_ok=True)
    _handle = path.open('x', encoding='utf-8', buffering=1)
    event('Media-Buddy gestartet')

def event(message, level='INFO'):
    if _handle is not None:
        message = str(message).replace('\n',' ').replace('\r',' ')
        _handle.write(f'{datetime.now():%d.%m.%Y %H:%M:%S} - {level} - {message}\n')
        _handle.flush()

def stop():
    global _handle
    if _handle is not None:
        event('Media-Buddy beendet')
        _handle.close()
        _handle = None
