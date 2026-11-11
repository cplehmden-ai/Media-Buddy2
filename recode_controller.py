"""Independent recode-only batch controller. Source files are read-only."""
from __future__ import annotations
from linked_media_walk import linked_files
import json
import tempfile
from datetime import datetime
from pathlib import Path
from ffmpeg_manager import discover_from_config, probe_json, FFmpegCancelled
from recode_file import recode_file
from recode_mission import make_mission
from movie_wizard_runner import profile
from video_output_policy import output_path, configured_suffix

VIDEO_EXT = {'.mkv', '.mk3d', '.mp4', '.ts', '.avi', '.mpg', '.mpeg', '.mov', '.wmv', '.webm', '.vob', '.flv', '.m4v'}

def find_videos(source):
    return sorted((p for p in linked_files(source) if p.is_file() and p.suffix.lower() in VIDEO_EXT), key=lambda p: str(p).casefold())


def _run_batch(settings, notify=lambda *a: None, stop_requested=lambda: False, immediate_requested=lambda: False, *, remux=False, normalize_audio=False):
    source = Path(settings['source']).expanduser().resolve()
    target = Path(settings['target']).expanduser().resolve()
    if not source.is_dir() or not target.is_dir():
        raise ValueError('Quell- und Zielordner müssen existieren')
    if source == target or source in target.parents or target in source.parents:
        raise ValueError('Quell- und Zielordner dürfen nicht ineinander verschachtelt sein')
    files = find_videos(source)
    if not files:
        raise ValueError('Keine unterstützten Videos im Quellordner')
    cfg = json.loads(Path(__file__).with_name('Media-Buddy.json').read_text(encoding='utf-8-sig'))
    configured_suffix(cfg)  # validate before starting batch
    recode = cfg['Recode']
    recode['VideoCodec'] = settings['video_codec']
    recode['Quality'] = settings['quality']
    recode['VideoMode'] = 'copy' if (remux or normalize_audio) else 'recode'
    recode['AudioMode'] = 'copy' if remux else 'recode' if normalize_audio else settings['audio_mode']
    recode['ForceStreamCopy'] = bool(remux)
    recode['ForceAudioRecode'] = bool(normalize_audio)
    recode['AudioTargetCodec'] = settings.get('audio_codec', 'source') if normalize_audio else None
    # The legacy audio planner accepts only encoder codec IDs or 'copy'.
    # 'source' belongs exclusively to AudioTargetCodec in ForceAudioRecode mode.
    recode.setdefault('Audio', {})['AudioCodec'] = ('copy' if normalize_audio else settings.get('audio_codec', 'copy'))
    recode['Audio']['EverydayDefault'] = False if remux else bool(settings.get('everyday_track', True))
    audio = recode.setdefault('AudioProcessing', {})
    audio['Normalize'] = False if remux else bool(settings['normalize']) if normalize_audio else (bool(settings['normalize']) and settings['audio_mode'] == 'recode')
    audio['NormalizeLevel'] = settings['normalize_level']
    audio['DynamicCompression'] = 0 if remux else settings['dynamic_compression'] if (normalize_audio or settings['audio_mode'] == 'recode') else 0
    if remux:
        recode.setdefault('Cutlist', {})['Enabled'] = False
        # A COPY-only job must never be silently promoted by global policies.
        cfg.setdefault('VideoSettings', {})['VideoWhitelistEnabled'] = False
        recode.setdefault('Deinterlace', {})['Enabled'] = False
        recode.setdefault('Audio', {})['AudioCodec'] = 'copy'
        recode['Audio']['WhitelistEnabled'] = False
        recode['Audio']['CodecBlacklist'] = []
        recode['Audio']['ProtectionMode'] = 'off'
    if normalize_audio:
        recode.setdefault('Cutlist', {})['Enabled'] = False
        recode.setdefault('Deinterlace', {})['Enabled'] = False
        cfg.setdefault('VideoSettings', {})['VideoWhitelistEnabled'] = False
        recode['Audio']['ProtectionMode'] = 'off'
        recode['Audio']['WhitelistEnabled'] = False
        recode['Audio']['CodecBlacklist'] = []
    # Optimizer settings are applied to a temporary configuration, never the global file.
    opt = cfg.setdefault('Optimizer', {})
    opt['Enabled'] = False if normalize_audio else bool(settings['audio_optimizer'] or settings['subtitle_optimizer'])
    if opt['Enabled']:
        audio_languages = {'default': int(settings['audio_count'])}
        sub_languages = {'default': {'Normal': int(settings['subtitle_normal']), 'Forced': int(settings['subtitle_forced'])}}
        for code, limits in settings.get('extra_languages', {}).items():
            audio_languages[code] = int(limits['audio'])
            sub_languages[code] = {'Normal': int(limits['normal']), 'Forced': int(limits['forced'])}
        if settings['audio_optimizer']:
            opt.setdefault('Audio', {})['Languages'] = audio_languages
        if settings['subtitle_optimizer']:
            opt.setdefault('Subtitles', {})['Languages'] = sub_languages
    logdir = Path(__file__).with_name('Log')
    logdir.mkdir(exist_ok=True)
    logfile = logdir / (('remux_batch_' if remux else 'audio_normalize_batch_' if normalize_audio else 'recode_batch_') + datetime.now().strftime('%Y-%m-%d_%H-%M-%S_%f') + '.log')
    result = {'processed': 0, 'failed': 0, 'skipped': 0, 'log_file': str(logfile)}
    def report(message, i=0):
        with logfile.open('a', encoding='utf-8') as out:
            out.write(str(message) + '\n')
        notify('log', str(message), i, len(files))
    report(f"{'Remux' if remux else 'Audio-Normalisieren' if normalize_audio else 'Recode'}-Batch: {len(files)} Dateien; Quelle={source}; Ziel={target}")
    with tempfile.TemporaryDirectory(prefix='mb_recode_') as directory:
        config_path = Path(directory) / 'config.json'
        config_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding='utf-8')
        tools = discover_from_config(config_path)
        for i, src in enumerate(files, 1):
            if stop_requested() or immediate_requested():
                result['aborted'] = True
                break
            rel = src.relative_to(source)
            notify('file', str(rel), i, len(files))
            try:
                info = probe_json(tools, src)
                videos = [s for s in info.get('streams', []) if s.get('codec_type') == 'video']
                if not videos:
                    raise ValueError('Keine Videospur gefunden')
                dst = output_path(target, rel, cfg, videos[0])
                if dst.exists():
                    result['skipped'] += 1
                    report(f'Übersprungen: Zieldatei vorhanden: {dst}', i)
                    continue
                quality = int(settings['quality'][profile(videos[0])])
                dst.parent.mkdir(parents=True, exist_ok=True)
                mission = make_mission(src, dst, video_mode='copy' if (remux or normalize_audio) else 'recode', audio_mode='copy' if remux else 'recode' if normalize_audio else settings['audio_mode'], quality_level=None if (remux or normalize_audio) else quality)
                report(f"{'Remux' if remux else 'Audio-Normalisieren' if normalize_audio else 'Recode'}: {src} -> {dst}", i)
                recode_file(src, dst, config_path=config_path, mission=mission,
                            on_progress=lambda data, index=i: notify('progress', data, index, len(files)),
                            on_plan=lambda msg, index=i: report(msg, index))
                if immediate_requested():
                    raise FFmpegCancelled('Sofortabbruch')
                result['processed'] += 1
            except FFmpegCancelled:
                report(f'Abgebrochen: {src}', i)
                result['aborted'] = True
                break
            except Exception as exc:
                if immediate_requested():
                    result['aborted'] = True
                    break
                result['failed'] += 1
                report(f'FEHLER: {src}: {exc}', i)
    report(f"Ende: verarbeitet={result['processed']}, übersprungen={result['skipped']}, Fehler={result['failed']}")
    return result


def run_recode_batch(settings, notify=lambda *a: None, stop_requested=lambda: False, immediate_requested=lambda: False):
    return _run_batch(settings, notify, stop_requested, immediate_requested, remux=False)

def run_remux_batch(settings, notify=lambda *a: None, stop_requested=lambda: False, immediate_requested=lambda: False):
    return _run_batch(settings, notify, stop_requested, immediate_requested, remux=True)


def run_audio_normalize_batch(settings, notify=lambda *a: None, stop_requested=lambda: False, immediate_requested=lambda: False):
    return _run_batch(settings, notify, stop_requested, immediate_requested, normalize_audio=True)
