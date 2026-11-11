"""Cross-platform FFmpeg/FFprobe discovery and subprocess execution.

No download, installation, shell invocation, or GUI dependency.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Callable, Sequence


class FFmpegError(RuntimeError):
    """FFmpeg configuration, discovery or execution failed."""


@dataclass(frozen=True)
class FFmpegTools:
    ffmpeg: Path
    ffprobe: Path
    ffmpeg_version: str
    ffprobe_version: str
    source: str


def _candidate_pair(configured: str | os.PathLike[str]) -> tuple[Path, Path]:
    raw = Path(configured).expanduser()
    folder = raw if raw.is_dir() else raw.parent
    exe = '.exe' if os.name == 'nt' else ''
    if raw.is_file() and raw.stem.lower() == 'ffmpeg':
        ffmpeg = raw
    elif raw.is_file() and raw.stem.lower() == 'ffprobe':
        ffmpeg = folder / ('ffmpeg' + exe)
    else:
        ffmpeg = folder / ('ffmpeg' + exe)
    return ffmpeg, folder / ('ffprobe' + exe)


def _version(executable: Path, timeout: float) -> str:
    try:
        result = subprocess.run([str(executable), '-version'], capture_output=True,
                                text=True, encoding='utf-8', errors='replace',
                                timeout=timeout, check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FFmpegError(f'Cannot run {executable}: {exc}') from exc
    if result.returncode != 0:
        raise FFmpegError(f'{executable} -version failed: {result.stderr.strip()}')
    first = result.stdout.splitlines()
    if not first:
        raise FFmpegError(f'{executable} returned no version information')
    expected = 'ffprobe' if 'ffprobe' in executable.stem.lower() else 'ffmpeg'
    if not first[0].lower().startswith(expected + ' version '):
        raise FFmpegError(f'Unexpected version output from {executable}: {first[0]}')
    return first[0]


def discover(configured_path: str | os.PathLike[str] | None = None,
             *, timeout: float = 8) -> FFmpegTools:
    """Prefer configured FFmpeg directory/executable, fall back to PATH.

    FFmpeg and FFprobe must be a usable pair from the same directory.
    """
    candidates: list[tuple[str, Path, Path]] = []
    if configured_path and str(configured_path).strip():
        a, b = _candidate_pair(configured_path)
        candidates.append(('configured', a, b))
    # Central tool resolution: configured -> project Tools/ -> PATH.
    # Validate ffmpeg and ffprobe as a matching pair, never mix installations.
    from tool_manager import find_tool
    for source_label, configured in [('Tool-Konfiguration', None)]:
        resolved, source = find_tool('ffmpeg', configured)
        if resolved:
            a, b = _candidate_pair(resolved)
            candidates.append((source, a, b))
    path_ffmpeg = shutil.which('ffmpeg')
    if path_ffmpeg:
        a, b = _candidate_pair(path_ffmpeg)
        candidates.append(('PATH', a, b))
    errors = []
    seen = set()
    for source, ffmpeg, ffprobe in candidates:
        identity = str(ffmpeg.resolve(strict=False))
        if identity in seen:
            continue
        seen.add(identity)
        if not ffmpeg.is_file() or not ffprobe.is_file():
            errors.append(f'{source}: FFmpeg/FFprobe pair missing in {ffmpeg.parent}')
            continue
        try:
            a = _version(ffmpeg, timeout)
            b = _version(ffprobe, timeout)
            return FFmpegTools(ffmpeg.resolve(), ffprobe.resolve(), a, b, source)
        except FFmpegError as exc:
            errors.append(f'{source}: {exc}')
    raise FFmpegError('No usable FFmpeg + FFprobe installation found. ' + '; '.join(errors))


def discover_from_config(config_file: str | os.PathLike[str]) -> FFmpegTools:
    """Read the configured FFmpeg executable from the central Tools section."""
    try:
        config = json.loads(Path(config_file).read_text(encoding='utf-8-sig'))
        if not isinstance(config, dict):
            raise ValueError('configuration root is not an object')
    except (OSError, ValueError) as exc:
        raise FFmpegError(f'Cannot read FFmpeg configuration: {exc}') from exc
    configured = (config.get('Tools') or {}).get('ffmpeg')
    return discover(configured)


def run_tool(executable: str | os.PathLike[str], args: Sequence[str | os.PathLike[str]],
             *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    """Run a tool without a shell; preserve stderr for diagnostics."""
    command = [str(executable), *(str(arg) for arg in args)]
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, encoding='utf-8', errors='replace',
                                timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FFmpegError(f'Process failed: {exc}') from exc
    if result.returncode:
        raise FFmpegError(f'Process exited with {result.returncode}: {result.stderr[-3000:]}')
    return result


def probe_json(tools: FFmpegTools, media_file: str | os.PathLike[str]) -> dict:
    """Return ffprobe JSON without interpreting streams or duplicating MediaInfo."""
    result = run_tool(tools.ffprobe, ['-v', 'error', '-show_format', '-show_streams',
                                     '-of', 'json', str(media_file)])
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise FFmpegError(f'Invalid ffprobe JSON: {exc}') from exc


# Cancellation is scoped to the worker thread, never to unrelated FFmpeg users.
import threading
_cancel_scope = threading.local()

class FFmpegCancelled(FFmpegError):
    """The user requested an immediate stop of the active FFmpeg job."""

def set_cancel_event(event):
    _cancel_scope.event = event


def _cancel_event():
    return getattr(_cancel_scope, 'event', None)


def run_ffmpeg(tools: FFmpegTools, args: Sequence[str | os.PathLike[str]],
               *, on_progress: Callable[[dict[str, str]], None] | None = None) -> None:
    """Execute FFmpeg; optional progress callback receives -progress key/value batches.

    stderr is redirected to a temporary file to avoid pipe deadlocks and retain
    the tail on failure. Callbacks run synchronously; callers must keep them fast.
    """
    import tempfile
    event = _cancel_event()
    if event is not None and event.is_set():
        raise FFmpegCancelled('FFmpeg cancelled before launch')
    command = [str(tools.ffmpeg), '-nostdin', '-hide_banner', '-nostats',
               '-progress', 'pipe:1', *(str(arg) for arg in args)]
    with tempfile.TemporaryFile(mode='w+t', encoding='utf-8', errors='replace') as err:
        try:
            with subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=err,
                                  text=True, encoding='utf-8', errors='replace',
                                  bufsize=1) as process:
                # The progress pipe can be silent for long periods. A daemon
                # watchdog stops FFmpeg promptly even when no progress arrives.
                finished = threading.Event()
                def watch_cancel():
                    if event is None:
                        return
                    while not finished.wait(0.1):
                        if event.is_set():
                            if process.poll() is None:
                                process.terminate()
                                try:
                                    process.wait(timeout=3)
                                except subprocess.TimeoutExpired:
                                    process.kill()
                            return
                watcher = threading.Thread(target=watch_cancel, daemon=True)
                watcher.start()
                progress: dict[str, str] = {}
                assert process.stdout is not None
                for line in process.stdout:
                    line = line.strip()
                    if '=' not in line:
                        continue
                    key, value = line.split('=', 1)
                    progress[key] = value
                    if key == 'progress':
                        if on_progress:
                            try:
                                on_progress(dict(progress))
                            except Exception:
                                process.kill()
                                process.wait()
                                raise
                        progress.clear()
                status = process.wait()
                finished.set()
                watcher.join(timeout=1)
        except OSError as exc:
            raise FFmpegError(f'Cannot launch FFmpeg: {exc}') from exc
        if event is not None and event.is_set():
            raise FFmpegCancelled('FFmpeg cancelled by user')
        if status:
            err.seek(0, os.SEEK_END)
            end = err.tell()
            err.seek(max(0, end - 4000))
            raise FFmpegError(f'FFmpeg exited with {status}: {err.read()}')


if __name__ == '__main__':
    try:
        tools = discover_from_config(Path(__file__).with_name('Media-Buddy.json'))
        print(f'FFmpeg: {tools.ffmpeg}\n{tools.ffmpeg_version}')
        print(f'FFprobe: {tools.ffprobe}\n{tools.ffprobe_version}')
        print(f'Source: {tools.source}')
    except FFmpegError as exc:
        print(f'FFmpeg check failed: {exc}')
        raise SystemExit(1)
