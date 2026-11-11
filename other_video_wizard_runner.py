"""Other-video wizard orchestration. Never changes or deletes source files."""
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
from matroska_tagger import tag_movies
from ffmpeg_manager import FFmpegCancelled

VIDEO_EXT = {'.mkv', '.mk3d', '.mp4', '.ts'}

def find_videos(source: Path):
    return sorted((p for p in linked_files(source) if p.is_file() and p.suffix.lower() in VIDEO_EXT), key=lambda p: str(p).casefold())

def profile(video):
    h = int(video.get('height') or 0)
    return '4K' if h >= 1440 else '1080' if h >= 900 else '720' if h >= 650 else 'SD'

def run_other_video_wizard(settings: dict, notify=lambda *args: None, stop_requested=lambda: False, immediate_requested=lambda: False):
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
    count = {'processed': 0, 'failed': 0, 'skipped': 0, 'tagged': 0, 'tag_errors': 0, 'attachments': 0, 'attachment_errors': 0, 'scraped': 0, 'scraper_skipped': 0, 'scraper_errors': 0}
    processed_outputs = []
    import other_video_scraper
    log_dir = Path(__file__).resolve().parent / 'Log'
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / ('wizard_other_videos_' + datetime.now().strftime('%Y-%m-%d_%H-%M-%S_%f') + '.log')
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
                # Artwork is generated exclusively beside the finished output.
                # Preserve the source file date for the poster/NFO year fallback.
                os.utime(dst, ns=(src.stat().st_atime_ns,src.stat().st_mtime_ns))
                options={
                    'make_poster':bool(settings.get('other_make_poster',True)),
                    'make_fanart':bool(settings.get('other_make_fanart',True)),
                    'make_nfo':bool(settings.get('other_make_nfo',True)),
                    'poster_count':int(settings.get('other_poster_count',1)),
                    'fanart_count':int(settings.get('other_fanart_count',6)),
                    'genre_from_folder':bool(settings.get('other_genre_from_folder',True)),
                    'mediainfo_plot':bool(settings.get('other_mediainfo_plot',True)),
                    'genre':settings.get('other_genre',''),
                    'plot':settings.get('other_plot',''),
                    'cover_template':settings.get('other_cover_template'),
                    'backdrop_template':settings.get('other_backdrop_template'),
                    'border_template':settings.get('other_border_template'),
                }
                try:
                    notify('phase', 'Sonstige-Videos-Scraper', i, len(files))
                    result=other_video_scraper.process(dst,target,target,options,
                        lambda message: report(message,i,len(files)))
                    if result=='ok':count['scraped']+=1
                    else:count['scraper_skipped']+=1
                except Exception as scraper_exc:
                    count['scraper_errors']+=1
                    report(f'[SCRAPER ERROR] {dst}: {scraper_exc}',i,len(files))
                if dst.suffix.lower() in ('.mkv', '.mk3d') and dst.is_file() and dst.with_suffix('.nfo').is_file():
                    notify('phase', 'Matroska-Tags', i, len(files))
                    try:
                        executable=get_tool('mkvpropedit')
                        tagged=tag_movies(dst,mkvpropedit=executable)
                        if tagged.status=='tagged':
                            count['tagged']+=1
                            report(f'[TAG OK] {dst}: {tagged.message}',i,len(files))
                        elif tagged.status=='error':
                            count['tag_errors']+=1
                            report(f'[TAG ERROR] {dst}: {tagged.message}',i,len(files))
                    except Exception as tag_exc:
                        count['tag_errors']+=1
                        report(f'[TAG ERROR] {dst}: {tag_exc}',i,len(files))
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
    report('Ergebnis: ' + json.dumps(count, ensure_ascii=False))
    return {**count, 'aborted': False, 'log_file': str(log_file)}
