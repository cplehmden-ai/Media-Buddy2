"""Series wizard orchestration. Never changes or deletes source files."""
from __future__ import annotations
from linked_media_walk import linked_files
from original_cleanup import start_run, add_success
import json
from pathlib import Path
import tempfile
import traceback
import os
from series_part_join import classify, append_parts
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

def run_series_wizard(settings: dict, notify=lambda *args: None, stop_requested=lambda: False, immediate_requested=lambda: False):
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
    log_file = log_dir / ('wizard_series_' + datetime.now().strftime('%Y-%m-%d_%H-%M-%S_%f') + '.log')
    def report(message, index=0, total=0):
        line = str(message)
        print('[WIZARD] ' + line, flush=True)
        with log_file.open('a', encoding='utf-8') as handle:
            handle.write(line + '\n')
        notify('log', line, index, total)
    groups, unsafe, ordinary_files = classify(files, source)
    start_run()
    report(f'Start: {len(files)} Quelldatei(en); Quelle={source}; Ziel={target}')
    with tempfile.TemporaryDirectory(prefix='media_buddy_wizard_') as temp:
        temporary_config = Path(temp) / 'config.json'
        temporary_config.write_text(json.dumps(config, ensure_ascii=False), encoding='utf-8')
        tools = discover_from_config(temporary_config)
        # Explicit split episodes: encode every cutlisted part first, then append.
        # The individual pieces never enter the final library or the scraper.
        for paths, reason in unsafe:
            count['skipped'] += len(paths)
            report(f'Teilgruppe nicht automatisch verarbeitet: {reason}: ' + ', '.join(p.name for p in paths))
        for relative_dir, episode, paths in groups:
            if stop_requested() or immediate_requested():
                return {**count, 'aborted': True, 'log_file': str(log_file)}
            # Keep episode spelling from source; remove only the part letter.
            final_stem = paths[0].stem[:-1]
            final_dst = target / relative_dir / (final_stem + '.mkv')
            if final_dst.exists():
                count['skipped'] += len(paths)
                report(f'Teilgruppe übersprungen (fertige Episode existiert): {final_dst}')
                continue
            report(f'Teilgruppe erkannt: ' + ' + '.join(p.name for p in paths) + f' -> {final_dst.name}')
            try:
                with tempfile.TemporaryDirectory(prefix='mb_series_parts_') as part_temp:
                    intermediate = []
                    for number, src in enumerate(paths, 1):
                        if stop_requested() or immediate_requested():
                            raise InterruptedError('Abbruch vor nächstem Teil')
                        notify('file', f'{src.name} (Teil {number}/{len(paths)})', number, len(paths))
                        metadata = probe_json(tools, src)
                        videos = [stream for stream in metadata.get('streams', []) if stream.get('codec_type') == 'video']
                        if not videos:
                            raise ValueError(f'Keine Videospur: {src}')
                        v = videos[0]
                        key = profile(v)
                        bitrate = int(v.get('bit_rate') or metadata.get('format', {}).get('bit_rate') or 0) // 1000
                        threshold = int(settings['remux_thresholds_kbps'].get(key, 0))
                        mode = wizard_video_mode(v.get('codec_name'), bitrate, threshold, config.get('VideoSettings', {}))
                        part_dst = Path(part_temp) / f'part_{number:02d}.mkv'
                        mission = make_mission(src, part_dst, video_mode=mode,
                                               audio_mode=('recode' if settings['normalize'] else 'copy'),
                                               quality_level=int(settings['quality'][key]))
                        report(f'Teilstück mit Schnittliste verarbeiten: {src} -> {part_dst}')
                        recode_file(src, part_dst, config_path=temporary_config, mission=mission,
                                    on_progress=lambda data, n=number: notify('progress', data, n, len(paths)),
                                    on_plan=lambda line: report(line))
                        if immediate_requested():
                            raise InterruptedError('Sofortabbruch nach Recode')
                        intermediate.append(part_dst)
                    final_dst.parent.mkdir(parents=True, exist_ok=True)
                    rc = append_parts(intermediate, final_dst, get_tool('mkvmerge'), report, immediate_requested)
                    if rc == 1:
                        report(f'WARNUNG: mkvmerge meldet Warnungen für {final_dst}')
                count['processed'] += len(paths)
                processed_outputs.append(final_dst)
                for src in paths:
                    add_success(src, final_dst)
                report(f'Fertige zusammengefügte Episode: {final_dst}')
            except InterruptedError as exc:
                report(str(exc))
                return {**count, 'aborted': True, 'log_file': str(log_file)}
            except Exception as exc:
                count['failed'] += len(paths)
                report(f'Teilgruppe FEHLER ({episode}): {exc}')
        # Normal episodes remain on the original, proven wizard path.
        files = ordinary_files
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
        notify('phase', 'Serien-Scraper', len(files), len(files))
        import series_scraper as scraper
        # Der Scraper lädt sonst ausschließlich seine eigene JSON-Konfiguration.
        # Wizard-Schalter müssen für diesen Lauf explizit wirksam sein.
        scraper.load_test15_config()
        scraper.DOWNLOAD_IMAGES = bool(settings.get('download_images', True))
        scraper.DOWNLOAD_EPISODE_IMAGES = scraper.DOWNLOAD_IMAGES and bool(settings.get('thumb', True))
        scraper.OVERWRITE_ARTWORK = bool(settings.get('overwrite_artwork', False))
        scraper.OVERWRITE_NFO = bool(settings.get('overwrite_nfo', False))
        scraper.ARTWORK_WRITTEN.clear()
        report(f'Grafik-Einstellungen: Download={scraper.DOWNLOAD_IMAGES}, Episoden-Thumb={scraper.DOWNLOAD_EPISODE_IMAGES}, Überschreiben={scraper.OVERWRITE_ARTWORK}', len(files), len(files))
        from wizard_final_paths import identity, locate_processed_outputs
        output_identities = {path: identity(path) for path in processed_outputs}
        # Alle Serien im Ausgangsordner berücksichtigen, auch vor dem Lauf
        # vorhandene Episoden. Nur das spätere Tagging bleibt auf neu
        # verarbeitete Ausgaben beschränkt.
        series_roots = scraper.discover_series_roots(target)
        report(f'Serien-Scraper: {len(series_roots)} Serienordner im gesamten Ausgang gefunden', len(files), len(files))
        from scraper_progress import progress_receiver
        def scraper_update(event):
            notify('scraper_progress', event, len(files), len(files))
        try:
            with progress_receiver(scraper_update):
                count['scraper_rc'] = scraper.run_series_batch(target, roots_override=series_roots) if series_roots else 0
        except Exception as exc:
            count['scraper_rc'] = -1
            report(f'Serien-Scraper fehlgeschlagen: {exc}', len(files), len(files))
        # Nach dem Scraper anhand der ursprünglichen relativen Namen die finalen
        # Episoden ermitteln. Keine fremden MKVs im Zielarchiv nachbearbeiten.
        from matroska_tagger import tag_series
        try:
            executable = get_tool('mkvpropedit')
            report(f'[TOOL] mkvpropedit: {executable}', len(files), len(files))
        except FileNotFoundError as exc:
            executable = None
            report(f'[TOOL ERROR] {exc}', len(files), len(files))
        candidates, unresolved = locate_processed_outputs(processed_outputs, target, output_identities)
        for dst in unresolved:
            report(f'Endgültigen Episodenpfad nicht eindeutig gefunden: {dst}', len(files), len(files))
            count['tag_errors'] += 1
        notify('phase', 'Matroska-Tags', len(files), len(files))
        for tag_index, video in enumerate(candidates, 1):
            if stop_requested() or immediate_requested(): return {**count, 'aborted': True, 'log_file': str(log_file)}
            if video.suffix.lower() not in {'.mkv', '.mk3d'}: continue
            report(f'[TAG] Datei: {video}', len(files), len(files))
            if executable is None:
                count['tag_errors'] += 1
                report(f'[TAG ERROR] mkvpropedit fehlt: {video}', len(files), len(files))
                continue
            try:
                result = tag_series(video, mkvpropedit=executable)
            except Exception as exc:
                count['tag_errors'] += 1
                report(f'[TAG ERROR] {video}: {exc}', len(files), len(files))
                continue
            if result.status == 'tagged':
                count['tagged'] += 1
                report(f'[TAG OK] {video} ({result.message})', len(files), len(files))
            elif result.status == 'error':
                count['tag_errors'] += 1
                report(f'[TAG ERROR] {video}: {result.message}', len(files), len(files))
            if settings.get('mkv_metadata'):
                from matroska_attachments import embed_series_attachments
                try:
                    merge = get_tool('mkvmerge')
                    attach = embed_series_attachments(video, mkvpropedit=executable, mkvmerge=merge)
                except (OSError, ValueError) as exc:
                    from matroska_attachments import AttachmentResult
                    attach = AttachmentResult(False, message=str(exc))
                if attach.ok:
                    count['attachments'] += attach.count
                    report(f'[ATTACH OK] {video}: {attach.count} Anhang/Anhänge; {attach.message}', len(files), len(files))
                else:
                    count['attachment_errors'] += 1
                    report(f'[ATTACH ERROR] {video}: {attach.message}', len(files), len(files))
            notify('phase_progress', 100*tag_index/max(1,len(candidates)), len(files), len(files))
    report('Ergebnis: ' + json.dumps(count, ensure_ascii=False))
    return {**count, 'aborted': False, 'log_file': str(log_file)}
