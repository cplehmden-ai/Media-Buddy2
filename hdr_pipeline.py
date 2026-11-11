"""Test84: fail-closed HEVC HDR metadata reinjection for staged Matroska recodes.

The source is read only. All temporary files are held in a private directory.
Only call this after a normal video+audio+subtitle MKV has been staged.
"""
from __future__ import annotations
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

class HDRPipelineError(RuntimeError): pass

def _tool(root: Path, name: str) -> str:
    from tool_manager import get_tool
    try:
        return get_tool(name)
    except FileNotFoundError as exc:
        raise HDRPipelineError(str(exc)) from exc

def _run(*cmd):
    start=time.perf_counter()
    p=subprocess.run([str(v) for v in cmd],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,errors='replace')
    duration=time.perf_counter()-start
    name=Path(str(cmd[0])).stem.lower()
    action=next((str(v) for v in cmd[1:] if str(v) in ('extract-rpu','inject-rpu','extract','inject')), '')
    print(f'[HDR timing] {name} {action}: {duration:.2f} s (exit={p.returncode})', flush=True)
    if p.returncode: raise HDRPipelineError('Command failed ('+str(p.returncode)+'): '+subprocess.list2cmdline([str(v) for v in cmd])+'\n'+p.stdout[-3000:])
    return p.stdout

def _hash(p):
    h=hashlib.sha256()
    with open(p,'rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''): h.update(block)
    return h.hexdigest()

def _rpu_first_difference(a: Path, b: Path) -> str:
    """Binary diagnostic only: never treat matching length as RPU equivalence."""
    offset = 0
    with a.open('rb') as left, b.open('rb') as right:
        while True:
            x, y = left.read(65536), right.read(65536)
            for i, (vx, vy) in enumerate(zip(x, y)):
                if vx != vy:
                    return f'first_byte_offset={offset+i}, expected=0x{vx:02x}, actual=0x{vy:02x}'
            if len(x) != len(y):
                return f'first_length_difference_offset={offset+min(len(x),len(y))}'
            if not x:
                return 'binary_identical'
            offset += len(x)


def _rpu_summary(root: Path, path: Path, label: str) -> None:
    """Best-effort diagnosis; summary output must not weaken fail-closed checks."""
    try:
        tool = _tool(root, 'dovi_tool')
        proc = subprocess.run([tool, 'info', '-s', '-i', str(path)],
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, errors='replace', timeout=45)
        if proc.returncode == 0:
            print(f'[HDR diagnostic] {label} summary:\n{proc.stdout.strip()[-1800:]}', flush=True)
        else:
            print(f'[HDR diagnostic] {label} summary unavailable (exit={proc.returncode})', flush=True)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f'[HDR diagnostic] {label} summary unavailable: {exc}', flush=True)


def _rpu_frame_count(root: Path, rpu: Path) -> int:
    """Count entries using dovi_tool's parsed RPU summary, not file size."""
    out = _run(_tool(root, 'dovi_tool'), 'info', '-s', '-i', rpu)
    match = re.search(r'(?m)^\s*Frames:\s*(\d+)\s*$', out)
    if not match:
        raise HDRPipelineError('Cannot establish source Dolby Vision RPU count; refusing unverified P7 conversion')
    return int(match.group(1))


def _preflight_p7_alignment(info: dict, ffprobe: Path, root: Path) -> None:
    """Check source decoded frames against converted P7 RPU *before* encoding.

    Never synthesize or duplicate metadata for unmatched pictures. The result
    also caches the expensive source PTS scan for the final verification.
    """
    rpu_count = _rpu_frame_count(root, info['rpu'])
    pts = _frame_timestamps(ffprobe, info['input_path'])
    info['source_pts'] = pts
    info['source_rpu_count'] = rpu_count
    delta = len(pts) - rpu_count
    print(f'[HDR preflight] P7 source decoded frames={len(pts)}, converted RPU entries={rpu_count}, delta={delta:+d}', flush=True)
    if delta:
        # A small numeric difference is not sufficient: require an isolated
        # suffix gap in the source HEVC NAL order (95H analysis).
        from test95h_dv_injection import nal_summary
        structure = nal_summary(info['source_hevc'])
        missing = structure['zeros']
        intervals = [b-a for a,b in zip(pts,pts[1:])]
        fps = 1.0 / sorted(intervals)[len(intervals)//2]
        limit = max(1, int(0.25 * fps + 1e-8))
        suffix = bool(missing) and missing == list(range(missing[0],missing[-1]+1)) and missing[-1] >= len(pts)-4
        accepted = (0 < delta <= limit and len(missing) == delta and suffix
                    and structure['pictures'] == len(pts) and structure['rpus'] == rpu_count
                    and not structure['multiple'])
        print(f'[HDR preflight 95J] missing_NAL_order={missing}, tolerance_frames={limit}, '
              f'tolerance_seconds=0.25, suffix={suffix}, accepted={accepted}', flush=True)
        if not accepted:
            raise HDRPipelineError(f'P7 RPU/frame mismatch: frames={len(pts)}, RPU={rpu_count}, '
                                   f'missing={missing}; not eligible for 95J suffix tolerance')
        info['rpu_tolerance_95j'] = {'missing':missing, 'frames':delta, 'limit':limit,
                                     'seconds':delta/fps}
        print('[HDR WARNING] Experimental 95J suffix-RPU tolerance accepted; '
              'dovi_tool may fill missing RPU entries. Output will be checked.', flush=True)


def _scene(p):
    data=json.loads(Path(p).read_text(encoding='utf-8'))
    scene=data.get('SceneInfo')
    if not isinstance(scene,list): raise HDRPipelineError('Invalid HDR10+ JSON')
    return scene

def inspect(source: Path, video: dict, ffmpeg: Path, ffprobe: Path, root: Path, work: Path, *, convert_p7: bool = False) -> dict:
    """Extract full source HEVC once; probe optional HDR10+ and Dolby Vision."""
    transfer=video.get('color_transfer') or ''
    side=video.get('side_data_list') or []
    dv_entries=[s for s in side if 'DOVI' in str(s.get('side_data_type','')).upper()]
    dv=bool(dv_entries)
    if dv:
        for entry in dv_entries:
            profile=entry.get('dv_profile')
            compat=entry.get('dv_bl_signal_compatibility_id')
            if profile is not None and int(profile)!=8 and not (convert_p7 and int(profile)==7):
                raise HDRPipelineError(f'Dolby Vision profile {profile} unsupported for re-encoding (only P8.1/P8.4)')
            if compat is not None and int(compat) not in (1,4) and not (convert_p7 and int(compat)==6):
                raise HDRPipelineError(f'Dolby Vision compatibility ID {compat} unsupported (only P8.1/P8.4)')
    if dv and any(int(e.get('dv_bl_signal_compatibility_id', -1)) == 4 for e in dv_entries) and transfer != 'arib-std-b67':
        raise HDRPipelineError('DV P8.4 requires HLG transfer signaling; refusing to convert unknown base')
    if transfer == 'arib-std-b67': kind='HLG'
    elif transfer == 'smpte2084': kind='HDR10'
    else: kind='SDR'
    info={'format':kind,'dolby_vision':dv,'hdr10plus':False,'source_hevc':None,'rpu':None,'hdr_json':None,'input_path':source, 'convert_p7':convert_p7}
    if kind!='HDR10' and not dv: return info
    hevc=work/'original.hevc'
    _run(ffmpeg,'-hide_banner','-loglevel','error','-nostdin','-y','-i',source,'-map','0:v:0','-c:v','copy','-bsf:v','hevc_mp4toannexb','-f','hevc',hevc)
    info['source_hevc']=hevc
    if dv:
        dovi=_tool(root,'dovi_tool')
        rpu=work/'original_rpu.bin'
        _run(dovi,*(['-m','2'] if convert_p7 else []),'extract-rpu','-i',hevc,'-o',rpu)
        if not rpu.is_file() or not rpu.stat().st_size: raise HDRPipelineError('Dolby Vision RPU extraction empty')
        info['rpu']=rpu
        if convert_p7:
            _preflight_p7_alignment(info, ffprobe, root)
    # Probe HDR10+ only for PQ HDR10 (including DV P8.1). P8.4 has an HLG base.
    if kind=='HDR10':
        hdrtool=_tool(root,'hdr10plus_tool')
        hdr=work/'original_hdr10plus.json'
        proc=subprocess.run([hdrtool,'extract',str(hevc),'-o',str(hdr)],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,errors='replace')
        if proc.returncode==0 and hdr.is_file() and hdr.stat().st_size:
            scene=_scene(hdr)
            if scene:
                info['hdr10plus']=True;info['hdr_json']=hdr
        elif dv:
            # DV-only is fine, but HDR10+ must not be falsely assumed.
            pass
    return info


def _frame_timestamps(ffprobe, path: Path) -> list[float]:
    """Decoded presentation timestamps, in display order (seconds)."""
    data = json.loads(_run(ffprobe, '-v', 'error', '-select_streams', 'v:0',
                           '-show_frames', '-show_entries',
                           'frame=best_effort_timestamp_time', '-of', 'json', path))
    values = []
    for frame in data.get('frames', []):
        ts = frame.get('best_effort_timestamp_time')
        if ts is None or ts == 'N/A':
            raise HDRPipelineError('Missing frame PTS; cannot safely preserve VFR')
        values.append(float(ts))
    if not values or any(b <= a for a, b in zip(values, values[1:])):
        raise HDRPipelineError('Invalid/nonmonotonic frame PTS')
    return values

def _packet_timestamps(ffprobe, path: Path) -> list[float]:
    """Fast path: presentation timestamps from video packets, without decoding.

    Matroska/HEVC normally carries one access unit per packet. Reject
    missing/duplicate timestamps and leave full decoded-frame probing as fallback.
    Sorting gives presentation order, independent of B-frame packet order.
    """
    data = json.loads(_run(ffprobe, '-v', 'error', '-select_streams', 'v:0',
                           '-show_packets', '-show_entries',
                           'packet=pts_time', '-of', 'json', path))
    packets = data.get('packets', [])
    if not packets:
        raise HDRPipelineError('No video packets returned')
    values = []
    for packet in packets:
        ts = packet.get('pts_time')
        if ts is None or ts == 'N/A':
            raise HDRPipelineError('Video packet without PTS')
        values.append(float(ts))
    values.sort()
    if any(b <= a for a, b in zip(values, values[1:])):
        raise HDRPipelineError('Duplicate/nonmonotonic packet PTS')
    return values


def _timestamps_fast_or_decode(ffprobe, path: Path, label: str) -> tuple[list[float], str]:
    """Use packet PTS when valid, otherwise retain the 95J decoded-frame path."""
    try:
        pts = _packet_timestamps(ffprobe, path)
        print(f'[HDR 95K] {label}: fast packet PTS, entries={len(pts)}', flush=True)
        return pts, 'packet'
    except (HDRPipelineError, ValueError, KeyError, TypeError) as exc:
        print(f'[HDR 95K] {label}: packet scan unsuitable ({exc}); decoding frames', flush=True)
        return _frame_timestamps(ffprobe, path), 'decoded'


def _timestamps_v2(path: Path, timestamps: list[float]) -> None:
    # mkvmerge timestamps v2 are millisecond timestamps, decimals supported.
    zero = timestamps[0]
    path.write_text('# timestamp format v2\n' + ''.join(
        f'{(t-zero)*1000:.6f}\n' for t in timestamps), encoding='ascii')

def restore(staged_mkv: Path, info: dict, ffmpeg: Path, ffprobe: Path, root: Path, work: Path) -> dict:
    """Reinject metadata, mux selected audio/subtitles/chapters from staged MKV, verify final."""
    if not (info['dolby_vision'] or info['hdr10plus']): return {'processed':False}
    if not staged_mkv.suffix.lower()=='.mkv': raise HDRPipelineError('Dynamic HDR reinjection requires MKV')
    encoded=work/'encoded.hevc'
    _run(ffmpeg,'-hide_banner','-loglevel','error','-nostdin','-y','-i',staged_mkv,'-map','0:v:0','-c:v','copy','-bsf:v','hevc_mp4toannexb','-f','hevc',encoded)
    # Avoid three expensive raw-HEVC ffprobe -count_frames passes. We already
    # need decoded source/staged/final PTS, which give us frame counts as well.
    # Keep the complete decoded-frame PTS checks: never rely on header estimates.
    # A raw HEVC stream has no original timestamps. Capture them from the
    # staged MKV before reinjection and supply explicit timestamps to mkvmerge.
    # 95K: P7 conversion keeps the fully decoded 95J reference path.
    # For P8.1/HDR10+ use fast container packet timestamps; fall back on
    # decoded-frame timestamps when packet timing is incomplete or ambiguous.
    fast_timing = not info.get('convert_p7')
    if fast_timing:
        staged_pts, staged_method = _timestamps_fast_or_decode(ffprobe, staged_mkv, 'staged')
    else:
        staged_pts = _frame_timestamps(ffprobe, staged_mkv)
        staged_method = 'decoded'
    source_pts = info.get('source_pts')
    if source_pts is None:
        if fast_timing:
            source_pts, source_method = _timestamps_fast_or_decode(ffprobe, info['input_path'], 'source')
        else:
            source_pts = _frame_timestamps(ffprobe, info['input_path'])
            source_method = 'decoded'
    else:
        source_method = 'decoded'
        print(f'[HDR performance] Reusing {len(source_pts)} preflight source PTS; avoiding duplicate full decode', flush=True)
    source_count = len(source_pts)
    if info.get('source_rpu_count') is not None and info['source_rpu_count'] != source_count and not info.get('rpu_tolerance_95j'):
        raise HDRPipelineError('Source RPU count changed since preflight')
    if len(staged_pts) != source_count:
        raise HDRPipelineError(
            f'PTS/frame mismatch: source PTS={len(source_pts)}, encoded PTS={len(staged_pts)}, '
            f'source frames={source_count}; refusing unsafe RPU alignment')
    if info['hdr10plus'] and len(_scene(info['hdr_json'])) != source_count:
        raise HDRPipelineError('HDR10+ source frame mismatch')
    print(f'[HDR performance] source/staged PTS entries: {source_count}/{len(staged_pts)} '
          f'(source={source_method}, staged={staged_method})', flush=True)
    # A re-encode can alter the timestamps without dropping frames (notably
    # iPhone variable-frame-rate video). Keep the *source* presentation timing
    # for the rebuilt stream rather than inheriting the altered staged timing.
    # RPU injection remains gated on identical frame counts. Note that frame
    # count alone cannot prove identical picture ordering; do not claim that.
    offsets = [((b-staged_pts[0])-(a-source_pts[0])) for a,b in zip(source_pts,staged_pts)]
    max_delta = max(abs(v) for v in offsets)
    first_changed = next((i for i,v in enumerate(offsets) if abs(v)>0.003), None)
    timing_restored = first_changed is not None
    if timing_restored:
        print('HDR timing: staged PTS differs from source; restoring original VFR timestamps. '
              f'frames={source_count}, first_mismatch_frame={first_changed}, '
              f'max_delta_ms={max_delta*1000:.3f}', flush=True)
    timestamps_file = work/'video_timestamps.txt'
    _timestamps_v2(timestamps_file, source_pts)
    current=encoded
    if info['dolby_vision']:
        dv=work/'with_dv.hevc'
        _run(_tool(root,'dovi_tool'),'inject-rpu','-i',current,'--rpu-in',info['rpu'],'-o',dv)
        current=dv
    if info['hdr10plus']:
        both=work/'with_hdr10plus.hevc'
        _run(_tool(root,'hdr10plus_tool'),'inject','-i',current,'-j',info['hdr_json'],'-o',both)
        current=both
    mkvmerge=_tool(root,'mkvmerge')
    rebuilt=work/'rebuilt.mkv'
    # Input #1 supplies the existing selected audio/subtitles, attachments and chapters.
    _run(mkvmerge,'--output',rebuilt,'--timestamps',f'0:{timestamps_file}',str(current),'--no-video',str(staged_mkv))
    verify_hevc=work/'verify.hevc'
    _run(ffmpeg,'-hide_banner','-loglevel','error','-nostdin','-y','-i',rebuilt,'-map','0:v:0','-c:v','copy','-bsf:v','hevc_mp4toannexb','-f','hevc',verify_hevc)
    if fast_timing:
        final_pts, final_method = _timestamps_fast_or_decode(ffprobe, rebuilt, 'rebuilt')
    else:
        final_pts = _frame_timestamps(ffprobe, rebuilt)
        final_method = 'decoded'
    print(f'[HDR 95K] timing methods: source={source_method}, staged={staged_method}, final={final_method}', flush=True)
    if len(final_pts) != source_count: raise HDRPipelineError(f'MKV frame count mismatch: expected={source_count}, actual={len(final_pts)}')
    # mkvmerge timestamps v2 is relative to the first source PTS.
    if len(final_pts) != len(source_pts) or any(
        abs((a-source_pts[0])-(b-final_pts[0])) > 0.002
        for a,b in zip(source_pts,final_pts)
    ):
        raise HDRPipelineError('MKV timestamps differ from original source after HDR reinjection')
    if info['dolby_vision']:
        got=work/'verify_rpu.bin'
        _run(_tool(root,'dovi_tool'),'extract-rpu','-i',verify_hevc,'-o',got)
        expected_hash=_hash(info['rpu'])
        actual_hash=_hash(got)
        if info.get('rpu_tolerance_95j'):
            injected_rpu=work/'diagnostic_injected_rpu.bin'
            _run(_tool(root,'dovi_tool'),'extract-rpu','-i',dv,'-o',injected_rpu)
            # Require exactly one metadata entry per output picture, identical
            # injection and remux results, and unchanged original RPU prefix.
            if (_rpu_frame_count(root,got)!=source_count or
                _rpu_frame_count(root,injected_rpu)!=source_count or
                _hash(injected_rpu)!=actual_hash):
                raise HDRPipelineError('95J: injected/remuxed RPU count or content mismatch')
            original_bytes=info['rpu'].read_bytes()
            actual_bytes=got.read_bytes()
            if not actual_bytes.startswith(original_bytes):
                raise HDRPipelineError('95J: original converted RPU payload is not an exact prefix')
            print(f'[HDR 95J] Verified {source_count} output RPUs; original {info["source_rpu_count"]} '
                  'RPU entries retained as binary prefix; appended entries validated by count.', flush=True)
        elif actual_hash!=expected_hash:
                print('[HDR diagnostic] RPU mismatch after mux; checking whether the change happened during injection or muxing.', flush=True)
                injected_rpu=work/'diagnostic_injected_rpu.bin'
                _run(_tool(root,'dovi_tool'),'extract-rpu','-i',dv,'-o',injected_rpu)
                injected_hash=_hash(injected_rpu)
                print(f'[HDR diagnostic] expected converted RPU: bytes={info["rpu"].stat().st_size}, sha256={expected_hash}',flush=True)
                print(f'[HDR diagnostic] after injection:      bytes={injected_rpu.stat().st_size}, sha256={injected_hash}',flush=True)
                print(f'[HDR diagnostic] after mux:            bytes={got.stat().st_size}, sha256={actual_hash}',flush=True)
                print('[HDR diagnostic] expected vs injected: '+_rpu_first_difference(info['rpu'],injected_rpu), flush=True)
                print('[HDR diagnostic] injected vs mux: '+_rpu_first_difference(injected_rpu,got), flush=True)
                _rpu_summary(root, info['rpu'], 'expected converted RPU')
                _rpu_summary(root, injected_rpu, 'after injection RPU')
                if injected_hash!=expected_hash:
                    stage='injection or RPU extraction/normalization'
                else:
                    stage='muxing or post-mux extraction'
                raise HDRPipelineError('Dolby Vision RPU differs after mux; first differing stage: '+stage)
    if info['hdr10plus']:
        got=work/'verify_hdr10plus.json'
        _run(_tool(root,'hdr10plus_tool'),'extract',verify_hevc,'-o',got)
        if _scene(got)!=_scene(info['hdr_json']): raise HDRPipelineError('HDR10+ metadata differs after mux')
    if info.get('convert_p7'):
        probe=json.loads(_run(ffprobe,'-v','error','-select_streams','v:0','-show_entries','stream_side_data=dv_profile,dv_bl_signal_compatibility_id,el_present_flag,rpu_present_flag','-show_streams','-of','json',rebuilt))
        records=[sd for st in probe.get('streams',[]) for sd in st.get('side_data_list',[]) if sd.get('side_data_type')=='DOVI configuration record']
        if len(records)!=1 or int(records[0].get('dv_profile',-1))!=8 or int(records[0].get('dv_bl_signal_compatibility_id',-1))!=1 or int(records[0].get('el_present_flag',-1))!=0 or int(records[0].get('rpu_present_flag',-1))!=1:
            raise HDRPipelineError('P7 to P8.1 output signaling not verified; refusing publication: '+str(records))
    # Replace only staged file, never published destination or source.
    os.replace(rebuilt,staged_mkv)
    return {'processed':True,'frames':source_count,'dolby_vision_verified':info['dolby_vision'],'hdr10plus_verified':info['hdr10plus'], 'timestamps_verified':True, 'source_timestamps_restored':timing_restored, 'max_staged_timestamp_delta_ms':round(max_delta*1000,3), 'first_staged_timestamp_mismatch_frame':first_changed, 'converted_p7_to_p81':bool(info.get('convert_p7')), 'rpu_tolerance_95j':info.get('rpu_tolerance_95j')}
