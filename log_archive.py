"""Conservative startup log archiving using only Python's standard library."""
from collections import defaultdict
from datetime import datetime
from pathlib import Path
import re
import zipfile

def kind(name):
    m = re.match(r"^(.*?)_\d{4}-\d{2}-\d{2}[_-]\d{2}[-:]\d{2}[-:]\d{2}.*\.log$", name, re.I)
    return m.group(1).lower() if m else name.lower()

def archive_file(path, directory):
    stamp = datetime.fromtimestamp(path.stat().st_mtime)
    target = directory / f"Logs_{stamp:%Y-%m}.zip"
    data = path.read_bytes()
    with zipfile.ZipFile(target, 'a', compression=zipfile.ZIP_DEFLATED) as z:
        names = set(z.namelist())
        entry = path.name
        n = 2
        while entry in names:
            entry = f"{path.stem}__{n}.log"
            n += 1
        z.writestr(entry, data)
    with zipfile.ZipFile(target, 'r') as z:
        if z.read(entry) != data:
            raise OSError('ZIP verification failed')
    path.unlink()

def rotate(root, retention_months=6):
    logdir = Path(root) / 'Log'
    logdir.mkdir(parents=True, exist_ok=True)
    archives = logdir / 'Archiv'
    archives.mkdir(exist_ok=True)
    errors = []
    general = logdir / 'Media-Buddy.log'
    if general.exists():
        try:
            archive_file(general, archives)
        except Exception as exc:
            return [f'{general.name}: {exc}'], False
    groups = defaultdict(list)
    for p in logdir.glob('*.log'):
        if p.is_file() and p.name != general.name:
            groups[kind(p.name)].append(p)
    for group in groups.values():
        group.sort(key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)
        for p in group[1:]:
            try:
                archive_file(p, archives)
            except Exception as exc:
                errors.append(f'{p.name}: {exc}')
    if retention_months > 0:
        now = datetime.now()
        cutoff = now.year * 12 + now.month - retention_months
        for p in archives.glob('Logs_????-??.zip'):
            m = re.fullmatch(r'Logs_(\d{4})-(\d{2})\.zip', p.name)
            if m and int(m[1])*12 + int(m[2]) < cutoff:
                try:
                    p.unlink()
                except OSError as exc:
                    errors.append(f'{p.name}: {exc}')
    return errors, True
