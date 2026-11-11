"""Media-Buddy V2: validated per-file intent, not an FFmpeg execution plan.

Technical decisions (DV/HDR, audio protection, encoder selection) stay in recode_file.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Mapping


class MissionError(ValueError):
    pass


@dataclass(frozen=True)
class RecodeMission:
    source: Path
    destination: Path
    video_mode: str
    audio_mode: str
    quality_level: int | None = None
    audio_tracks: tuple[dict, ...] | None = None

    @classmethod
    def from_dict(cls, data: Mapping) -> 'RecodeMission':
        if not isinstance(data, dict):
            raise MissionError('Mission must be a JSON object')
        allowed = {'Source', 'Destination', 'VideoMode', 'AudioMode', 'QualityLevel', 'AudioTracks'}
        unknown = set(data) - allowed
        if unknown:
            raise MissionError(f'Unknown mission fields: {sorted(unknown)}')
        for name in ('Source', 'Destination'):
            if not isinstance(data.get(name), str) or not data[name].strip():
                raise MissionError(f'{name} must be a non-empty path')
        for name in ('VideoMode', 'AudioMode'):
            if data.get(name) not in ('copy', 'recode'):
                raise MissionError(f'{name} must be copy or recode')
        quality = data.get('QualityLevel')
        if quality is not None and (type(quality) is not int or not 1 <= quality <= 7):
            raise MissionError('QualityLevel must be an integer from 1 to 7')
        tracks = data.get('AudioTracks')
        if tracks is not None:
            if not isinstance(tracks, list) or not tracks:
                raise MissionError('AudioTracks must be a non-empty list')
            seen = set()
            for track in tracks:
                if not isinstance(track, dict) or set(track) - {'SourceIndex', 'Mode', 'Normalize', 'KeepHiResOriginal'}:
                    raise MissionError('Invalid AudioTracks entry or unknown field')
                index = track.get('SourceIndex')
                if type(index) is not int or index < 0 or index in seen:
                    raise MissionError('AudioTracks SourceIndex must be unique nonnegative integer')
                seen.add(index)
                if track.get('Mode') not in ('copy', 'recode'):
                    raise MissionError('AudioTracks Mode must be copy or recode')
                if any(type(track[k]) is not bool for k in ('Normalize', 'KeepHiResOriginal') if k in track):
                    raise MissionError('AudioTracks boolean setting invalid')
                if track['Mode'] == 'copy' and track.get('Normalize') is True:
                    raise MissionError('AudioTracks copy cannot enable normalization')
        source = Path(data['Source']).expanduser().resolve()
        destination = Path(data['Destination']).expanduser().resolve()
        if source == destination:
            raise MissionError('Source and Destination must differ')
        return cls(source, destination, data['VideoMode'], data['AudioMode'], quality, tuple(dict(x) for x in tracks) if tracks is not None else None)

    @classmethod
    def from_json(cls, path: str | Path) -> 'RecodeMission':
        try:
            data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise MissionError(f'Cannot read mission {path}: {exc}') from exc
        return cls.from_dict(data)

    def to_dict(self) -> dict:
        data = {'Source': str(self.source), 'Destination': str(self.destination),
                'VideoMode': self.video_mode, 'AudioMode': self.audio_mode}
        if self.quality_level is not None:
            data['QualityLevel'] = self.quality_level
        if self.audio_tracks is not None:
            data['AudioTracks'] = [dict(x) for x in self.audio_tracks]
        return data


def make_mission(source: str | Path, destination: str | Path, *,
                 video_mode: str, audio_mode: str,
                 quality_level: int | None = None) -> RecodeMission:
    data = {'Source': str(source), 'Destination': str(destination),
            'VideoMode': video_mode, 'AudioMode': audio_mode}
    if quality_level is not None:
        data['QualityLevel'] = quality_level
    return RecodeMission.from_dict(data)
