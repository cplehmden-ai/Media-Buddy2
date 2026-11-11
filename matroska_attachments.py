"""Optional Matroska attachments; only per-episode NFO and thumbnail."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import json
import mimetypes
import shutil
import subprocess

@dataclass
class AttachmentResult:
    ok: bool
    count: int = 0
    message: str = ''

def embed_series_attachments(video: Path, *, mkvpropedit: str = 'mkvpropedit', mkvmerge: str | None = None) -> AttachmentResult:
    video = Path(video)
    if video.suffix.lower() not in ('.mkv', '.mk3d'):
        return AttachmentResult(True)
    nfo = video.with_suffix('.nfo')
    if not nfo.is_file():
        nfos = sorted(video.parent.glob(video.stem + '-S??E??.nfo'))
        nfo = nfos[0] if nfos else nfo
    thumb = video.with_name(video.stem + '-thumb.jpg')
    files = [p for p in (nfo, thumb) if p.is_file()]
    if not files:
        return AttachmentResult(True, message='Keine Episoden-Anhänge vorhanden')
    from tool_manager import find_tool
    edit = (find_tool('mkvpropedit', mkvpropedit)[0] if mkvpropedit != 'mkvpropedit'
            else find_tool('mkvpropedit')[0])
    if not edit:
        return AttachmentResult(False, message=f'mkvpropedit nicht gefunden: {mkvpropedit}')
    # Only delete known attachment names. Preserve every unrelated attachment.
    merge_candidate = Path(edit).with_name('mkvmerge.exe' if Path(edit).suffix.lower()=='.exe' else 'mkvmerge')
    merge = (find_tool('mkvmerge', mkvmerge)[0] if mkvmerge else None)
    if not merge:
        merge = str(merge_candidate) if merge_candidate.is_file() else find_tool('mkvmerge')[0]
    if not merge:
        return AttachmentResult(False, message='mkvmerge zur sicheren Prüfung vorhandener Anhänge nicht gefunden')
    try:
        info = subprocess.run([merge, '-J', str(video)], stdin=subprocess.DEVNULL, capture_output=True, text=True, errors='replace')
        if info.returncode:
            return AttachmentResult(False, message=info.stderr.strip() or 'mkvmerge -J fehlgeschlagen')
        attachments = json.loads(info.stdout).get('attachments', [])
        existing_names = {a.get('file_name') for a in attachments}
        args = [edit, str(video)]
        for item in files:
            if item.name in existing_names:
                args.extend(['--delete-attachment', 'name:' + item.name])
            mime = 'application/x-nfo' if item.suffix.lower()=='.nfo' else (mimetypes.guess_type(item.name)[0] or 'application/octet-stream')
            args.extend(['--attachment-name', item.name, '--attachment-mime-type', mime, '--add-attachment', str(item)])
        result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors='replace')
        if result.returncode:
            return AttachmentResult(False, message=result.stderr.strip() or result.stdout.strip())
        return AttachmentResult(True, len(files))
    except (OSError, ValueError) as exc:
        return AttachmentResult(False, message=str(exc))
