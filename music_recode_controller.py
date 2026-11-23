"""Audio-only batch adapter for the existing WizardPage progress protocol."""
from pathlib import Path
from music_recode_engine import PROFILES, recode_file
from music_cue import find_cue_for_image, safe_track_name
from tool_manager import get_tool
from activity_log import event as activity_event

EXTENSIONS = {'.flac', '.mp3', '.wav', '.ogg', '.opus', '.m4a', '.aac', '.aiff', '.aif', '.wma'}

def run_music_recode_batch(settings, update, stop_after_file, stop_immediately):
    source, target = Path(settings['source']), Path(settings['target'])
    codec, quality = settings['music_codec'], int(settings['music_quality'])
    gain = float(settings.get('music_gain', 0))
    ffmpeg, ffprobe = get_tool('ffmpeg'), get_tool('ffprobe')
    if not ffmpeg or not ffprobe:
        return {'fatal': 'FFmpeg and FFprobe are required.'}
    files = sorted((p for p in source.rglob('*') if p.is_file() and p.suffix.lower() in EXTENSIONS), key=lambda p: str(p).casefold())
    success = failed = skipped = 0
    reserved = set()
    for index, item in enumerate(files, 1):
        if stop_after_file() or stop_immediately():
            break
        output = (target / item.relative_to(source)).with_suffix(PROFILES[codec].extension)
        cue = find_cue_for_image(item)
        if cue is not None:
            # Keep the image intact and create the split tracks in their own album folder.
            album_dir = target / item.relative_to(source).parent / item.stem
            ext = PROFILES[codec].extension
            destinations = [album_dir / safe_track_name(t, ext) for t in cue.tracks]
            if len(set(destinations)) != len(destinations):
                failed += 1
                activity_event(f'Music CUE ERROR: duplicate track filenames: {cue.cue}', 'ERROR')
                continue
            update('file', item.name, index, len(files))
            reserved.update(destinations)
            activity_event(f'Music CUE split: {cue.cue} -> {len(destinations)} tracks')
            for track_index, (track, destination) in enumerate(zip(cue.tracks, destinations)):
                if stop_after_file() or stop_immediately():
                    break
                end = cue.tracks[track_index + 1].start if track_index + 1 < len(cue.tracks) else None
                length = end - track.start if end is not None else None
                if destination.exists():
                    skipped += 1
                    activity_event(f'Music CUE track already exists; skipped: {destination}')
                    continue
                last_error = None
                for seek_mode in ('output', 'input', 'filter', 'reblock'):
                    if stop_immediately():
                        break
                    try:
                        recode_file(item, destination, codec=codec, quality=quality, gain_db=gain,
                                    ffmpeg=ffmpeg, ffprobe=ffprobe, include_cover=False,
                                    start_seconds=track.start, duration_seconds=length,
                                    track_metadata=track.metadata, seek_mode=seek_mode,
                                    stop_event=settings['_immediate_cancel_event'],
                                    progress=lambda pct, j=track_index, n=len(destinations), i=index, total=len(files):
                                        update('progress', {'percent': (j + pct / 100) / n * 100}, i, total))
                        success += 1
                        activity_event(f'Music CUE track OK ({seek_mode}): {item} -> {destination}')
                        last_error = None
                        break
                    except Exception as exc:
                        last_error = exc
                        if stop_immediately():
                            break
                        if seek_mode != 'reblock':
                            activity_event(f'Music CUE track {track.number}: FFmpeg {seek_mode} failed; retrying alternate seek: {exc}', 'WARNING')
                if last_error is not None and not stop_immediately():
                    failed += 1
                    activity_event(f'Music CUE track ERROR: {item} / {track.number}: {last_error}', 'ERROR')
            update('progress', {'percent': 100}, index, len(files))
            continue
        update('file', item.name, index, len(files))
        if output.exists() or output in reserved:
            skipped += 1
            activity_event(f'Music encode: output exists, skipped: {output}', 'WARNING')
            update('progress', {'percent': 100}, index, len(files))
            continue
        reserved.add(output)
        # The FFmpeg encoder receives the worker's immediate-cancel Event directly.
        # The after-file flag is intentionally NOT passed to the encoder.
        try:
            recode_file(item, output, codec=codec, quality=quality, gain_db=gain,
                        ffmpeg=ffmpeg, ffprobe=ffprobe,
                        include_cover=codec in ('mp3', 'flac', 'aac'),
                        stop_event=settings['_immediate_cancel_event'],
                        progress=lambda pct, i=index, n=len(files): update('progress', {'percent': pct}, i, n))
            success += 1
            activity_event(f'Music encode OK: {item} -> {output}')
        except Exception as exc:
            if stop_immediately():
                break
            failed += 1
            activity_event(f'Music encode ERROR: {item}: {exc}', 'ERROR')
        update('progress', {'percent': 100}, index, len(files))
    return {'processed': success, 'failed': failed, 'skipped': skipped,
            'aborted': bool(stop_after_file() or stop_immediately())}
