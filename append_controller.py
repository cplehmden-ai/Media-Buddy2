"""Test 137: Verlustfreies Zusammenfügen nach Unterordnern mit mkvmerge."""
from __future__ import annotations
import re
import subprocess
from datetime import datetime
from pathlib import Path
from tool_manager import get_tool

VIDEO_EXT = {'.mkv', '.mk3d', '.mp4', '.m4v', '.mov', '.avi', '.ts', '.m2ts', '.mpg', '.mpeg', '.webm', '.wmv', '.vob'}

def natural_key(path):
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r'(\d+)', path.name)]

def run_append_batch(settings, notify=lambda *a: None, stop_requested=lambda: False, immediate_requested=lambda: False):
    source = Path(settings['source']).expanduser().resolve()
    target = Path(settings['target']).expanduser().resolve()
    if not source.is_dir() or not target.is_dir():
        raise ValueError('Quell- und Zielordner müssen existieren')
    if source == target or source in target.parents or target in source.parents:
        raise ValueError('Quell- und Zielordner dürfen nicht ineinander liegen')
    mkvmerge = get_tool('mkvmerge')
    groups = sorted((d for d in source.iterdir() if d.is_dir()), key=natural_key)
    if not groups:
        raise ValueError('Keine Unterordner mit Zusammenfügeaufträgen gefunden')
    logdir = Path(__file__).with_name('Log')
    logdir.mkdir(exist_ok=True)
    logfile = logdir / ('append_batch_' + datetime.now().strftime('%Y-%m-%d_%H-%M-%S_%f') + '.log')
    result = {'processed': 0, 'skipped': 0, 'failed': 0, 'log_file': str(logfile)}
    def report(message, index=0):
        with logfile.open('a', encoding='utf-8') as log:
            log.write(str(message) + '\n')
        notify('log', str(message), index, len(groups))
    report(f'Videos zusammenfügen: {len(groups)} Unterordner; Quelle={source}; Ziel={target}')
    for index, group in enumerate(groups, 1):
        if stop_requested() or immediate_requested():
            result['aborted'] = True
            break
        notify('file', group.name, index, len(groups))
        inputs = sorted((f for f in group.iterdir() if f.is_file() and f.suffix.lower() in VIDEO_EXT), key=natural_key)
        output = target / (group.name + '.mkv')
        if output.exists():
            result['skipped'] += 1
            report(f'Übersprungen (Zieldatei vorhanden): {output}', index)
            continue
        if len(inputs) < 2:
            result['skipped'] += 1
            report(f'Übersprungen (weniger als zwei unterstützte Videodateien): {group}', index)
            continue
        report(f'Zusammenfügen: {group.name}: ' + ' + '.join(f.name for f in inputs), index)
        # Write only to a unique temporary target; never overwrite existing outputs.
        partial = target / ('.' + group.name + '.append-' + datetime.now().strftime('%H%M%S%f') + '.partial.mkv')
        args = [mkvmerge, '--gui-mode', '--output', str(partial), str(inputs[0])]
        for part in inputs[1:]:
            args.extend(['+', str(part)])
        proc = None
        try:
            proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, text=True, encoding='utf-8', errors='replace',
                                    bufsize=1)
            for line in proc.stdout:
                line = line.strip()
                if immediate_requested():
                    proc.kill()
                    break
                match = re.search(r'(?:#GUI#progress\s+|Progress:\s*)(\d+)%', line)
                if match:
                    notify('progress', {'percent': int(match.group(1))}, index, len(groups))
                elif line and not line.startswith('#GUI#progress'):
                    report('mkvmerge: ' + line, index)
            rc = proc.wait()
            if immediate_requested():
                result['aborted'] = True
                report('Sofortabbruch: ' + group.name, index)
                break
            # mkvmerge 1 = warning (successful file), 2 = error.
            if rc not in (0, 1) or not partial.is_file() or partial.stat().st_size == 0:
                raise RuntimeError(f'mkvmerge fehlgeschlagen (Rückgabewert {rc})')
            if output.exists():
                raise FileExistsError(f'Zieldatei während Verarbeitung angelegt: {output}')
            partial.rename(output)
            result['processed'] += 1
            notify('progress', {'percent': 100}, index, len(groups))
            report(('WARNUNG: ' if rc == 1 else 'Erfolgreich: ') + str(output), index)
        except Exception as exc:
            result['failed'] += 1
            report(f'FEHLER: {group.name}: {exc}', index)
        finally:
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait()
            # Only the temporary output of this run may be removed.
            try:
                partial.unlink(missing_ok=True)
            except OSError as exc:
                report(f'WARNUNG: temporäre Ausgabedatei konnte nicht gelöscht werden: {partial}: {exc}', index)
    report(f"Ende: verarbeitet={result['processed']}, übersprungen={result['skipped']}, Fehler={result['failed']}")
    return result
