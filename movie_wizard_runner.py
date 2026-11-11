"""Series wizard orchestration. Never changes or deletes source files."""
from __future__ import annotations
from linked_media_walk import linked_files
from original_cleanup import start_run, add_success
import json
from pathlib import Path
import tempfile
import traceback
import os
from datetime import datetime
from tool_manager import get_tool
from ffmpeg_manager import discover_from_config, probe_json
from recode_file import recode_file
from video_output_policy import output_path, configured_suffix
from recode_mission import make_mission
from codec_whitelist import wizard_video_mode, permitted
from matroska_tagger import tag_series
from ffmpeg_manager import FFmpegCancelled

VIDEO_EXT = {'.mkv', '.mk3d', '.mp4', '.ts'}

def find_videos(source: Path):
    return sorted((p for p in linked_files(source) if p.is_file() and p.suffix.lower() in VIDEO_EXT), key=lambda p: str(p).casefold())

def profile(video):
    h = int(video.get('height') or 0)
    return '4K' if h >= 1440 else '1080' if h >= 900 else '720' if h >= 650 else 'SD'

def run_movie_wizard(settings: dict, notify=lambda *args: None, stop_requested=lambda: False, immediate_requested=lambda: False, *, musicvideo=False):
    source = Path(settings['source']).expanduser().resolve()
    target = Path(settings['target']).expanduser().resolve()
    if not source.is_dir(): raise ValueError('Quellordner existiert nicht')
    if source == target or source in target.parents or target in source.parents:
        raise ValueError('Quell- und Zielordner dürfen nicht identisch oder ineinander verschachtelt sein')
    if not target.is_dir(): raise ValueError('Zielordner existiert nicht')
    files = find_videos(source)
    if not files: raise ValueError('Keine unterstützten Videodateien im Quellordner gefunden')
    cfg_file = Path(__file__).with_name('Media-Buddy.json')
    config = json.loads(cfg_file.read_text(encoding='utf-8-sig'))
    configured_suffix(config)  # validate selected container
    recode = config['Recode']
    recode['VideoCodec'] = settings['video_codec']
    recode['Quality'] = settings['quality']
    recode.setdefault('AudioProcessing', {})['Normalize'] = settings['normalize']
    recode['AudioProcessing']['NormalizeLevel'] = settings['normalize_level']
    recode['AudioProcessing']['DynamicCompression'] = settings['dynamic_compression']
    # Existing optimizer and language settings remain global until the per-wizard
    # optimizer schema is fully mapped. Never pretend they were applied.
    count = {'processed': 0, 'failed': 0, 'skipped': 0, 'tagged': 0, 'tag_errors': 0, 'attachments': 0, 'attachment_errors': 0}
    processed_outputs = []
    log_dir = Path(__file__).resolve().parent / 'Log'
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / (('wizard_musicvideos_' if musicvideo else 'wizard_movies_') + datetime.now().strftime('%Y-%m-%d_%H-%M-%S_%f') + '.log')
    def report(message, index=0, total=0):
        line = str(message)
        print('[WIZARD] ' + line, flush=True)
        with log_file.open('a', encoding='utf-8') as handle:
            handle.write(line + '\n')
        notify('log', line, index, total)
    start_run()
    report(f'Start: {len(files)} Quelldatei(en); Quelle={source}; Ziel={target}')
    with tempfile.TemporaryDirectory(prefix='media_buddy_wizard_') as temp:
        temporary_config = Path(temp) / 'config.json'
        temporary_config.write_text(json.dumps(config, ensure_ascii=False), encoding='utf-8')
        tools = discover_from_config(temporary_config)
        for i, src in enumerate(files, 1):
            if stop_requested() or immediate_requested():
                notify('aborted', 'Vor der nächsten Datei abgebrochen', i, len(files)); break
            relative = src.relative_to(source)
            notify('file', str(relative), i, len(files))
            try:
                metadata = probe_json(tools, src)
                videos = [s for s in metadata.get('streams', []) if s.get('codec_type') == 'video']
                if not videos: raise ValueError('Keine Videospur')
                dst = output_path(target, relative, config, videos[0])
                if dst.exists():
                    count['skipped'] += 1
                    report(f'Übersprungen (Zieldatei existiert): {dst}', i, len(files))
                    continue
                v = videos[0]
                key = profile(v)
                bitrate = int(v.get('bit_rate') or metadata.get('format', {}).get('bit_rate') or 0) // 1000
                threshold = int(settings['remux_thresholds_kbps'].get(key, 0))
                report(f'Videoentscheidung: {v.get("width", "?")}x{v.get("height", "?")}, Codec={v.get("codec_name", "?")}, Klasse={key}, Bitrate={bitrate} kbit/s, Grenzwert={threshold} kbit/s, Ausgabe={dst.suffix}', i, len(files))
                # Zero is unconfigured: choose recode, never silently remux.
                mode = wizard_video_mode(v.get('codec_name'), bitrate, threshold, config.get('VideoSettings', {}))
                dst.parent.mkdir(parents=True, exist_ok=True)
                mission = make_mission(src, dst, video_mode=mode,
                                       audio_mode=('recode' if settings['normalize'] else 'copy'),
                                       quality_level=int(settings['quality'][key]))
                report(f'Verarbeitung: {src} -> {dst}', i, len(files))
                recode_file(src, dst, config_path=temporary_config, mission=mission,
                            on_progress=lambda data: notify('progress', data, i, len(files)),
                            on_plan=lambda line: report(line, i, len(files)))
                if immediate_requested():
                    raise FFmpegCancelled('Sofortabbruch nach FFmpeg')
                count['processed'] += 1
                processed_outputs.append(dst)
                add_success(src, dst)
            except FFmpegCancelled:
                report(f'Sofortabbruch bei {src}', i, len(files))
                return {**count, 'aborted': True, 'log_file': str(log_file)}
            except Exception as exc:
                if immediate_requested():
                    report(f'Sofortabbruch bei {src}', i, len(files))
                    return {**count, 'aborted': True, 'log_file': str(log_file)}
                count['failed'] += 1
                report(f'FEHLER bei {src}: {exc}', i, len(files))
        if stop_requested() or immediate_requested():
            return {**count, 'aborted': True, 'log_file': str(log_file)}
        notify('phase', 'Musikvideo-Scraper' if musicvideo else 'Film-Scraper', len(files), len(files))
        import movie_scraper
        import musicvideo_scraper
        # Scan the entire output tree, including films from earlier or interrupted runs.
        overrides = {
            'Download_Images': bool(settings.get('download_images', True)),
            'Download_Movieset_Images': bool(settings.get('movieset', False)),
            'Overwrite': {
                'NFO': bool(settings.get('overwrite_nfo', False)),
                'Artwork': bool(settings.get('overwrite_artwork', False)),
            },
            'Movies': {'Download_Chapters': bool(settings.get('chapters', False)),
                       'Download_Trailer': bool(settings.get('trailer', False))},
        }
        if musicvideo:
            overrides.pop('Download_Movieset_Images', None)
            overrides['MusicVideos'] = {
                'Image_Thumb': bool(settings.get('thumb', True)),
                'Image_Poster': bool(settings.get('poster', True)),
                'Image_Fanart': bool(settings.get('fanart', True)),
                'Image_Banner': bool(settings.get('banner', True)),
                'Image_ClearLogo': bool(settings.get('clearlogo', True)),
            }
        completed=[]
        from scraper_progress import progress_receiver
        def scraper_update(event):
            notify('scraper_progress', event, len(files), len(files))
        try:
            from contextlib import redirect_stdout
            class ScraperLogStream:
                def __init__(self):
                    self.pending = ''
                def write(self, chunk):
                    self.pending += str(chunk)
                    while '\n' in self.pending:
                        line, self.pending = self.pending.split('\n', 1)
                        if line.strip():
                            with log_file.open('a', encoding='utf-8') as handle:
                                handle.write('[SCRAPER] ' + line + '\n')
                    return len(chunk)
                def flush(self):
                    if self.pending.strip():
                        with log_file.open('a', encoding='utf-8') as handle:
                            handle.write('[SCRAPER] ' + self.pending + '\n')
                    self.pending = ''
            scraper_stream = ScraperLogStream()
            with progress_receiver(scraper_update), redirect_stdout(scraper_stream):
                count['scraper_rc'] = (musicvideo_scraper.main if musicvideo else movie_scraper.main)(
                    [str(target)], wizard_overrides=overrides, completed_paths=completed)
            scraper_stream.flush()
        except Exception as exc:
            count['scraper_rc'] = -1
            report(f'Scraper fehlgeschlagen: {exc}', len(files), len(files))
        from matroska_tagger import tag_movies, tag_musicvideos
        try:
            executable=get_tool('mkvpropedit')
        except FileNotFoundError as exc:
            executable=None
            report(f'[TOOL ERROR] {exc}', len(files), len(files))
        notify('phase', 'Matroska-Tags', len(files), len(files))
        for index,video in enumerate(completed,1):
            if stop_requested() or immediate_requested():
                return {**count, 'aborted': True, 'log_file': str(log_file)}
            if not video.is_file() or video.suffix.lower() not in {'.mkv','.mk3d'}:
                continue
            if executable is None:
                count['tag_errors']+=1
                report(f'[TAG ERROR] mkvpropedit fehlt: {video}', len(files), len(files))
                continue
            try:
                result=(tag_musicvideos if musicvideo else tag_movies)(video,mkvpropedit=executable)
                if result.status=='tagged':
                    count['tagged']+=1
                    report(f'[TAG OK] {video}: {result.message}', len(files), len(files))
                elif result.status=='error':
                    count['tag_errors']+=1
                    report(f'[TAG ERROR] {video}: {result.message}', len(files), len(files))
            except Exception as exc:
                count['tag_errors']+=1
                report(f'[TAG ERROR] {video}: {exc}', len(files), len(files))
            notify('phase_progress', 100*index/max(1,len(completed)), len(files), len(files))
    report('Ergebnis: ' + json.dumps(count, ensure_ascii=False))
    return {**count, 'aborted': False, 'log_file': str(log_file)}


def run_musicvideo_wizard(settings: dict, notify=lambda *args: None,
                          stop_requested=lambda: False, immediate_requested=lambda: False):
    return run_movie_wizard(settings, notify, stop_requested, immediate_requested, musicvideo=True)
