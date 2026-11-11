"""Test 74: preserve source frame timing and verify the encoded stream.

For reliable CFR/VFR classification inspect frame timestamps rather than relying
on r_frame_rate or avg_frame_rate alone. Fail closed on unavailable timestamps.
"""
from __future__ import annotations
from fractions import Fraction
import json
import math
from pathlib import Path
from ffmpeg_manager import run_tool, FFmpegError

class FrameRateError(ValueError):
    pass

def frame_timestamps(tools, path, stream_index=0):
    args = ['-v','error','-select_streams',f'v:{stream_index}',
            '-show_frames','-show_entries','frame=best_effort_timestamp_time',
            '-of','json',str(path)]
    data=json.loads(run_tool(tools.ffprobe,args).stdout)
    times=[]
    for frame in data.get('frames',[]):
        value=frame.get('best_effort_timestamp_time')
        if value is not None and value != 'N/A':
            times.append(float(value))
    if len(times)<2 or any(not math.isfinite(t) for t in times):
        raise FrameRateError('Insufficient valid video frame timestamps')
    if any(b<=a for a,b in zip(times,times[1:])):
        raise FrameRateError('Non-increasing video frame timestamps')
    return times

def inspect_timing(tools,path,video_stream):
    times=frame_timestamps(tools,path)
    deltas=[b-a for a,b in zip(times,times[1:])]
    median=sorted(deltas)[len(deltas)//2]
    # PTS in common 1/1000 time bases may jitter by 1ms at 60fps.
    tolerance=max(0.0015,median*0.025)
    cfr=all(abs(d-median)<=tolerance for d in deltas)
    raw=video_stream.get('avg_frame_rate') or video_stream.get('r_frame_rate') or '0/0'
    try: declared=Fraction(raw)
    except (ValueError,ZeroDivisionError): declared=Fraction(0)
    observed=1/median
    # Use the declared rational only if consistent with actual frame intervals.
    if cfr and declared>0 and abs(float(declared)-observed)/observed < 0.03:
        fps=declared
    elif cfr:
        fps=Fraction(observed).limit_denominator(1001)
    else:
        fps=None
    return {'mode':'cfr' if cfr else 'vfr', 'fps':str(fps) if fps else None,
            'frame_count':len(times), 'median_interval':median,
            'first_timestamp':times[0], 'last_timestamp':times[-1],
            'duration_from_pts':times[-1]-times[0]}

def encoder_timing_args(timing):
    if timing['mode']=='cfr':
        return ['-fps_mode:v:0','passthrough','-enc_time_base:v:0',str(Fraction(1,1)/Fraction(timing['fps']))]
    return ['-fps_mode:v:0','passthrough']

def verify_timing(source,target):
    if source['mode']!=target['mode']:
        raise FrameRateError(f"Frame-rate mode changed: {source['mode']} -> {target['mode']}")
    if source['frame_count']!=target['frame_count']:
        raise FrameRateError(f"Frame count changed: {source['frame_count']} -> {target['frame_count']}")
    if source['mode']=='cfr':
        a=float(Fraction(source['fps'])); b=float(Fraction(target['fps']))
        if abs(a-b)/a>0.0001:
            raise FrameRateError(f'Frame rate changed: {a:g} -> {b:g}')
    else:
        if abs(source['duration_from_pts']-target['duration_from_pts']) > max(.04,source['duration_from_pts']*.001):
            raise FrameRateError('VFR frame-timestamp span changed')
