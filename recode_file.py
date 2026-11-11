"""Media-Buddy V2: safe single-file recode engine (Test 72).

No source deletion, no batch/wizard logic. Intel QSV and software fallback supported.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import math
import time
import uuid
import os
import re
import csv
import tempfile
from hdr_pipeline import inspect as inspect_hdr, restore as restore_hdr, HDRPipelineError
from fractions import Fraction
from ffmpeg_manager import run_tool

from ffmpeg_manager import FFmpegError, discover_from_config, probe_json, run_ffmpeg
from progress_manager import ProgressTracker, ffmpeg_position, format_duration
from stream_optimizer import select_streams, normalize_language
from recode_audio_bridge import plan_selected_audio, describe_audio_plan
from audio_engine import rescue_plan
from frame_rate_manager import inspect_timing, encoder_timing_args, verify_timing, FrameRateError
from encoder_manager import CODECS, CODEC_NAMES, EncoderError, resolve_encoder
from quality_profiles import PROFILES, LABELS, codec_args
from recode_mission import RecodeMission, MissionError


class RecodeError(RuntimeError):
    pass


CONTAINERS = {'.mkv': 'matroska', '.mp4': 'mp4', '.ts': 'mpegts'}
# Text subtitle codecs are converted only when necessary. Image-based subtitles
# are extracted losslessly instead of using OCR or burning them into the video.
MP4_TEXT_SUBS = {'subrip', 'ass', 'ssa', 'webvtt', 'mov_text', 'text'}
TS_SUBS = {'dvb_subtitle'}

def _deinterlace_settings(options: dict, video: dict) -> dict:
    """Metadata-only interlace detection; no additional full-frame scan."""
    cfg = options.get('Deinterlace', {'Enabled': False})
    if not isinstance(cfg, dict) or not isinstance(cfg.get('Enabled', False), bool):
        raise RecodeError('Recode.Deinterlace.Enabled must be boolean')
    order = str(video.get('field_order') or 'unknown').lower()
    # ffprobe field_order values: progressive, tt, bb, tb, bt, unknown.
    detected = order in ('tt', 'bb', 'tb', 'bt')
    return {'enabled': cfg.get('Enabled', False), 'field_order': order,
            'detected': detected, 'applied': bool(cfg.get('Enabled', False) and detected),
            'filter': 'bwdif=mode=send_field:parity=auto:deint=all' if detected else None}


def _half_4k_settings(options: dict, video: dict) -> dict:
    cfg = options.get('Half4KFramerate', {'Enabled': False})
    if not isinstance(cfg, dict) or not isinstance(cfg.get('Enabled', False), bool):
        raise RecodeError('Recode.Half4KFramerate.Enabled must be boolean')
    try:
        fps = Fraction(video.get('avg_frame_rate') or video.get('r_frame_rate') or '0/0')
    except (ValueError, ZeroDivisionError):
        fps = Fraction(0)
    eligible = int(video.get('width') or 0) >= 3840 and int(video.get('height') or 0) >= 2160 and fps > 30
    return {'enabled': cfg['Enabled'], 'eligible': bool(eligible), 'source_fps': str(fps),
            'applied': False, 'filter': 'framestep=2'}


def _external_subtitle_settings(options: dict) -> bool:
    cfg = options.get('ExternalSubtitles', {'MuxIntoMKV': False})
    if not isinstance(cfg, dict) or not isinstance(cfg.get('MuxIntoMKV', False), bool):
        raise RecodeError('Recode.ExternalSubtitles.MuxIntoMKV must be boolean')
    return cfg['MuxIntoMKV']


def _find_external_subtitles(source: Path) -> list[dict]:
    # Exact video stem, optionally followed by .language and .forced markers.
    # No broad glob that could accidentally include subtitles for another movie.
    languages = {'de':'deu','deu':'deu','ger':'deu','en':'eng','eng':'eng',
                 'fr':'fra','fra':'fra','es':'spa','spa':'spa','it':'ita','ita':'ita'}
    found = []
    for path in sorted(source.parent.iterdir()):
        if not path.is_file() or path.suffix.lower() not in ('.srt', '.ass', '.ssa', '.vtt'):
            continue
        stem = path.stem
        if stem.casefold() == source.stem.casefold():
            tags = []
        elif stem.casefold().startswith(source.stem.casefold()+'.') or stem.casefold().startswith(source.stem.casefold()+'_'):
            tags = stem[len(source.stem)+1:].lower().replace('_', '.').split('.')
        else:
            continue
        if any(tag not in languages and tag not in ('forced','sdh','hi','default') for tag in tags):
            continue
        lang = next((languages[t] for t in tags if t in languages), 'und')
        found.append({'path':path,'language':lang,'forced':'forced' in tags,
                      'default':'default' in tags})
    return found



def _filter_external_subtitles(candidates: list[dict], config: dict,
                               embedded: list[dict], primary_language: str) -> list[dict]:
    """Apply shared Normal/Forced quotas across embedded and external subtitles.

    Embedded tracks have precedence; external files fill remaining quota.
    Disabled optimizer retains legacy behavior (all external sidecars).
    """
    optimizer = config.get('Optimizer', {'Enabled': False})
    if not optimizer.get('Enabled', True):
        return candidates
    raw = optimizer.get('Subtitles', {}).get('Languages',
                                            {'default': {'Normal': 0, 'Forced': 1}})
    quotas = {}
    for key, value in raw.items():
        lang = normalize_language(primary_language if key.lower() == 'default' else key)
        if lang in quotas:
            raise RecodeError(f'Duplicate subtitle language selection: {lang}')
        quotas[lang] = value
    used = {}
    for stream in embedded:
        lang = normalize_language((stream.get('tags') or {}).get('language'))
        forced = bool((stream.get('disposition') or {}).get('forced'))
        bucket = (lang, 'Forced' if forced else 'Normal')
        used[bucket] = used.get(bucket, 0) + 1
    selected = []
    for item in candidates:
        lang = normalize_language(item['language'])
        bucket = (lang, 'Forced' if item['forced'] else 'Normal')
        limit = quotas.get(lang, {}).get(bucket[1], 0)
        if used.get(bucket, 0) < limit:
            selected.append(item)
            used[bucket] = used.get(bucket, 0) + 1
    return selected


def _read_cutlist(source: Path, duration: float | None, settings: dict) -> tuple[Path | None, list[tuple[float,float]]]:
    if not settings.get('Enabled', True):
        return None, []
    candidates = [source.with_name(source.name + '.csv'), source.with_suffix('.csv')]
    found = [p for p in candidates if p.is_file()]
    if len(found) > 1:
        raise RecodeError(f'Ambiguous cutlists: {found}')
    if not found:
        return None, []
    path = found[0]
    def timestamp(raw: str) -> float:
        parts = raw.strip().split(':')
        try:
            if len(parts) == 3:
                h,m,sec = int(parts[0]),int(parts[1]),float(parts[2])
                value = h*3600+m*60+sec
            elif len(parts) == 1:
                value = float(parts[0])
            else:
                raise ValueError('invalid timestamp')
        except ValueError as exc:
            raise RecodeError(f'Invalid cut timestamp {raw!r} in {path}') from exc
        if not math.isfinite(value) or value < 0:
            raise RecodeError(f'Invalid cut timestamp {raw!r} in {path}')
        return value
    with path.open('r', encoding='utf-8-sig', newline='') as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames or not {'Start','End'}.issubset(reader.fieldnames):
            raise RecodeError(f'Cutlist needs Start,End columns: {path}')
        segments = []
        for number, row in enumerate(reader, 2):
            start, end = timestamp(row['Start'] or ''), timestamp(row['End'] or '')
            if end <= start or (segments and start < segments[-1][1]):
                raise RecodeError(f'Overlapping, unordered or empty segment at CSV line {number}')
            if duration is not None and end > duration + 0.15:
                raise RecodeError(f'Cutlist segment at line {number} ends after source duration ({duration:.3f}s)')
            segments.append((start,end))
    if not segments:
        raise RecodeError(f'Empty cutlist: {path}')
    return path, segments


def _cut_filter(segments: list[tuple[float,float]], audio_ordinals: list[int], audio_filter: str | None, video_filter: str | None = None) -> str:
    count = len(segments)
    filters = []
    filters.append(f'[0:v:0]split={count}' + ''.join(f'[vs{i}]' for i in range(count)))
    for a in audio_ordinals:
        filters.append(f'[0:a:{a}]asplit={count}' + ''.join(f'[as{a}_{i}]' for i in range(count)))
    for i, (start,end) in enumerate(segments):
        filters.append(f'[vs{i}]trim=start={start:.6f}:end={end:.6f},setpts=PTS-STARTPTS[v{i}]')
        for a in audio_ordinals:
            filters.append(f'[as{a}_{i}]atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS[a{a}_{i}]')
    for a in audio_ordinals:
        joined = ''.join(f'[a{a}_{i}]' for i in range(count))
        filters.append(f'{joined}concat=n={count}:v=0:a=1' + (f',{audio_filter}' if audio_filter else '') + f'[ac{a}]')
    joined = ''.join(f'[v{i}]' for i in range(count))
    filters.append(f'{joined}concat=n={count}:v=1:a=0' + (f',{video_filter}' if video_filter else '') + '[vc]')
    return ';'.join(filters)



def _metadata_timing(video: dict) -> dict:
    """Fast cutlist metadata: do not scan every frame of a long source."""
    raw = video.get('avg_frame_rate') or video.get('r_frame_rate') or '0/0'
    try:
        fps = Fraction(raw)
    except (ValueError, ZeroDivisionError):
        fps = Fraction(0)
    return {'mode': 'metadata_only', 'fps': str(fps) if fps > 0 else None,
            'frame_count': None, 'note': 'No full-source frame scan; exact source CFR/VFR not asserted'}


def _run_segmented_cut(tools, source: Path, destination: Path, segments,
                       selected_audio, encoder, quality_level, bit_depth, hdr_active, audio_plan,
                       audio_output_args, container, on_progress,
                       result, temp: Path, video_filter: str | None = None) -> None:
    """Encode seeked segments independently; losslessly concatenate identical streams."""
    import tempfile
    from progress_manager import ProgressTracker, ffmpeg_position
    from ffmpeg_manager import run_ffmpeg
    total = sum(end - start for start, end in segments)
    tracker = ProgressTracker(total)
    with tempfile.TemporaryDirectory(prefix='mb-cut-', dir=str(destination.parent)) as directory:
        folder = Path(directory)
        parts = []
        segment_commands = []
        accumulated = 0.0
        for number, (begin, end) in enumerate(segments, 1):
            length = end - begin
            part = folder / f'segment_{number:04d}{destination.suffix.lower()}'
            enc_args, _, _ = _quality_encoder_args(encoder, quality_level, bit_depth, hdr=hdr_active)
            args = ['-ss', f'{begin:.6f}', '-i', str(source),
                    '-map', '0:v:0',
                    *[x for track in audio_plan for x in ('-map', f'0:{track.source_index}')],
                    '-t', f'{length:.6f}', '-map_metadata', '-1', '-map_chapters', '-1',
                    *enc_args, *(['-vf', video_filter] if video_filter else []),
                    *audio_output_args(audio_plan),
                    '-f', container, str(part)]
            print(f'Cutlist segment {number}/{len(segments)}: '
                  f'{begin:.3f}s - {end:.3f}s ({length:.3f}s)', flush=True)
            segment_commands.append([str(tools.ffmpeg), '-nostdin', '-hide_banner',
                                     '-nostats', '-progress', 'pipe:1', *args])
            def progress(values, base=accumulated, seglen=length):
                current = min(seglen, max(0.0, ffmpeg_position(values) or 0.0))
                data = dict(values)
                data['duration_seconds'] = total
                data.update(tracker.update(min(total, base + current)))
                data['phase'] = 'encoding'
                if on_progress:
                    on_progress(data)
            run_ffmpeg(tools, args, on_progress=progress)
            if not part.is_file() or part.stat().st_size == 0:
                raise RecodeError(f'Empty segment output: {number}')
            parts.append(part)
            accumulated += length
        concat_list = folder / 'segments.ffconcat'
        # The list lives beside the temporary files, so only simple filenames are needed.
        concat_list.write_text('ffconcat version 1.0\n' +
                               ''.join(f"file '{part.name}'\n" for part in parts),
                               encoding='utf-8')
        print(f'Joining {len(parts)} segments without re-encoding...', flush=True)
        join_args = ['-f', 'concat', '-safe', '0', '-i', str(concat_list),
                     '-map', '0:v:0', '-map', '0:a?', '-c', 'copy',
                     *[x for i, track in enumerate(audio_plan) for x in (f'-disposition:a:{i}', 'default' if track.default else '0')],
                     '-f', container, str(temp)]
        run_ffmpeg(tools, join_args)
        result['segment_commands'] = segment_commands
        result['join_command'] = [str(tools.ffmpeg), '-nostdin', '-hide_banner',
                                  '-nostats', '-progress', 'pipe:1', *join_args]
        result['command'] = result['segment_commands']


def _subtitle_plan(subtitles: list[dict], suffix: str) -> tuple[list[int], list[dict], str]:
    embedded, external = [], []
    for stream in subtitles:
        index = stream['index']
        codec = stream.get('codec_name', '')
        if suffix == '.mkv':
            embedded.append(index)
        elif suffix == '.mp4' and codec in MP4_TEXT_SUBS:
            embedded.append(index)
        elif suffix == '.ts' and codec in TS_SUBS:
            embedded.append(index)
        else:
            if codec in ('hdmv_pgs_subtitle', 'pgssub'):
                ext, muxer = '.sup', 'sup'
            elif codec == 'dvd_subtitle':
                ext, muxer = '.sub', 'dvdsub'
            elif codec in ('subrip', 'srt'):
                ext, muxer = '.srt', 'srt'
            elif codec in ('ass', 'ssa'):
                ext, muxer = '.ass', 'ass'
            elif codec == 'webvtt':
                ext, muxer = '.vtt', 'webvtt'
            else:
                raise RecodeError(f'Cannot safely export subtitle codec {codec!r} (stream {index})')
            external.append({'index': index, 'codec': codec, 'extension': ext, 'muxer': muxer,
                             'language': re.sub('[^a-zA-Z0-9]', '', stream.get('tags', {}).get('language', 'und')) or 'und',
                             'forced': bool(stream.get('disposition', {}).get('forced'))})
    subtitle_codec = 'mov_text' if suffix == '.mp4' else 'copy'
    return embedded, external, subtitle_codec


def _audio_for_container(suffix: str, bitrate: int) -> tuple[str, int, list[str]]:
    if suffix == '.mp4':
        # E-AC-3 is supported in MP4 by FFmpeg; preserve the current codec.
        return 'eac3', bitrate, []
    if suffix == '.ts':
        # MPEG-TS accepts AC-3 reliably; E-AC-3 support varies among players.
        return 'ac3', min(bitrate, 640), ['MPEG-TS: E-AC-3 -> AC-3 compatibility fallback']
    return 'eac3', bitrate, []


def _quality_encoder_args(encoder: str, level: int, bit_depth: int, *, hdr: bool = False) -> tuple[list[str], str, int]:
    """Use the exact same seven-step profiles as quality_test.py.

    HDR color signalling and 10-bit output are controlled by the existing HDR
    pipeline; remove only the profile pixel-format option in that case.
    """
    aliases = {'libx264': 'x264', 'libx265': 'x265', 'libsvtav1': 'svt_av1'}
    name = aliases.get(encoder, encoder)
    if name not in PROFILES:
        raise RecodeError(f'No Q1-Q7 profile for encoder {encoder!r}; select a calibrated/supported encoder')
    args, value, depth = codec_args(name, level, bit_depth)
    if hdr:
        if '-pix_fmt' in args:
            idx = args.index('-pix_fmt')
            del args[idx:idx+2]
    return args, PROFILES[name].mode, value


def _source_bit_depth(video: dict) -> int:
    pix = str(video.get('pix_fmt') or '').lower()
    bits = video.get('bits_per_raw_sample')
    if bits and str(bits).isdigit() and int(bits) >= 10:
        return 10
    return 10 if ('10le' in pix or '10be' in pix or pix.startswith('p010')) else 8


def _video_profile(width: int, config: dict) -> tuple[str, int]:
    if width < 900:
        key = 'SD'
    elif width < 1400:
        key = '720'
    elif width < 2500:
        key = '1080'
    else:
        key = '4K'
    quality = config['Quality'][key]
    if not isinstance(quality, int) or isinstance(quality, bool) or not 1 <= quality <= 7:
        raise RecodeError(f'Invalid Q-level for {key}: {quality!r} (expected Q1..Q7)')
    return key, quality


def _audio_filter(options: dict) -> str | None:
    settings = options.get('AudioProcessing', {})
    if not isinstance(settings, dict):
        raise RecodeError('AudioProcessing must be an object')
    enabled = settings.get('Normalize', True)
    if not isinstance(enabled, bool):
        raise RecodeError('AudioProcessing.Normalize must be boolean')
    compression = settings.get('DynamicCompression', 2)
    if isinstance(compression, bool) or not isinstance(compression, int) or compression not in range(5):
        raise RecodeError('DynamicCompression must be an integer from 0 to 4')
    if not enabled and compression == 0:
        return None
    if not enabled:
        # Compression is independent of normalization. The legacy dynaudnorm
        # s= parameter only has an effect while dynaudnorm itself is enabled.
        # Use a dedicated compressor when normalization is disabled.
        profiles = {
            1: (-18, 2),
            2: (-21, 3),
            3: (-24, 4),
            4: (-27, 6),
        }
        threshold_db, ratio = profiles[compression]
        threshold = 10 ** (threshold_db / 20)
        return f'acompressor=threshold={threshold:.6f}:ratio={ratio}:attack=20:release=250:makeup=1'
    level = settings.get('NormalizeLevel', 0.95)
    if isinstance(level, bool) or not isinstance(level, (int, float)) or not math.isfinite(level) or not 0 < level <= 1:
        raise RecodeError('NormalizeLevel must be > 0 and <= 1')
    level_text = f'{level:g}'
    suffix = {0:'',1:':s=22',2:':s=15',3:':s=11',4:':s=7'}[compression]
    return f'dynaudnorm=p={level_text}:f=500:m=10{suffix}:g=11:r={level_text}:b=false'


def _duration(info: dict) -> float | None:
    try:
        value = float(info.get('format', {}).get('duration', 0))
        return value if math.isfinite(value) and value > 0 else None
    except (TypeError, ValueError):
        return None


def _check_output(input_info: dict, output_info: dict, expected_video: int, expected_audio: int,
                  expected_subtitle: int, duration: float | None, expected_codec: str = "hevc", expected_audio_codec: str = 'eac3', cut_mode: bool = False, external_subtitles_added: bool = False) -> None:
    streams = output_info.get('streams', [])
    def count(kind):
        return sum(s.get('codec_type') == kind for s in streams)
    if count('video') != expected_video or count('audio') != expected_audio or count('subtitle') != expected_subtitle:
        raise RecodeError('Output verification failed: stream counts differ')
    if any(s.get('codec_name') != expected_codec for s in streams if s.get('codec_type') == 'video'):
        raise RecodeError(f'Output verification failed: {expected_codec} missing')
    if ([s.get('codec_name') for s in streams if s.get('codec_type') == 'audio'] != expected_audio_codec if isinstance(expected_audio_codec, list) else any(s.get('codec_name') != expected_audio_codec for s in streams if s.get('codec_type') == 'audio')):
        raise RecodeError(f'Output verification failed: {expected_audio_codec} missing')
    source_video = [s for s in input_info['streams'] if s.get('codec_type') == 'video' and not s.get('disposition', {}).get('attached_pic')]
    target_video = [s for s in streams if s.get('codec_type') == 'video']
    for a,b in zip(source_video,target_video):
        if (a.get('width'),a.get('height')) != (b.get('width'),b.get('height')):
            raise RecodeError('Output verification failed: resolution differs')
    out_duration = _duration(output_info)
    if not external_subtitles_added and duration is not None and (out_duration is None or abs(out_duration-duration) > (0.25 if cut_mode else max(2.0, duration*0.01))):
        raise RecodeError('Output verification failed: duration differs')


def _verify_copied_dv_metadata(source_video: dict, output_info: dict) -> dict:
    """Fail closed if DV profile/compatibility signaling disappears on video copy.

    This is a metadata check, not proof of byte-identical BL/EL/RPU payloads.
    """
    def dv_entry(stream):
        return next((item for item in stream.get('side_data_list', [])
                     if 'DOVI' in str(item.get('side_data_type', '')).upper()), None)

    src = dv_entry(source_video)
    if src is None:
        return {'dolby_vision_signaling_checked': False}
    output_video = next((v for v in output_info.get('streams', [])
                         if v.get('codec_type') == 'video' and not v.get('disposition', {}).get('attached_pic')), None)
    dst = dv_entry(output_video or {})
    if dst is None:
        raise RecodeError('Dolby Vision metadata missing after video streamcopy; refusing output')
    for key in ('dv_version_major', 'dv_version_minor', 'dv_profile', 'dv_level',
                'rpu_present_flag', 'el_present_flag', 'bl_present_flag',
                'dv_bl_signal_compatibility_id'):
        if src.get(key) is not None and str(src[key]) != str(dst.get(key)):
            raise RecodeError(f'Dolby Vision {key} changed during streamcopy: {src[key]} -> {dst.get(key)}')
    return {'dolby_vision_signaling_checked': True,
            'profile': src.get('dv_profile'),
            'compatibility_id': src.get('dv_bl_signal_compatibility_id'),
            'note': 'Profile and compatibility signaling verified; BL/EL/RPU bitstream identity not yet verified'}


def _recode_file_impl(source: str | Path, destination: str | Path, *,
                config_path: str | Path | None = None, on_progress=None,
                dry_run: bool = False, encoder_override: str | None = None,
                allow_fallback: bool | None = None,
                full_frame_analysis: bool | None = None, _hdr_work: Path | None = None,
                mission: RecodeMission | None = None, on_plan=None) -> dict:
    source = Path(source).expanduser().resolve()
    destination = Path(os.path.abspath(Path(destination).expanduser()))
    if not source.is_file():
        raise RecodeError(f'Input file does not exist: {source}')
    if source == destination:
        raise RecodeError('Input and output must be different files')
    suffix = destination.suffix.lower()
    if suffix not in CONTAINERS:
        raise RecodeError('Test 76 supports .mkv, .mp4 and .ts output')
    if not destination.parent.is_dir():
        raise RecodeError(f'Output directory does not exist: {destination.parent}')
    config_path = Path(config_path) if config_path else Path(__file__).with_name('Media-Buddy.json')
    config = json.loads(config_path.read_text(encoding='utf-8-sig'))
    options = config['Recode']
    force_audio_recode = options.get('ForceAudioRecode', False)
    if not isinstance(force_audio_recode, bool):
        raise RecodeError('Recode.ForceAudioRecode must be boolean')
    audio_target_codec = options.get('AudioTargetCodec')
    if force_audio_recode and audio_target_codec not in ('source', 'ac3', 'eac3', 'aac', 'mp3', 'flac', 'opus', 'dts'):
        raise RecodeError('Invalid AudioTargetCodec for audio normalization')
    force_stream_copy = options.get('ForceStreamCopy', False)
    if not isinstance(force_stream_copy, bool):
        raise RecodeError('Recode.ForceStreamCopy must be boolean')
    configured_analysis = options.get('FullFrameAnalysis', False)
    if not isinstance(configured_analysis, bool):
        raise RecodeError('Recode.FullFrameAnalysis must be boolean')
    exhaustive_analysis = configured_analysis if full_frame_analysis is None else full_frame_analysis
    video_mode_requested = mission.video_mode if mission else options.get('VideoMode', 'recode')
    video_mode = video_mode_requested
    audio_mode_requested = mission.audio_mode if mission else options.get('AudioMode', 'recode')
    if video_mode not in ('copy', 'recode'):
        raise RecodeError('Recode.VideoMode must be copy or recode')
    if audio_mode_requested not in ('copy', 'recode'):
        raise RecodeError('Recode.AudioMode must be copy or recode')
    if force_audio_recode and (video_mode_requested != 'copy' or audio_mode_requested != 'recode' or force_stream_copy):
        raise RecodeError('ForceAudioRecode requires video copy and audio recode')
    if force_stream_copy and (video_mode_requested != 'copy' or audio_mode_requested != 'copy'):
        raise RecodeError('ForceStreamCopy requires VideoMode=copy and AudioMode=copy')
    encoder = encoder_override or options.get('VideoEncoder', 'libx265')
    codec = options.get('VideoCodec', 'hevc')
    backend = options.get('EncoderBackend', 'auto')
    hardware_enabled = options.get('HardwareEncoding', True)
    if not isinstance(hardware_enabled,bool):
        raise RecodeError('Recode.HardwareEncoding must be boolean')
    # Legacy VideoEncoder remains supported when VideoCodec/EncoderBackend are absent.
    legacy = options.get('VideoEncoder') if 'VideoCodec' not in options and not encoder_override else None
    # Encoder selection is deferred until Dolby Vision safety rules have chosen
    # the effective mode. Avoid probing hardware for a forced streamcopy.
    software_encoder = CODECS[codec]['software']
    fallback_enabled = options.get('SoftwareFallback', True) if allow_fallback is None else allow_fallback
    if not isinstance(fallback_enabled, bool):
        raise RecodeError('Recode.SoftwareFallback must be boolean')
    tools = discover_from_config(config_path)
    metadata = probe_json(tools, source)
    streams = metadata.get('streams', [])
    # Global video whitelist is authoritative even for COPY missions.
    from codec_whitelist import permitted
    global_video_settings = config.get('VideoSettings', {})
    if not isinstance(global_video_settings, dict):
        raise RecodeError('VideoSettings must be an object')
    video_whitelist_enabled = bool(global_video_settings.get('VideoWhitelistEnabled', False))
    video_whitelist = global_video_settings.get('VideoCodecWhitelist', [])
    if video_whitelist_enabled and not isinstance(video_whitelist, list):
        raise RecodeError('VideoSettings.VideoCodecWhitelist must be a list')
    source_video_stream = next((item for item in streams if item.get('codec_type') == 'video'
                                and not item.get('disposition', {}).get('attached_pic')), None)
    if not force_stream_copy and source_video_stream is not None and not permitted(source_video_stream.get('codec_name'),
                                                       video_whitelist_enabled, video_whitelist):
        if video_mode == 'copy':
            print('[WHITELIST] Global video whitelist overrides COPY: ' +
                  str(source_video_stream.get('codec_name')) + ' -> RECODE', flush=True)
        video_mode_requested = video_mode = 'recode'
    videos = [s for s in streams if s.get('codec_type') == 'video' and not s.get('disposition', {}).get('attached_pic')]
    audios = [s for s in streams if s.get('codec_type') == 'audio']
    if not videos:
        raise RecodeError('No video stream in input')
    # Silent video sources are valid (trailers, DV test patterns, video-only captures).
    video = videos[0]
    width = int(video.get('width') or 0)
    if width <= 0:
        raise RecodeError('Cannot determine video width')
    profile, quality_level = _video_profile(width, options)
    if mission and mission.quality_level is not None:
        quality_level = mission.quality_level
    source_bit_depth = _source_bit_depth(video)
    source_timing = None  # Analyze only after checking whether a cutlist exists.
    bitrate = options['AudioBitrateKbps']
    if not isinstance(bitrate, int) or isinstance(bitrate, bool) or not 64 <= bitrate <= 1536:
        raise RecodeError('Invalid audio bitrate')
    audio_filter = _audio_filter(options)
    if force_stream_copy and audio_filter:
        raise RecodeError('ForceStreamCopy forbids audio filters')
    # A whitelist permits retaining the *codec*, not bypassing requested DSP.
    # Audio processing requires decoding and re-encoding even for allowed codecs.
    audio_mode = 'recode' if audio_filter else audio_mode_requested
    mode_notes = []
    if audio_filter and audio_mode_requested == 'copy':
        mode_notes.append('AudioMode=copy overridden: audio processing requires re-encoding')
    # Test93: decide safe effective mode/container from source metadata FIRST.
    dv_record = next((item for item in video.get('side_data_list', [])
                      if 'DOVI' in str(item.get('side_data_type', '')).upper()), None)
    dv_profile = int(dv_record['dv_profile']) if dv_record and dv_record.get('dv_profile') is not None else None
    deinterlace = _deinterlace_settings(options, video)
    half_4k = _half_4k_settings(options, video)
    mux_external = _external_subtitle_settings(options)
    dv_settings = options.get('DolbyVision', {})
    if not isinstance(dv_settings, dict) or not isinstance(dv_settings.get('AllowProfile7To81Recode', False), bool):
        raise RecodeError('Recode.DolbyVision.AllowProfile7To81Recode must be boolean')
    allow_p7 = dv_settings.get('AllowProfile7To81Recode', False)
    # Test95I: mismatched source RPU counts are not proof of safe alignment.
    # Fallback to untouched video, never invent Dolby Vision metadata.
    dv_fallback_reason = None
    p7_conversion = bool(dv_profile == 7 and allow_p7 and video_mode_requested == 'recode')
    requested_destination = destination
    if dv_profile == 20:
        video_mode = 'copy'
        if suffix != '.mp4':
            destination = destination.with_suffix('.mp4')
            suffix = '.mp4'
            mode_notes.append(f'Dolby Vision profile 20: output changed to {destination.name} (MP4 required)')
        if video_mode_requested != 'copy':
            mode_notes.append('Dolby Vision profile 20: VideoMode overridden to copy')
    elif dv_profile is not None and video_mode_requested == 'recode':
        if dv_profile == 7 and allow_p7:
            if video.get('codec_name') != 'hevc' or codec != 'hevc':
                raise RecodeError('P7 to P8.1 recode requires HEVC source and HEVC output')
            video_mode = 'recode'
            mode_notes.append('Dolby Vision profile 7: explicit permission; re-encode base layer as profile 8.1 (EL discarded)')
        elif dv_profile not in (8,):
            video_mode = 'copy'
            mode_notes.append(f'Dolby Vision profile {dv_profile}: VideoMode overridden to copy to preserve DV layers')
        elif dv_profile == 8 and int(dv_record.get('dv_bl_signal_compatibility_id') or 0) not in (1, 4):
            video_mode = 'copy'
            mode_notes.append('Unknown Dolby Vision profile 8 compatibility: preserving original via streamcopy')
    # Preflight the known dynamic-HDR constraints before inspect_hdr() extracts
    # original.hevc / original.rpu. FFprobe's DV side data is sufficient for
    # this decision; HDR10+ side data is checked when available as well.
    early_dynamic_hdr = bool(dv_record) or any(
        'HDR10+' in str(item.get('side_data_type', '')).upper()
        for item in video.get('side_data_list', [])
    )
    if early_dynamic_hdr and video_mode == 'recode' and (codec != 'hevc' or suffix != '.mkv'):
        raise RecodeError(
            'Dolby Vision/HDR10+: re-encoding requires HEVC/H.265 output in MKV; '
            f'configured output codec={codec}, output container={suffix}. '
            'Please check the output codec and container settings.'
        )
    if destination == source:
        raise RecodeError('Effective output path would overwrite input')
    if destination.is_symlink() or (destination.exists() and os.path.samefile(source, destination)):
        raise RecodeError('Output must not be a symlink or alias of the input file')
    if video_mode == 'copy':
        encoder, selected_backend, encoder_notes = 'copy', 'copy', []
    else:
        encoder, selected_backend, encoder_notes = resolve_encoder(
            tools.ffmpeg, codec, backend, hardware_enabled=hardware_enabled,
            requested=encoder_override or legacy, verify_hardware=not dry_run)
    if video_mode == 'copy' and encoder_override:
        mode_notes.append('Explicit encoder override ignored: Dolby Vision requires streamcopy')
    hdr_info = {'format': 'SDR', 'dolby_vision': bool(dv_record), 'hdr10plus': False}
    transfer = video.get('color_transfer')
    hdr_info['format'] = 'HLG' if transfer == 'arib-std-b67' else 'HDR10' if transfer == 'smpte2084' else ('Dolby Vision' if dv_record else 'SDR')
    if video_mode == 'recode' and video.get('codec_name') == 'hevc':
        try:
            hdr_info = inspect_hdr(source, video, tools.ffmpeg, tools.ffprobe,
                                   Path(__file__).resolve().parent, _hdr_work,
                                   convert_p7=p7_conversion)
        except HDRPipelineError as exc:
            if not p7_conversion:
                raise
            dv_fallback_reason = str(exc)
            print(f'[HDR WARNING] P7 -> P8.1 preflight failed: {exc}', file=sys.stderr, flush=True)
            print('[HDR WARNING] Falling back to original P7 video streamcopy.', file=sys.stderr, flush=True)
            video_mode = 'copy'
            p7_conversion = False
            encoder, selected_backend, encoder_notes = 'copy', 'copy', []
            mode_notes.append('WARNING: P7 -> P8.1 preflight rejected; original P7 streamcopy fallback')
            hdr_info = {'format': 'HDR10' if transfer == 'smpte2084' else 'Dolby Vision',
                        'dolby_vision': True, 'hdr10plus': False}
    elif video.get('codec_name') == 'hevc':
        hdr_info['hdr10plus'] = any('HDR10+' in str(item.get('side_data_type', '')).upper()
                                    for item in video.get('side_data_list', []))
    dynamic_hdr = bool(hdr_info['dolby_vision'] or hdr_info['hdr10plus'])
    if deinterlace['applied'] and dynamic_hdr:
        # send_field changes picture count: source dynamic HDR metadata would
        # no longer map one-to-one to output pictures. Refuse unsafe injection.
        deinterlace['applied'] = False
        deinterlace['skipped_reason'] = 'Dynamic HDR requires original picture-to-metadata mapping'
        mode_notes.append('Deinterlacing skipped: dynamic HDR picture/RPU mapping must be preserved')
    if deinterlace['applied'] and video_mode == 'copy':
        video_mode = 'recode'
        encoder, selected_backend, encoder_notes = resolve_encoder(
            tools.ffmpeg, codec, backend, hardware_enabled=hardware_enabled,
            requested=encoder_override or legacy, verify_hardware=not dry_run)
        mode_notes.append('Interlaced source: streamcopy overridden by bwdif recode')
    if deinterlace['applied']:
        mode_notes.append(f"Deinterlace: {deinterlace['field_order']} -> progressive (bwdif send_field)")

    # Frame dropping invalidates per-picture DV/HDR10+ metadata alignment.
    if half_4k['enabled'] and half_4k['eligible']:
        if dynamic_hdr:
            half_4k['skipped_reason'] = 'Dynamic HDR frame metadata cannot safely be halved'
            mode_notes.append('4K frame halving skipped: dynamic HDR metadata alignment')
        elif deinterlace['detected']:
            half_4k['skipped_reason'] = 'Interlaced 4K source unsupported'
            mode_notes.append('4K frame halving skipped: interlaced source')
        elif video_mode_requested == 'copy':
            half_4k['skipped_reason'] = 'Explicit Remux keeps original video frames'
            mode_notes.append('4K frame halving skipped: VideoMode=copy')
        else:
            half_4k['applied'] = True
            mode_notes.append(f"4K frame halving: {half_4k['source_fps']} fps -> half (framestep=2)")
    active_video_filter = (deinterlace['filter'] if deinterlace['applied'] else
                           half_4k['filter'] if half_4k['applied'] else None)

    if dynamic_hdr and video_mode == 'recode' and (codec != 'hevc' or suffix != '.mkv'):
        raise RecodeError(
            'Dolby Vision/HDR10+: re-encoding requires HEVC/H.265 output in MKV; '
            f'configured output codec={codec}, output container={suffix}. '
            'Please check the output codec and container settings.'
        )
    duration = _duration(metadata)
    cut_settings = options.get('Cutlist', {})
    if not isinstance(cut_settings, dict):
        raise RecodeError('Recode.Cutlist must be an object')
    cutlist_path, segments = _read_cutlist(source, duration, cut_settings)
    cutting = bool(segments)
    if cutting and dynamic_hdr:
        print('WARNING: Dolby Vision/HDR10+ detected: ignoring LosslessCut cutlist; encoding full source.', flush=True)
        cutting = False
        segments = []
    if cutting:
        source_timing = _metadata_timing(video)
        print(f"Cutlist detected: {len(segments)} segments; skipping full-source frame scan.", flush=True)
    elif exhaustive_analysis:
        print("Analyzing full-source frame timing (diagnostic mode)...", flush=True)
        source_timing = inspect_timing(tools, source, video)
    else:
        print("Fast metadata analysis (full frame scan disabled).", flush=True)
        source_timing = _metadata_timing(video)
    if cutting and len(videos) != 1:
        raise RecodeError('Test 78 cutlists require exactly one video stream')
    if cutting and video_mode == 'copy':
        video_mode = 'recode'
        encoder, selected_backend, encoder_notes = resolve_encoder(
            tools.ffmpeg, codec, backend, hardware_enabled=hardware_enabled,
            requested=encoder_override or legacy, verify_hardware=not dry_run)
    if cutting:
        # Accurate cuts cannot be achieved with audio stream copy either.
        audio_mode = 'recode' if audios else 'none'
        if audio_mode_requested == 'copy':
            mode_notes.append('AudioMode=copy overridden: accurate segment cutting requires audio re-encoding')
        if video_mode_requested == 'copy':
            mode_notes.append('VideoMode=copy overridden: frame-accurate cutlist requires video re-encoding')
    output_duration = sum(end-start for start,end in segments) if cutting else duration
    subtitles = [s for s in streams if s.get('codec_type') == 'subtitle']
    video_count = len(videos)
    selection = select_streams(metadata, config.get('Optimizer', {'Enabled': False}),
                               config.get('Language_ThreeLetter') or config.get('Language') or 'en')
    selected_audio = selection['audio_indexes']
    mission_track_rules = {t['SourceIndex']: t for t in mission.audio_tracks} if mission and mission.audio_tracks else None
    if mission_track_rules is not None:
        available = {int(x['index']) for x in audios}
        if not set(mission_track_rules) <= available:
            raise RecodeError('AudioTracks references unavailable source audio index')
        # Explicit track selection overrides the language optimizer; preserve user order.
        selected_audio = list(mission_track_rules)
        selection['audio_indexes'] = selected_audio
        selection['audio_reason'] = 'mission_explicit_audio_tracks'

    # Test 100-11: production FFmpeg commands consume the validated audio plan.
    audio_settings = options.get('Audio', {})
    if not isinstance(audio_settings, dict):
        raise RecodeError('Recode.Audio must be an object')
    audio_config = dict(audio_settings)
    audio_policy_preview = plan_selected_audio(
        metadata, selected_audio, options, audio_config,
        processing_enabled=bool(audio_filter) or cutting or audio_mode_requested == 'recode')
    audio_plan = list(audio_policy_preview)
    if force_audio_recode:
        from audio_plan import AudioOutput, CODECS as AUDIO_CODECS, MAX_CHANNELS, channel_details
        fallback_codec = str(audio_config.get('FallbackCodec', 'eac3')).lower()
        if fallback_codec not in AUDIO_CODECS:
            raise RecodeError('Invalid global audio fallback codec')
        normalized_plan = []
        for source_index in selected_audio:
            stream = next(s for s in audios if int(s['index']) == source_index)
            source_codec = str(stream.get('codec_name') or '').lower()
            desired = source_codec if audio_target_codec == 'source' else audio_target_codec
            if desired not in AUDIO_CODECS:
                desired = fallback_codec
                reason = f'Fallback: original codec {source_codec} cannot be encoded'
            else:
                reason = 'Explicit audio normalization codec selection'
            channels, _, _, layout_note = channel_details(stream)
            target_channels = min(channels, MAX_CHANNELS[desired]) if channels and desired in MAX_CHANNELS else channels
            normalized_plan.append(AudioOutput(source_index, desired, False, len(normalized_plan) == 0,
                'processed', reason, channels, target_channels, layout_note))
        audio_plan = normalized_plan
        audio_policy_preview = list(audio_plan)
    if force_stream_copy:
        from audio_plan import AudioOutput
        audio_plan = [AudioOutput(int(stream['index']), 'copy', True, index == 0,
                      'original', 'ForceStreamCopy: preserve source codec and channels',
                      stream.get('channels'), stream.get('channels'))
                      for index, stream in enumerate(audios) if int(stream['index']) in selected_audio]
        audio_plan.sort(key=lambda item: selected_audio.index(item.source_index))
        audio_plan = [AudioOutput(t.source_index, t.codec, True, i == 0, t.purpose,
                      t.reason, t.source_channels, t.target_channels, t.channel_note)
                      for i, t in enumerate(audio_plan)]
        audio_policy_preview = list(audio_plan)
    if mission_track_rules is not None and force_stream_copy:
        raise RecodeError('ForceStreamCopy does not support per-track re-encoding rules')
    if mission_track_rules is not None:
        from audio_plan import plan_audio, AudioOutput
        per_track = []
        for source_index in selected_audio:
            rule = mission_track_rules[source_index]
            stream = next(x for x in audios if x['index'] == source_index)
            wants_filter = rule.get('Normalize', bool(audio_filter))
            if wants_filter and not audio_filter:
                raise RecodeError('AudioTracks requests normalization but global audio filter is disabled')
            mode = rule['Mode']
            config_for_track = dict(audio_config)
            if rule.get('KeepHiResOriginal') is False:
                config_for_track['ProtectionMode'] = 'off'
            elif rule.get('KeepHiResOriginal') is True:
                config_for_track['ProtectionMode'] = 'additional'
            if mode == 'copy':
                config_for_track['AudioCodec'] = 'copy'
                # An explicit copy request is authoritative unless codec policy forbids it.
                config_for_track['ProtectionMode'] = 'off'
                config_for_track['CodecWhitelist'] = []
                config_for_track['WhitelistEnabled'] = False
                config_for_track['CodecBlacklist'] = []
            planned = plan_audio([stream], config_for_track,
                                 processing_enabled=(mode == 'recode' or wants_filter or cutting))
            for item in planned:
                per_track.append((item, wants_filter and not item.copy))
        if per_track:
            # One and only one default, respecting the user's source order.
            audio_plan = [AudioOutput(t.source_index, t.codec, t.copy, i == 0,
                        t.purpose, t.reason, t.source_channels, t.target_channels,
                        t.channel_note) for i, (t, _) in enumerate(per_track)]
            per_track_filter = [enabled for _, enabled in per_track]
        else:
            audio_plan = []
            per_track_filter = []
        audio_policy_preview = list(audio_plan)
    else:
        per_track_filter = None

    if cutting and any(x.copy for x in audio_plan):
        raise RecodeError('Cutlist cannot safely preserve copied original audio; refusing to silently remove protected tracks')
    audio_bitrates = audio_config.get('CodecBitratesKbps', {})
    if not isinstance(audio_bitrates, dict) or any(
            not isinstance(v, int) or isinstance(v, bool) or v < 64 or v > 1536
            for v in audio_bitrates.values()):
        raise RecodeError('Recode.Audio.CodecBitratesKbps contains invalid bitrates')
    preserve_source_bitrate = audio_config.get('PreserveSourceBitrate', True)
    if not isinstance(preserve_source_bitrate, bool):
        raise RecodeError('Recode.Audio.PreserveSourceBitrate must be boolean')

    # Prefer the source's lossy bitrate. Lossless codecs do not have a
    # meaningful target bitrate for a lossy encoder.
    lossless_sources = {'flac', 'alac', 'truehd', 'mlp', 'wavpack', 'ape',
                        'pcm_s16le', 'pcm_s24le', 'pcm_s32le', 'pcm_f32le',
                        'pcm_f64le'}
    ac3_rates = (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192,
                 224, 256, 320, 384, 448, 512, 576, 640)
    source_audio_map = {int(x['index']): x for x in audios}
    # Test 103: bitrate fallback only when the source bitrate is missing.
    # Existing DTS policy and explicitly configured fixed-rate mode stay intact.
    unknown_bitrate_fallbacks = {
        'aac': {1: 96, 2: 192, 6: 384},
        'ac3': {1: 96, 2: 192, 6: 448},
        'eac3': {1: 96, 2: 192, 6: 448},
    }

    def unknown_source_fallback(track, configured):
        if track.codec == 'dts':
            return configured  # DTS uses its existing special bitrate policy.
        channels = track.target_channels or track.source_channels
        codec_defaults = unknown_bitrate_fallbacks.get(track.codec)
        if codec_defaults is None or channels is None:
            return configured
        # For other layouts retain the configured codec-specific default.
        return codec_defaults.get(channels, configured)


    def target_audio_rate(track):
        if track.copy or track.codec == 'flac':
            return None, 'copy/lossless'
        fallback = audio_bitrates.get(track.codec, bitrate)
        if not preserve_source_bitrate:
            return fallback, 'configured'
        source_stream = source_audio_map[track.source_index]
        source_codec = str(source_stream.get('codec_name') or '').lower()
        # DTS-HD MA / DTS:X and other lossless or hybrid formats must use
        # configured lossy defaults rather than their often huge bitrates.
        profile = str(source_stream.get('profile') or '').lower()
        if (source_codec in lossless_sources or 'lossless' in profile or
                (source_codec == 'dts' and ('ma' in profile or 'hd' in profile))):
            return fallback, 'lossless/hybrid source -> configured'
        try:
            bits = int(source_stream.get('bit_rate') or 0)
        except (ValueError, TypeError):
            bits = 0
        if bits <= 0:
            selected = unknown_source_fallback(track, fallback)
            return selected, ('unknown source bitrate -> channel fallback'
                              if selected != fallback else 'unknown source bitrate -> configured')
        requested = round(bits / 1000)
        if track.codec == 'ac3':
            # AC-3 accepts a discrete set of bitrates; pick the nearest.
            chosen = min(ac3_rates, key=lambda n: (abs(n-requested), n))
        elif track.codec == 'dts':
            # FFmpeg native DTS encoder supports only defined core rates.
            supported = (192, 224, 256, 320, 384, 448, 512, 576, 640, 768, 960, 1024, 1152, 1344, 1408, 1411, 1472, 1536)
            chosen = min(supported, key=lambda n: (abs(n-requested), n))
        elif track.codec in ('aac', 'eac3', 'mp3', 'opus'):
            # Conservative encoder range; out-of-range values use the
            # already configured per-codec fallback.
            chosen = requested if 64 <= requested <= 1536 else fallback
        else:
            chosen = fallback
        return chosen, ('source' if chosen == requested else
                        'nearest supported' if chosen != fallback else 'configured/adjusted')

    def audio_output_args(plan):
        result_args = []
        for i, track in enumerate(plan):
            codec_name = 'copy' if track.copy else ('dca' if track.codec == 'dts' else track.codec)
            result_args += [f'-c:a:{i}', codec_name]
            if not track.copy:
                result_args += [f'-map_metadata:s:a:{i}', '-1']
                if track.codec == 'dts':
                    result_args += [f'-strict:a:{i}', '-2']
                if track.codec != 'flac':
                    rate, _rate_reason = target_audio_rate(track)
                    result_args += [f'-b:a:{i}', f'{rate}k']
                if audio_filter and (per_track_filter is None or per_track_filter[i]):
                    result_args += [f'-filter:a:{i}', audio_filter]
                if track.target_channels is not None and track.source_channels is not None and track.target_channels < track.source_channels:
                    layout = str(source_audio_map[track.source_index].get('channel_layout') or source_audio_map[track.source_index].get('ch_layout') or '')
                    if not layout or layout.lower() in ('unknown', 'unspecified'):
                        raise RecodeError(f'Unsafe downmix: unknown channel layout on stream {track.source_index}')
                    if track.target_channels != 6:
                        raise RecodeError(f'Unsupported automatic downmix to {track.target_channels} channels')
                    result_args += [f'-ac:a:{i}', '6', f'-channel_layout:a:{i}', '5.1(side)']
            result_args += [f'-disposition:a:{i}', 'default' if track.default else '0']
            tags = source_audio_map[track.source_index].get('tags') or {}
            language = tags.get('language')
            if language and language not in ('und', '?'):
                result_args += [f'-metadata:s:a:{i}', f'language={language}']
            title = tags.get('title', '')
            if track.purpose == 'everyday': title = 'Alltag (bearbeitet)'
            elif track.purpose == 'original' and len(plan) > 1: title = 'Original (unverändert)'
            if title: result_args += [f'-metadata:s:a:{i}', f'title={title}']
        return result_args if plan else ['-an']

    def expected_audio_codecs(plan):
        return [source_audio_map[x.source_index].get('codec_name') if x.copy else x.codec for x in plan]

    def audio_verified(output_info, plan):
        output_tracks = [x for x in output_info['streams'] if x.get('codec_type') == 'audio']
        if len(output_tracks) != len(plan):
            raise RecodeError('Audio output stream count differs from audio policy')
        for index, (actual, desired) in enumerate(zip(output_tracks, plan)):
            if desired.target_channels is not None and actual.get('channels') != desired.target_channels:
                raise RecodeError(f'Audio #{index}: wrong channel count')
            if bool((actual.get('disposition') or {}).get('default')) != desired.default:
                raise RecodeError(f'Audio #{index}: wrong default disposition')

    selected_subtitles = selection['subtitle_indexes']
    if audios and not selected_audio:
        raise RecodeError('Audio streams exist but none selected; refusing accidental audio loss')
    if not audios:
        audio_filter = None
        audio_mode = 'none'
        mode_notes.append('Input has no audio streams; video-only output preserved')
    selected_sub_streams = [s for s in subtitles if s['index'] in selected_subtitles]
    embedded_subs, external_subs, subtitle_codec = _subtitle_plan(selected_sub_streams, '.mp4' if cutting else suffix)
    additional_subs = (_filter_external_subtitles(
        _find_external_subtitles(source), config, selected_sub_streams,
        selection['primary_language']) if mux_external and suffix == '.mkv' else [])
    if additional_subs and cutting:
        raise RecodeError('External subtitle muxing with cutlists is not supported (timestamps would be wrong)')
    if mux_external and suffix != '.mkv':
        mode_notes.append('External subtitle muxing requires MKV; option skipped')
    if cutting:
        # Subtitle timestamps are not valid after a discontinuous cut. Export originals
        # as diagnostic sidecars only; never embed an unsynchronized subtitle stream.
        embedded_subs = []
        external_subs = []
        for sub in selected_sub_streams:
            _, exports, _ = _subtitle_plan([sub], '.ts')
            if not exports:
                # E.g. DVB subtitles need special handling; refuse silent loss.
                raise RecodeError(f"Cannot safely export cutlist subtitle stream {sub['index']}")
            external_subs.extend(exports)
    audio_codec, audio_bitrate, adjustments = _audio_for_container(suffix, bitrate)
    adjustments.extend(mode_notes)
    if audio_mode == 'copy':
        selected_audio_streams = [s for s in audios if s['index'] in selected_audio]
        audio_codec = 'copy'
        audio_filter = None
        # Copy is only safe when the muxer accepts the selected codecs.
        # Let FFmpeg reject incompatible combinations; never silently re-encode.
        adjustments.append('Audio stream copy requested; container compatibility checked by FFmpeg')
    if video_mode == 'copy':
        adjustments.append('Video stream copy requested; no video filtering or encoder timing options')
    if dv_profile == 20:
        adjustments.append('Dolby Vision profile 20: MP4 muxer -strict unofficial')
    if cutlist_path and dynamic_hdr:
        adjustments.append('WARNING: Dolby Vision/HDR10+ cutlist ignored; full source processed')
    adjustments.append('HDR detection: '+hdr_info['format'] + (' + Dolby Vision' if hdr_info['dolby_vision'] else '') + (' + HDR10+' if hdr_info['hdr10plus'] else ''))
    # MPEG-TS supports neither Matroska-style chapter storage nor all metadata.
    chapter_args = [] if suffix == '.ts' else ['-map_chapters', '0']
    if suffix == '.ts':
        adjustments.append('MPEG-TS: chapter mapping omitted')
    if additional_subs:
        adjustments.append(f'{len(additional_subs)} external subtitle(s) muxed into MKV (originals retained)')
        adjustments.append('Container duration check skipped: external subtitle timestamps may exceed video duration; stream counts and codecs still verified')
    if external_subs:
        adjustments.append(f'{len(external_subs)} incompatible subtitle stream(s) exported externally')
    if suffix == '.mp4' and embedded_subs:
        adjustments.append('MP4: compatible text subtitles converted to mov_text')
    # Never silently discard selected subtitles; sidecars are verified before publication.
    def build_args(chosen_encoder: str) -> list[str]:
        encode_options, quality_mode = (['-c:v', 'copy'], 'copy') if video_mode == 'copy' else _quality_encoder_args(chosen_encoder, quality_level, source_bit_depth, hdr=hdr_info['format'] in ('HLG','HDR10'))[:2]
        # Preserve color signaling for HLG and HDR10 (and DV P8.1 HDR10 base).
        color_args=[]
        if video_mode != 'copy' and hdr_info['format'] in ('HLG','HDR10'):
            color_args=['-pix_fmt',('p010le' if chosen_encoder.endswith(('_qsv','_nvenc','_amf')) else 'yuv420p10le'),'-color_primaries',video.get('color_primaries') or 'bt2020',
                        '-color_trc',video.get('color_transfer') or 'arib-std-b67',
                        '-colorspace',video.get('color_space') or 'bt2020nc']
        return ['-i', str(source), *[item for sub in additional_subs for item in ('-i', str(sub['path']))], '-map', '0:V?',
                *[part for track in audio_plan for part in ('-map', f'0:{track.source_index}')],
                *[part for index in embedded_subs for part in ('-map', f'0:{index}')],
                *[part for i in range(len(additional_subs)) for part in ('-map', f'{i+1}:0')],
                '-map_metadata', '0', *chapter_args,
                *encode_options, *color_args,
                *(['-vf', active_video_filter] if active_video_filter else []),
                *(encoder_timing_args(source_timing) if video_mode != 'copy' and not (deinterlace['applied'] or half_4k['applied']) else ['-fps_mode:v:0', 'passthrough'] if (deinterlace['applied'] or half_4k['applied']) else []),
                *audio_output_args(audio_plan),
                '-c:s', subtitle_codec,
                *[arg for i, sub in enumerate(additional_subs) for arg in
                  (f'-metadata:s:s:{len(embedded_subs)+i}', f"language={sub['language']}",
                   f'-disposition:s:{len(embedded_subs)+i}', 'forced' if sub['forced'] else 'default' if sub['default'] else '0')],
                *(['-strict', 'unofficial'] if dv_profile == 20 else []),
                '-f', CONTAINERS[suffix]]
    def emit_plan(line):
        print(line, flush=True)
        if on_plan is not None:
            on_plan(line)

    emit_plan(f"[PLAN] Video: {video_mode.upper()} | {video.get('codec_name', '?')} | "
          f"{width}x{video.get('height', '?')} | {hdr_info['format']} | "
          f"Container: {suffix}")
    for i, track in enumerate(audio_plan, 1):
        stream = source_audio_map[track.source_index]
        source_rate = stream.get('bit_rate')
        source_kbps = (f'{int(source_rate)/1000:.0f} kb/s' if source_rate and
                       str(source_rate).isdigit() else 'unknown')
        target_rate, rate_reason = target_audio_rate(track)
        target_text = 'COPY' if track.copy else (f'{track.codec.upper()} {target_rate} kb/s' if target_rate else track.codec.upper())
        emit_plan(f"[PLAN] Audio #{i}: {stream.get('codec_name', '?')} "
              f"{stream.get('channels', '?')}ch {source_kbps} -> {target_text} "
              f"({rate_reason}); default={track.default}")
    emit_plan(f'[PLAN] Audio filter: {audio_filter or "none"}')
    emit_plan(f'[PLAN] Streamcopy video: {video_mode == "copy"}; cutlist: {cutting}')
    args = build_args(encoder)
    temp = destination.with_name(f'.{destination.stem}.recode-{uuid.uuid4().hex}.part{suffix}')
    args.append(str(temp))
    result = {'input': str(source), 'output': str(destination), 'requested_output': str(requested_destination), 'dolby_vision_profile': dv_profile, 'resolution': f"{width}x{video.get('height','?')}",
              'profile': profile, 'quality': quality_level, 'quality_label': LABELS[quality_level-1], 'quality_mode': ('copy' if video_mode == 'copy' else _quality_encoder_args(encoder,quality_level,source_bit_depth,hdr=hdr_info['format'] in ('HLG','HDR10'))[1]), 'quality_parameter': (None if video_mode == 'copy' else _quality_encoder_args(encoder,quality_level,source_bit_depth)[2]), 'video_codec': (video.get('codec_name') if video_mode == 'copy' else codec), 'encoder_backend': selected_backend, 'encoder_selection_notes': encoder_notes,
              'video_encoder': encoder, 'requested_video_encoder': encoder,
              'audio_encoder': ([x.codec for x in audio_plan] if audio_plan else None), 'audio_bitrate_kbps': ([target_audio_rate(x)[0] for x in audio_plan] if audio_plan else None),
              'video_mode': video_mode, 'requested_video_mode': video_mode_requested, 'audio_mode': audio_mode, 'requested_audio_mode': audio_mode_requested,
              'container': CONTAINERS[suffix], 'container_adjustments': adjustments,
              'external_subtitles': [],
              'audio_filter': audio_filter, 'audio_track_rules': [dict(x) for x in mission.audio_tracks] if mission and mission.audio_tracks else None, 'duration_seconds': duration,
              'cutlist': {'path': str(cutlist_path), 'segments': segments, 'expected_duration': output_duration, 'subtitle_policy': 'original external backup; cut subtitle not yet supported'} if cutting else None,
              'hdr': {'format':hdr_info['format'],'dolby_vision':hdr_info['dolby_vision'],'hdr10plus':hdr_info['hdr10plus']},
              'input_video_codec': video.get('codec_name'),
              'input_audio_codecs': [s.get('codec_name') for s in audios],
              'optimizer': selection, 'audio_policy_preview': describe_audio_plan(audio_policy_preview), 'audio_plan': describe_audio_plan(audio_plan), 'source_frame_timing': source_timing,
              'full_frame_analysis': bool(exhaustive_analysis and not cutting),
              'deinterlace': dict(deinterlace), 'half_4k_framerate': dict(half_4k),
              'muxed_external_subtitles': [str(x['path']) for x in additional_subs],
              'command': [str(tools.ffmpeg), '-nostdin', '-hide_banner', '-nostats', '-progress', 'pipe:1', *args]}
    result['dolby_vision_conversion'] = ({'from_profile': 7, 'to_profile': 8, 'compatibility_id': 1, 'enhancement_layer': 'discarded', 'explicitly_permitted': True} if p7_conversion else None)
    result['software_fallback_enabled'] = fallback_enabled
    result['dolby_vision_streamcopy_fallback'] = bool(dv_fallback_reason)
    result['dolby_vision_fallback_reason'] = dv_fallback_reason
    if dry_run:
        if cutting:
            result['command_note'] = 'Cutlist execution uses per-segment seek/encode and concat copy; see segment_commands after execution.'
        return result
    # Audio-only retry is deliberately separate from the video hardware fallback.
    # Never retry on input, disk or muxer errors.
    from audio_engine import encoder_failure
    from types import SimpleNamespace
    def execute_with_audio_rescue(chosen_encoder, progress_callback):
        nonlocal audio_plan, args
        rescue_codecs = ([audio_config.get('FallbackCodec', 'eac3')] if force_audio_recode else [audio_config.get('FallbackCodec', 'eac3'), 'ac3', 'aac'])
        if not cutting and not force_audio_recode:
            rescue_codecs.append('copy')
        attempted = {x.codec for x in audio_plan if not x.copy}
        current = chosen_encoder
        rescue_index = 0
        while True:
            temp.unlink(missing_ok=True)
            try:
                if cutting:
                    _run_segmented_cut(
                        tools, source, destination, segments, selected_audio,
                        current, quality_level, source_bit_depth,
                        hdr_info['format'] in ('HLG', 'HDR10'),
                        audio_plan, audio_output_args, CONTAINERS[suffix],
                        on_progress, result, temp, active_video_filter)
                else:
                    args = build_args(current) + [str(temp)]
                    result['command'] = [str(tools.ffmpeg), '-nostdin', '-hide_banner',
                                         '-nostats', '-progress', 'pipe:1', *args]
                    run_ffmpeg(tools, args, on_progress=progress_callback)
            except FFmpegError as error:
                message = str(error).lower()
                if not encoder_failure(SimpleNamespace(stderr=message)) or not any(
                    token in message for token in ('aost#', 'audio', 'dca', 'ac3', 'eac3', 'aac', 'opus', 'flac', 'mp3')):
                    raise
                failure = error
            else:
                break
            next_codec = None
            while rescue_index < len(rescue_codecs):
                candidate = rescue_codecs[rescue_index]
                rescue_index += 1
                if candidate not in attempted:
                    next_codec = candidate
                    break
            if next_codec is None:
                raise failure
            attempted.add(next_codec)
            print(f'[AUDIO RESCUE] Encoder failed; retrying with {next_codec.upper()}', flush=True)
            next_plan = rescue_plan(audio_plan, next_codec, source_audio_map)
            if not next_plan:
                raise failure
            audio_plan = next_plan
            result['audio_plan'] = describe_audio_plan(audio_plan)
            result['audio_encoder'] = [x.codec for x in audio_plan]
            result['audio_bitrate_kbps'] = [target_audio_rate(x)[0] for x in audio_plan]
            result.setdefault('audio_rescue_attempts', []).append(next_codec)
        return

    sidecars: list[tuple[Path, Path, dict]] = []
    try:
        tracker = ProgressTracker(output_duration)
        def report(values):
            data = dict(values)
            data['duration_seconds'] = output_duration
            data.update(tracker.update(ffmpeg_position(values)))
            data['phase'] = 'encoding'
            if on_progress:
                on_progress(data)
        start = time.monotonic()
        try:
            execute_with_audio_rescue(encoder, report)
            if on_progress:
                on_progress({**tracker.update(tracker.last_position), 'phase': 'verifying',
                             'duration_seconds': output_duration})
            if dynamic_hdr and video_mode == 'recode':
                try:
                    result['hdr_verification'] = restore_hdr(temp, hdr_info, tools.ffmpeg, tools.ffprobe,
                                                             Path(__file__).resolve().parent, _hdr_work)
                except HDRPipelineError as hdr_error:
                    if not p7_conversion:
                        raise
                    print(f'[HDR WARNING] P7 -> P8.1 verification failed: {hdr_error}', file=sys.stderr, flush=True)
                    print('[HDR WARNING] Rebuilding output with original P7 video streamcopy.', file=sys.stderr, flush=True)
                    temp.unlink(missing_ok=True)
                    video_mode = 'copy'
                    p7_conversion = False
                    dv_fallback_reason = str(hdr_error)
                    encoder = 'copy'
                    hdr_info = {'format': 'HDR10' if transfer == 'smpte2084' else 'Dolby Vision',
                                'dolby_vision': True, 'hdr10plus': False}
                    fallback_args = build_args('copy') + [str(temp)]
                    run_ffmpeg(tools, fallback_args, on_progress=report)
                    result.update(video_mode='copy',video_encoder='copy',encoder_backend='copy',
                                  quality_mode='copy',video_codec=video.get('codec_name'),
                                  dolby_vision_conversion=None, dolby_vision_streamcopy_fallback=True,
                                  dolby_vision_fallback_reason=dv_fallback_reason,
                                  command=[str(tools.ffmpeg), '-nostdin','-hide_banner','-nostats',
                                           '-progress','pipe:1',*fallback_args])
                    result['container_adjustments'].append('WARNING: P7 -> P8.1 restore failed; original P7 streamcopy fallback')
            output_info = probe_json(tools, temp)
            if deinterlace['applied']:
                output_video = next(s for s in output_info['streams'] if s.get('codec_type') == 'video')
                out_order = str(output_video.get('field_order') or 'unknown').lower()
                if out_order in ('tt', 'bb', 'tb', 'bt'):
                    raise RecodeError(f'Deinterlaced output still marked interlaced: {out_order}')
                result['deinterlace']['output_field_order'] = out_order
            if video_mode == 'copy':
                result['video_copy_verification'] = _verify_copied_dv_metadata(video, output_info)
            _check_output(metadata, output_info, video_count, len(audio_plan), len(embedded_subs)+len(additional_subs), output_duration,
                          video.get('codec_name') if video_mode == 'copy' else CODEC_NAMES[codec],
                          expected_audio_codecs(audio_plan), cut_mode=cutting,
                          external_subtitles_added=bool(additional_subs))
            audio_verified(output_info, audio_plan)
            output_timing = (_metadata_timing(next(s for s in output_info['streams'] if s.get('codec_type')=='video')) if (cutting or not exhaustive_analysis) else inspect_timing(tools, temp, next(s for s in output_info['streams'] if s.get('codec_type')=='video')))
            if not cutting and exhaustive_analysis and not (deinterlace['applied'] or half_4k['applied']):
                verify_timing(source_timing, output_timing)
            result['output_frame_timing'] = output_timing
        except FFmpegError as hardware_error:
            if video_mode == 'copy' or encoder == software_encoder or not fallback_enabled:
                raise
            # Never treat a failed hardware encode as successful. Restart cleanly.
            print(f'Hardware encoder failed: {hardware_error}; retrying with {software_encoder} (same-codec software fallback).',
                  file=sys.stderr, flush=True)
            temp.unlink(missing_ok=True)
            tracker = ProgressTracker(output_duration)  # fallback restarts encoding from zero
            fallback_args = build_args(software_encoder) + [str(temp)]
            result['fallback_reason'] = str(hardware_error)
            result['video_encoder'] = software_encoder
            result['quality_mode'] = _quality_encoder_args(software_encoder,quality_level,source_bit_depth,hdr=hdr_info['format'] in ('HLG','HDR10'))[1]
            result['quality_parameter'] = _quality_encoder_args(software_encoder,quality_level,source_bit_depth)[2]
            result['command'] = [str(tools.ffmpeg), '-nostdin', '-hide_banner', '-nostats',
                                 '-progress', 'pipe:1', *fallback_args]
            execute_with_audio_rescue(software_encoder, report)
            if on_progress:
                on_progress({**tracker.update(tracker.last_position), 'phase': 'verifying',
                             'duration_seconds': output_duration})
            if dynamic_hdr and video_mode == 'recode':
                try:
                    result['hdr_verification'] = restore_hdr(temp, hdr_info, tools.ffmpeg, tools.ffprobe,
                                                             Path(__file__).resolve().parent, _hdr_work)
                except HDRPipelineError as hdr_error:
                    if not p7_conversion:
                        raise
                    print(f'[HDR WARNING] P7 -> P8.1 verification failed: {hdr_error}', file=sys.stderr, flush=True)
                    print('[HDR WARNING] Rebuilding output with original P7 video streamcopy.', file=sys.stderr, flush=True)
                    temp.unlink(missing_ok=True)
                    video_mode = 'copy'
                    p7_conversion = False
                    dv_fallback_reason = str(hdr_error)
                    encoder = 'copy'
                    hdr_info = {'format': 'HDR10' if transfer == 'smpte2084' else 'Dolby Vision',
                                'dolby_vision': True, 'hdr10plus': False}
                    fallback_args = build_args('copy') + [str(temp)]
                    run_ffmpeg(tools, fallback_args, on_progress=report)
                    result.update(video_mode='copy',video_encoder='copy',encoder_backend='copy',
                                  quality_mode='copy',video_codec=video.get('codec_name'),
                                  dolby_vision_conversion=None, dolby_vision_streamcopy_fallback=True,
                                  dolby_vision_fallback_reason=dv_fallback_reason,
                                  command=[str(tools.ffmpeg), '-nostdin','-hide_banner','-nostats',
                                           '-progress','pipe:1',*fallback_args])
                    result['container_adjustments'].append('WARNING: P7 -> P8.1 restore failed; original P7 streamcopy fallback')
            output_info = probe_json(tools, temp)
            if deinterlace['applied']:
                output_video = next(s for s in output_info['streams'] if s.get('codec_type') == 'video')
                out_order = str(output_video.get('field_order') or 'unknown').lower()
                if out_order in ('tt', 'bb', 'tb', 'bt'):
                    raise RecodeError(f'Deinterlaced output still marked interlaced: {out_order}')
                result['deinterlace']['output_field_order'] = out_order
            if video_mode == 'copy':
                result['video_copy_verification'] = _verify_copied_dv_metadata(video, output_info)
            _check_output(metadata, output_info, video_count, len(audio_plan), len(embedded_subs)+len(additional_subs), output_duration,
                          video.get('codec_name') if video_mode == 'copy' else CODEC_NAMES[codec],
                          expected_audio_codecs(audio_plan), cut_mode=cutting,
                          external_subtitles_added=bool(additional_subs))
            audio_verified(output_info, audio_plan)
            output_timing = (_metadata_timing(next(s for s in output_info['streams'] if s.get('codec_type')=='video')) if (cutting or not exhaustive_analysis) else inspect_timing(tools, temp, next(s for s in output_info['streams'] if s.get('codec_type')=='video')))
            if not cutting and exhaustive_analysis and not (deinterlace['applied'] or half_4k['applied']):
                verify_timing(source_timing, output_timing)
            result['output_frame_timing'] = output_timing
        # Extract selected unsupported subtitle streams without transcoding.
        # Use temporary sidecars; do not publish the video until all are verified.
        for sub in external_subs:
            qualifier = '.forced' if sub['forced'] else ''
            filename = f"{destination.stem}.{sub['language']}{qualifier}.s{sub['index']}{'.original' if cutting else ''}{sub['extension']}"
            target = destination.with_name(filename)
            if target.is_symlink() or (target.exists() and os.path.samefile(source, target)):
                raise RecodeError(f'External subtitle destination is unsafe: {target}')
            staged = destination.with_name(f'.{filename}.{uuid.uuid4().hex}.part{sub["extension"]}')
            sidecars.append((staged, target, sub))
            run_tool(tools.ffmpeg, ['-nostdin', '-v', 'error', '-i', str(source),
                                    '-map', f'0:{sub["index"]}', '-c:s', 'copy',
                                    '-f', sub['muxer'], str(staged)])
            if not staged.is_file() or staged.stat().st_size == 0:
                raise RecodeError(f'External subtitle extraction failed: {filename}')
        result['external_subtitles'] = [str(target) for _, target, _ in sidecars]
        if cutting and external_subs:
            result['subtitle_warning'] = 'Original subtitles backed up; cut/retimed subtitle attempts are not implemented. Do not auto-load these backups.'
        result['elapsed_seconds'] = round(time.monotonic() - start, 2)
        result['average_speed'] = round(output_duration / result['elapsed_seconds'], 2) if output_duration and result['elapsed_seconds'] else None
        result['hardware_encoding_used'] = video_mode != 'copy' and result['video_encoder'] != software_encoder
        output_existed_before_publish = destination.exists()
        # Serialize publication of the same destination. The lock is acquired only
        # for the short commit phase; existing output remains intact during encoding.
        lock_path = destination.with_name(f'.{destination.name}.recode-publish.lock')
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise RecodeError(f'Another publication is in progress: {destination}') from exc
        os.close(lock_fd)
        backups: list[tuple[Path, Path]] = []
        published_sidecars: list[Path] = []
        try:
            if destination.is_symlink() or (destination.exists() and os.path.samefile(source, destination)):
                raise RecodeError('Output destination changed to an unsafe path')
            # Stage rollback copies of existing sidecars, without touching originals.
            for staged, target, _ in sidecars:
                if target.is_symlink() or (target.exists() and os.path.samefile(source, target)):
                    raise RecodeError(f'Unsafe subtitle destination: {target}')
                if target.exists():
                    backup = target.with_name(f'.{target.name}.{uuid.uuid4().hex}.rollback')
                    os.link(target, backup)
                    backups.append((backup, target))
            # The video is committed last. A failed sidecar publication rolls back
            # without replacing the existing video.
            try:
                for staged, target, _ in sidecars:
                    os.replace(staged, target)
                    published_sidecars.append(target)
                os.replace(temp, destination)
            except OSError:
                for target in reversed(published_sidecars):
                    backup = next((b for b, t in backups if t == target), None)
                    if backup is not None:
                        os.replace(backup, target)
                    else:
                        target.unlink(missing_ok=True)
                raise
            for backup, _ in backups:
                backup.unlink(missing_ok=True)
        finally:
            for backup, _ in backups:
                backup.unlink(missing_ok=True)
            lock_path.unlink(missing_ok=True)
        result['replaced_existing_output'] = output_existed_before_publish
        result['verified'] = True
        result['output_size_bytes'] = destination.stat().st_size
        if on_progress:
            on_progress({**tracker.update(output_duration, complete=True), 'phase': 'complete',
                         'duration_seconds': output_duration})
        return result
    finally:
        # Failed/partial results must never appear as completed videos.
        temp.unlink(missing_ok=True)
        for staged, _, _ in sidecars:
            staged.unlink(missing_ok=True)



def recode_file(source: str | Path, destination: str | Path, *,
                config_path: str | Path | None = None, on_progress=None,
                dry_run: bool = False, encoder_override: str | None = None,
                allow_fallback: bool | None = None,
                full_frame_analysis: bool | None = None,
                mission: RecodeMission | dict | str | Path | None = None,
                on_plan=None) -> dict:
    """Run one file. Mission overrides only per-file intent, never technical safety.

    Legacy calls without mission remain supported during controller migration.
    """
    if mission is not None:
        if isinstance(mission, (str, Path)):
            mission = RecodeMission.from_json(mission)
        elif isinstance(mission, dict):
            mission = RecodeMission.from_dict(mission)
        elif not isinstance(mission, RecodeMission):
            raise MissionError('mission must be RecodeMission, dict or JSON path')
        if Path(source).expanduser().resolve() != mission.source or Path(destination).expanduser().resolve() != mission.destination:
            raise MissionError('Mission source/destination differ from recode_file arguments')
    with tempfile.TemporaryDirectory(prefix='media-buddy-hdr84-', dir=str(Path(destination).expanduser().resolve().parent)) as directory:
        return _recode_file_impl(source, destination, config_path=config_path,
                                 on_progress=on_progress, dry_run=dry_run,
                                 encoder_override=encoder_override,
                                 allow_fallback=allow_fallback,
                                 full_frame_analysis=full_frame_analysis,
                                 _hdr_work=Path(directory), mission=mission,
                                 on_plan=on_plan)


def main() -> int:
    parser = argparse.ArgumentParser(description='Media-Buddy V2 Test 78 optimized segmented cutlists' )
    parser.add_argument('input', nargs='?', help='Source video (optional with --mission)')
    parser.add_argument('output', nargs='?', help='New output file (optional with --mission)')
    parser.add_argument('--mission', metavar='FILE', help='Load Source, Destination, VideoMode, AudioMode and optional QualityLevel from JSON')
    parser.add_argument('--full-frame-analysis', action='store_true', default=None, help='Enable slow exhaustive source/output frame timing diagnostics')
    parser.add_argument('--dry-run', action='store_true', help='Print planned FFmpeg command without encoding')
    parser.add_argument('--encoder', choices=sorted({name for group in CODECS.values() for name in group.values()}), help='Override configured video encoder (must match VideoCodec)')
    parser.add_argument('--no-fallback', action='store_true', help='Fail if QSV is unavailable (recommended for benchmark)')
    args = parser.parse_args()
    try:
        mission = RecodeMission.from_json(args.mission) if args.mission else None
    except MissionError as exc:
        parser.error(str(exc))
    if mission is None and (args.input is None or args.output is None):
        parser.error('input and output are required unless --mission is provided')
    source = args.input if args.input is not None else str(mission.source)
    destination = args.output if args.output is not None else str(mission.destination)
    # CLI output is time-throttled; FFmpeg progress callbacks continue without
    # percentage throttling so future GUI consumers can update independently.
    progress_interval_seconds = 1.0
    last_progress_print = None
    last_percent = None
    last_phase = None
    def progress(values):
        nonlocal last_progress_print, last_percent, last_phase
        percent = values.get('percent')
        phase = values.get('phase', 'encoding')
        if phase != last_phase:
            if phase == 'verifying':
                print('FFmpeg processing complete; verifying output...', flush=True)
            elif phase == 'complete':
                print('Output verified and published.', flush=True)
            last_phase = phase
        if phase == 'encoding' and percent is not None:
            now = time.monotonic()
            # A fallback encoder restarts at 0%; report that immediately.
            restarted = last_percent is not None and percent < last_percent - 10
            finished = values.get('progress') == 'end'
            due = last_progress_print is None or now - last_progress_print >= progress_interval_seconds
            if restarted or finished or due:
                speed = values.get('speed')
                elapsed = format_duration(values.get('elapsed_seconds'))
                remaining = format_duration(values.get('remaining_seconds'))
                print(f'Progress: {percent:.1f}% | elapsed: {elapsed} | remaining: {remaining} | speed: {speed or "--"}x', flush=True)
                last_progress_print = now
                last_percent = percent
    try:
        result = recode_file(source, destination, mission=mission, on_progress=progress, dry_run=args.dry_run,
                             encoder_override=args.encoder, allow_fallback=False if args.no_fallback else None,
                             full_frame_analysis=args.full_frame_analysis)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (FFmpegError, RecodeError, HDRPipelineError, FrameRateError, EncoderError, OSError, ValueError, KeyError) as exc:
        print(f'RECODE ERROR: {exc}', file=sys.stderr)
        return 1

if __name__ == '__main__':
    raise SystemExit(main())
