"""Shared progress accounting for external tools and future GUI (Test 72).

The adapters own tool-specific parsing. ProgressTracker owns clocks, smoothed
speed, ETA and monotonic percentage. BatchProgress owns the second progress bar.
No Qt dependency, no background threads and no external executables.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Callable


def _positive(value):
    try:
        n = float(value)
        return n if math.isfinite(n) and n > 0 else None
    except (TypeError, ValueError):
        return None


def format_duration(seconds: float | None) -> str:
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return '--:--'
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f'{hours}:{minutes:02}:{secs:02}' if hours else f'{minutes}:{secs:02}'


@dataclass
class ProgressTracker:
    duration_seconds: float | None = None
    clock: Callable[[], float] = time.monotonic
    started: float = field(init=False)
    last_position: float = 0.0
    speed_smoothed: float | None = None
    last_time: float = field(init=False)
    last_speed_time: float = field(init=False)
    last_speed_position: float = 0.0

    def __post_init__(self):
        self.duration_seconds = _positive(self.duration_seconds)
        self.started = self.clock()
        self.last_time = self.started
        self.last_speed_time = self.started

    def update(self, position_seconds: float | None, *, complete: bool = False) -> dict:
        now = self.clock()
        elapsed = max(0.0, now - self.started)
        pos = _positive(position_seconds) or 0.0
        pos = max(pos, self.last_position)
        if self.duration_seconds:
            pos = min(pos, self.duration_seconds)
        # Estimate speed from a window rather than reacting to every tiny FFmpeg update.
        dt = now - self.last_speed_time
        if dt >= 1.0 and pos > self.last_speed_position:
            measured = (pos - self.last_speed_position) / dt
            self.speed_smoothed = measured if self.speed_smoothed is None else (0.25 * measured + 0.75 * self.speed_smoothed)
            self.last_speed_time = now
            self.last_speed_position = pos
        self.last_position = pos
        self.last_time = now
        percent = min(100.0, 100.0 * pos / self.duration_seconds) if self.duration_seconds else None
        # 100% encoded != verified and published. Keep phase explicit.
        if complete:
            percent = 100.0
        remaining = None
        if complete:
            remaining = 0.0
        elif self.duration_seconds and self.speed_smoothed and elapsed >= 3.0:
            remaining = max(0.0, self.duration_seconds - pos) / self.speed_smoothed
        return {
            'percent': percent,
            'position_seconds': pos,
            'elapsed_seconds': round(elapsed, 2),
            'remaining_seconds': round(remaining, 2) if remaining is not None else None,
            'speed': round(self.speed_smoothed, 2) if self.speed_smoothed else None,
            'status': 'complete' if complete else 'running',
        }


def ffmpeg_position(values: dict) -> float | None:
    """FFmpeg progress out_time_us/out_time_ms (both microseconds), or HH:MM:SS."""
    for key in ('out_time_us', 'out_time_ms'):
        if key in values:
            try:
                return max(0.0, int(values[key]) / 1_000_000)
            except (TypeError, ValueError):
                pass
    value = values.get('out_time')
    if value:
        try:
            hours, minutes, seconds = value.split(':')
            return max(0.0, int(hours) * 3600 + int(minutes) * 60 + float(seconds))
        except (TypeError, ValueError):
            pass
    return None


@dataclass
class BatchProgress:
    """Weighted total progress for a sequential batch.

    Weights are estimated work units (e.g. video seconds adjusted by expected
    encode speed); duration is a reasonable initial approximation, not a promise.
    """
    weights: list[float]
    clock: Callable[[], float] = time.monotonic
    started: float = field(init=False)
    completed_count: int = 0
    previous_percent: float = 0.0

    def __post_init__(self):
        if not self.weights or any(_positive(w) is None for w in self.weights):
            raise ValueError('Batch weights must be positive numbers')
        self.started = self.clock()

    def update(self, current_percent: float | None, *, current_remaining_seconds: float | None = None) -> dict:
        if not 0 <= self.completed_count <= len(self.weights):
            raise ValueError('Invalid batch item index')
        p = min(100., max(0., float(current_percent or 0.)))
        done = sum(self.weights[:self.completed_count])
        if self.completed_count < len(self.weights):
            done += self.weights[self.completed_count] * p / 100.
        percent = 100 * done / sum(self.weights)
        percent = max(percent, self.previous_percent)
        self.previous_percent = percent
        elapsed = max(0., self.clock() - self.started)
        # Extrapolation is only meaningful once a measurable amount of work is done.
        remaining = (elapsed * (100. - percent) / percent) if percent >= 2 and elapsed >= 3 else None
        if percent >= 100:
            remaining = 0.
        return {'percent': round(percent, 2), 'elapsed_seconds': round(elapsed, 2),
                'remaining_seconds': round(remaining, 2) if remaining is not None else None,
                'completed_files': self.completed_count, 'total_files': len(self.weights)}

    def finish_file(self) -> dict:
        if self.completed_count >= len(self.weights):
            raise ValueError('Batch already complete')
        self.completed_count += 1
        return self.update(0)
