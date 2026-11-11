"""Explicit, post-processing-only cleanup of original files (Test 140).

A single last-run list is deliberately replaced on every new wizard run.
No processing code ever removes an original file.
"""
import json
import os
from pathlib import Path
from datetime import datetime

LIST_PATH = Path(__file__).resolve().parent / 'last_processed_originals.json'


def _write(data):
    temporary = LIST_PATH.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, LIST_PATH)


def start_run():
    _write({'created': datetime.now().isoformat(timespec='seconds'), 'files': []})


def add_success(source, target):
    source, target = Path(source).resolve(), Path(target).resolve()
    if source == target or not source.is_file() or not target.is_file():
        return
    stat = source.stat()
    data = load()
    files = data['files']
    item = {'source': str(source), 'target': str(target), 'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}
    files[:] = [f for f in files if f.get('source') != str(source)]
    files.append(item)
    _write(data)


def load():
    if not LIST_PATH.exists():
        return {'files': []}
    data = json.loads(LIST_PATH.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or not isinstance(data.get('files'), list):
        raise ValueError('Ungültige Löschliste')
    return data


def cleanup():
    data = load()
    result = {'deleted': 0, 'missing': 0, 'errors': []}
    for item in data['files']:
        src = Path(item['source'])
        dst = Path(item['target'])
        try:
            if not src.exists():
                result['missing'] += 1
                continue
            if not src.is_file() or src.resolve() == dst.resolve():
                raise ValueError('Ungültiger Quellpfad oder identischer Zielpfad')
            stat = src.stat()
            if stat.st_size != item['size'] or stat.st_mtime_ns != item['mtime_ns']:
                raise ValueError('Quelldatei seit der Verarbeitung verändert')
            src.unlink()
            result['deleted'] += 1
        except Exception as exc:
            result['errors'].append((str(src), str(exc)))
    # Keep only failed entries for a deliberate retry; never delete new inputs.
    failed_sources = {source for source, _error in result['errors']}
    _write({'files': [item for item in data['files'] if item['source'] in failed_sources]})
    log = LIST_PATH.parent / 'Log' / 'original_cleanup.log'
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open('a', encoding='utf-8') as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')} gelöscht={result['deleted']} fehlend={result['missing']} Fehler={len(result['errors'])}\n")
        for path, error in result['errors']:
            f.write(f'  FEHLER: {path}: {error}\n')
    return result
