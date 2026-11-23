"""Media-Buddy V2: standalone audio-only FFmpeg transcoding core (Test 185).

No imports from the video recode pipeline. No source-file modifications.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable
import json
import logging
import math
import os
import subprocess
import threading
import time


@dataclass(frozen=True)
class CodecProfile:
    extension: str
    encoder: str
    mode: str
    levels: tuple
    cover: bool = False


# Five ascending user-facing levels. FLAC levels control compression, not fidelity.
PROFILES = {
    'mp3': CodecProfile('.mp3', 'libmp3lame', 'qscale', (7, 5, 3, 1, 0), True),
    'flac': CodecProfile('.flac', 'flac', 'compression', (0, 2, 5, 7, 8), True),
    'opus': CodecProfile('.opus', 'libopus', 'bitrate', (96, 128, 160, 192, 256)),
    'aac': CodecProfile('.m4a', 'aac', 'bitrate', (128, 160, 192, 256, 320), True),
    'vorbis': CodecProfile('.ogg', 'libvorbis', 'qscale', (2, 4, 5, 7, 9)),
    'wav16': CodecProfile('.wav', 'pcm_s16le', 'fixed', (None,) * 5),
    'wav24': CodecProfile('.wav', 'pcm_s24le', 'fixed', (None,) * 5),
}


LOGGER = logging.getLogger(__name__)


class MusicRecodeError(RuntimeError):
    pass


def _probe(ffprobe: str, source: Path) -> dict:
    result = subprocess.run([str(ffprobe), '-v', 'error', '-show_streams', '-show_format',
                             '-of', 'json', str(source)], capture_output=True, text=True,
                            encoding='utf-8', errors='replace', stdin=subprocess.DEVNULL)
    if result.returncode:
        raise MusicRecodeError('ffprobe: ' + result.stderr[-1200:])
    return json.loads(result.stdout)


def build_command(ffmpeg: str, source: Path, output: Path, *, codec: str,
                  quality: int = 4, gain_db: float = 0.0,
                  sample_rate: int | None = None, channels: int | None = None,
                  include_cover: bool = False, flac_bits: int | None = None,
                  start_seconds: float | None = None, duration_seconds: float | None = None,
                  track_metadata: dict[str, str] | None = None,
                  seek_mode: str = "output") -> list[str]:
    if codec not in PROFILES:
        raise ValueError(f'Unknown audio codec profile: {codec}')
    if quality not in range(1, 6):
        raise ValueError('Quality must be 1..5')
    if not math.isfinite(gain_db) or not -60 <= gain_db <= 30:
        raise ValueError('Invalid gain_db')
    profile = PROFILES[codec]
    if output.suffix.lower() != profile.extension:
        raise ValueError(f'Output extension must be {profile.extension}')
    if sample_rate is not None and not 8000 <= sample_rate <= 384000:
        raise ValueError('Invalid sample rate')
    if channels is not None and not 1 <= channels <= 8:
        raise ValueError('Invalid channel count')
    if seek_mode not in ('output', 'input', 'filter', 'reblock'):
        raise ValueError(f'Invalid seek mode: {seek_mode}')
    cmd = [str(ffmpeg), '-hide_banner', '-nostdin', '-n']
    if seek_mode == 'input' and start_seconds is not None:
        cmd += ['-ss', f'{start_seconds:.9f}']
    cmd += ['-i', str(source),
           '-map', '0:a:0', '-map_metadata', '0', '-map_chapters', '0',
           '-c:a', profile.encoder]
    if start_seconds is not None and seek_mode == 'output':
        cmd += ['-ss', f'{start_seconds:.9f}']
    if duration_seconds is not None and seek_mode not in ('filter', 'reblock'):
        cmd += ['-t', f'{duration_seconds:.9f}']
    if track_metadata:
        for key, value in track_metadata.items():
            cmd += ['-metadata', f'{key}={value}']
    setting = profile.levels[quality - 1]
    if profile.mode == 'qscale':
        cmd += ['-q:a', str(setting)]
    elif profile.mode == 'compression':
        cmd += ['-compression_level', str(setting)]
    elif profile.mode == 'bitrate':
        cmd += ['-b:a', f'{setting}k']
    if codec == 'flac':
        # Lossy formats (Opus, Vorbis, AAC, etc.) commonly report bit depth 0.
        # A missing/unsupported probe value is not a reason to refuse encoding.
        # Explicitly choose 16-bit PCM as the fallback; preserve known 24-bit.
        selected_bits = 24 if flac_bits == 24 else 16
        cmd += ['-sample_fmt', 's32' if selected_bits == 24 else 's16',
                '-bits_per_raw_sample', str(selected_bits)]
    if sample_rate is not None:
        cmd += ['-ar', str(sample_rate)]
    if channels is not None:
        cmd += ['-ac', str(channels)]
    filters = []
    if seek_mode in ('filter', 'reblock') and start_seconds is not None:
        trim = f'atrim=start={start_seconds:.9f}'
        if duration_seconds is not None:
            trim += f':end={start_seconds + duration_seconds:.9f}'
        filters += [trim, 'asetpts=PTS-STARTPTS']
    if seek_mode == 'reblock' and codec == 'flac':
        # Re-frame tiny end-of-segment buffers before the FLAC encoder.
        # Padding is at most 4095 samples (~93 ms at 44.1 kHz).
        filters.append('asetnsamples=n=4096:p=1')
    if gain_db:
        filters.append(f'volume={gain_db:.6f}dB')
    if filters:
        cmd += ['-af', ','.join(filters)]
    # Cover-art support is container-dependent and intentionally opt-in.
    if include_cover:
        if not profile.cover:
            raise ValueError(f'Embedded cover not supported for {codec} profile')
        cmd += ['-map', '0:v:0?', '-c:v', 'copy', '-disposition:v:0', 'attached_pic']
    cmd += ['-progress', 'pipe:1', '-nostats', str(output)]
    return cmd


def recode_file(source: str | Path, output: str | Path, *, codec: str,
                quality: int = 4, ffmpeg: str = 'ffmpeg', ffprobe: str = 'ffprobe',
                gain_db: float = 0.0, sample_rate: int | None = None,
                channels: int | None = None, include_cover: bool = False,
                stop_event: threading.Event | None = None,
                progress: Callable[[float], None] | None = None,
                start_seconds: float | None = None, duration_seconds: float | None = None,
                track_metadata: dict[str, str] | None = None,
                seek_mode: str = "output") -> dict:
    """Transcode one audio file; remove partial output on failure/cancel.

    Caller owns batch iteration, logging and album-level gain analysis.
    """
    src, dst = Path(source), Path(output)
    if not src.is_file():
        raise MusicRecodeError(f'Input missing: {src}')
    if src.resolve() == dst.resolve():
        raise MusicRecodeError('Input and output must be different')
    if dst.exists():
        raise MusicRecodeError(f'Output already exists: {dst}')
    # ffprobe is advisory. FFmpeg is the authority on whether input decodes.
    try:
        info = _probe(ffprobe, src)
    except (OSError, ValueError, MusicRecodeError) as exc:
        LOGGER.warning('Input ffprobe unavailable for %s; trying FFmpeg: %s', src, exc)
        info = {}
    streams = info.get('streams', [])
    audio = next((s for s in streams if s.get('codec_type') == 'audio'), {})
    flac_bits = None
    if codec == 'flac':
        raw = audio.get('bits_per_raw_sample') or audio.get('bits_per_sample')
        try:
            detected = int(raw)
        except (TypeError, ValueError):
            detected = None
        if detected in (16, 24):
            flac_bits = detected
        else:
            LOGGER.warning('Unknown/unsupported FLAC input bit depth %r for %s; '
                           'trying FFmpeg with 16-bit PCM fallback', raw, src)
            flac_bits = 16
    try:
        duration = float((info.get('format') or {}).get('duration') or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if start_seconds is not None and start_seconds < 0:
        raise ValueError("Negative segment start")
    if duration_seconds is not None and duration_seconds <= 0:
        raise ValueError("Invalid segment duration")
    if duration_seconds is not None:
        duration = duration_seconds
    command = build_command(ffmpeg, src, dst, codec=codec, quality=quality,
                            gain_db=gain_db, sample_rate=sample_rate,
                            channels=channels, include_cover=include_cover, flac_bits=flac_bits,
                            start_seconds=start_seconds, duration_seconds=duration_seconds,
                            track_metadata=track_metadata, seek_mode=seek_mode)
    dst.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    process = None
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding='utf-8', errors='replace')
        # Drain stderr concurrently to avoid pipe deadlocks on verbose FFmpeg output.
        stderr_lines = []
        def drain_stderr():
            for line in process.stderr:
                stderr_lines.append(line)
                if len(stderr_lines) > 200:
                    del stderr_lines[:100]
        reader = threading.Thread(target=drain_stderr, daemon=True)
        reader.start()
        for line in process.stdout:
            if stop_event is not None and stop_event.is_set():
                process.terminate()
                break
            if line.startswith('out_time_us=') or line.startswith('out_time_ms='):
                try:
                    seconds = int(line.partition('=')[2]) / 1_000_000
                    if duration > 0 and progress:
                        progress(min(100., max(0., seconds / duration * 100)))
                except ValueError:
                    pass
        if stop_event is not None and stop_event.is_set() and process.poll() is None:
            process.terminate()
        try:
            returncode = process.wait(timeout=8 if stop_event and stop_event.is_set() else None)
        except subprocess.TimeoutExpired:
            process.kill()
            returncode = process.wait()
        reader.join(timeout=3)
        if stop_event is not None and stop_event.is_set():
            raise MusicRecodeError('Cancelled')
        if returncode:
            raise MusicRecodeError(f'FFmpeg exited {returncode}: {"".join(stderr_lines)[-2000:]}')
        if not dst.is_file() or not dst.stat().st_size:
            raise MusicRecodeError('FFmpeg produced no output')
        # Post-encode ffprobe fields are diagnostic, not a reason to delete
        # a successfully encoded file. Some codecs omit bit depth entirely.
        try:
            output_info = _probe(ffprobe, dst)
            output_audio = next((s for s in output_info.get('streams', [])
                                 if s.get('codec_type') == 'audio'), None)
            if not output_audio:
                LOGGER.warning('Output ffprobe did not report audio for %s', dst)
            else:
                for field in ('sample_rate', 'channels'):
                    requested = sample_rate if field == 'sample_rate' else channels
                    source_value = audio.get(field)
                    expected = requested if requested is not None else source_value
                    actual = output_audio.get(field)
                    if expected is not None and actual is not None and str(actual) != str(expected):
                        LOGGER.warning('Output %s differs from input/request: %s -> %s (%s)',
                                       field, expected, actual, dst)
                if codec == 'flac':
                    actual_bits = output_audio.get('bits_per_raw_sample') or output_audio.get('bits_per_sample')
                    if actual_bits is not None and str(actual_bits) != str(flac_bits):
                        LOGGER.warning('Output FLAC bit depth differs: %s -> %s (%s)',
                                       flac_bits, actual_bits, dst)
        except (OSError, ValueError, MusicRecodeError) as exc:
            LOGGER.warning('Output ffprobe unavailable for %s; keeping FFmpeg output: %s', dst, exc)
        if progress:
            progress(100.)
        return {'input': str(src), 'output': str(dst), 'codec': codec,
                'quality': quality, 'bytes': dst.stat().st_size,
                'seconds': round(time.monotonic() - start, 3)}
    except BaseException:
        if process and process.poll() is None:
            process.kill()
            process.wait()
        if dst.exists():
            dst.unlink()
        raise
