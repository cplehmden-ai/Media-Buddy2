"""Shared Media-Buddy audio command, rescue and verification helpers.

Extracted without algorithm changes from the validated Test 100-10 audio path.
The production Recode engine must use the same functions after stream mapping
and cutlist/HDR integration has been verified.
"""
from __future__ import annotations
import json
import subprocess
from pathlib import Path
from dataclasses import replace
from audio_plan import CODECS
ENCODERS = {'dts': 'dca'}

def run(cmd, timeout=None):
    return subprocess.run([str(x) for x in cmd], capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=timeout)


def probe_file(probe, path):
    r=run([probe,'-v','error','-show_entries',
           'format=format_name:stream=index,codec_type,codec_name,profile,channels,channel_layout,ch_layout:stream_disposition=default:stream_tags=language,title',
           '-of','json',path],timeout=120)
    if r.returncode: raise RuntimeError(f'ffprobe: {r.stderr[-1000:]}')
    return json.loads(r.stdout)


def build_command(ffmpeg, source, target, info, plan, afilter, bitrate, codec_rates, simulate_dts_failure=False):
    streams={int(s['index']):s for s in info.get('streams',[])}
    cmd=[str(ffmpeg),'-hide_banner','-nostdin','-n','-i',str(source)]
    # Explicit maps prevent FFmpeg's automatic stream selection. All video streams copied.
    cmd+=['-map','0:v?', '-c:v','copy']
    for out in plan: cmd+=['-map',f'0:{out.source_index}']
    # Preserve subtitle and attachment streams where MKV can mux them. Data streams are not mapped.
    cmd+=['-map','0:s?', '-map','0:t?', '-c:s','copy', '-c:t','copy']
    for i,out in enumerate(plan):
        codec='copy' if out.copy else ENCODERS.get(out.codec,out.codec)
        cmd += [f'-c:a:{i}',codec]
        if not out.copy:
            # Clear inherited source stream statistics for re-encoded tracks.
            cmd += [f'-map_metadata:s:a:{i}', '-1']
        # Media-Buddy V1 rule: native FFmpeg DTS (dca) encoder is experimental.
        # Apply only to DTS encoding, never to copied DTS-HD MA / DTS:X originals.
        if not out.copy and out.codec == 'dts' and not simulate_dts_failure:
            cmd += [f'-strict:a:{i}', '-2']
        if not out.copy:
            if out.codec not in ('flac',):
                # DTS 5.1: 448k from global AC3/EAC3 settings fails dca initialization.
                # Keep the user-configured bitrate for all other codecs.
                effective_bitrate = codec_rates.get(out.codec, bitrate)
                cmd += [f'-b:a:{i}',f'{effective_bitrate}k']
            if afilter: cmd += [f'-filter:a:{i}',afilter]
            if out.target_channels is not None and out.source_channels is not None and out.target_channels < out.source_channels:
                if out.target_channels != 6: raise ValueError(f'Unsupported target downmix {out.target_channels}')
                # FFmpeg's channel-layout-aware rematrixing. Do not truncate channels with pan.
                source_layout=str(streams[out.source_index].get('channel_layout') or streams[out.source_index].get('ch_layout') or '')
                if not source_layout or source_layout.lower() in ('unknown','unspecified'):
                    raise ValueError(f'Cannot safely downmix stream #{out.source_index}: channel layout unknown')
                cmd += [f'-ac:a:{i}','6',f'-channel_layout:a:{i}','5.1(side)']
        cmd += [f'-disposition:a:{i}','default' if out.default else '0']
        src=streams[out.source_index]
        lang=(src.get('tags') or {}).get('language')
        if lang and lang.lower() not in ('und','?'): cmd += [f'-metadata:s:a:{i}',f'language={lang}']
        title=(src.get('tags') or {}).get('title','')
        if out.purpose=='everyday': title='Alltag (bearbeitet)'
        elif out.purpose=='processed': title='Bearbeitet' if not title else title
        elif out.purpose=='original' and len(plan)>1: title='Original (unverändert)'
        if title: cmd += [f'-metadata:s:a:{i}',f'title={title}']
    cmd += ['-map_metadata','0','-map_chapters','0','-f','matroska',str(target)]
    return cmd


def retryable_dts_encoder_error(result):
    """Do not mask unrelated FFmpeg failures (input, muxer, disk, video)."""
    message = (result.stderr or '').lower()
    return ('experimental' in message and ('dca' in message or 'dts' in message)) or (
        'error while opening encoder' in message and ('dca' in message or 'dts' in message))


def fallback_plan(plan, fallback):
    """Replace only encoded DTS outputs, preserving copied originals and flags."""
    from audio_plan import MAX_CHANNELS
    outputs = []
    changed = 0
    for out in plan:
        if out.copy or out.codec != 'dts':
            outputs.append(out)
            continue
        max_channels = MAX_CHANNELS.get(fallback)
        target = out.source_channels
        if max_channels is not None and target is not None:
            target = min(target, max_channels)
        note = out.channel_note
        if target is not None and out.source_channels is not None and target < out.source_channels:
            note = f'REQUIRES DOWNMIX: {out.source_channels} -> {target} channels; explicit layout/mix validation before encoding'
        outputs.append(replace(out, codec=fallback, target_channels=target,
                               channel_note=note, reason=out.reason + '; encoder failed, automatic fallback'))
        changed += 1
    return outputs, changed


def rescue_plan(plan, codec, source_streams):
    """Replace re-encoded audio only; never alter an existing protected original."""
    from audio_plan import MAX_CHANNELS
    result=[]
    for out in plan:
        if out.copy:
            result.append(out)
            continue
        if codec == 'copy':
            # Last resort: original audio as the only track for this source.
            # Avoid duplicating an already protected original stream.
            if any(x.copy and x.source_index == out.source_index for x in plan):
                # Promote existing protected original to default if the processed
                # default track is being dropped (handled below).
                continue
            result.append(replace(out, copy=True, codec=source_streams[out.source_index].get('codec_name','copy'),
                                  target_channels=out.source_channels, channel_note='',
                                  reason=out.reason+'; emergency original copy'))
            continue
        channels=out.source_channels
        max_channels=MAX_CHANNELS.get(codec)
        if channels is not None and max_channels is not None:
            channels=min(channels,max_channels)
        note=''
        if channels is not None and out.source_channels is not None and channels<out.source_channels:
            note=f'REQUIRES DOWNMIX: {out.source_channels} -> {channels} channels; explicit layout/mix validation before encoding'
        result.append(replace(out,codec=codec,target_channels=channels,channel_note=note,
                              reason=out.reason+'; automatic rescue'))
    if codec=='copy' and result:
        # A dropped processed default leaves the protected original as default.
        if not any(x.default for x in result):
            result=[replace(x,default=(i==0)) for i,x in enumerate(result)]
    return result


def encoder_failure(result):
    """Retry only encoder-related errors, not disk, input, or muxer failures."""
    msg=(result.stderr or '').lower()
    return any(token in msg for token in (
        'error while opening encoder', 'could not open encoder',
        'unknown encoder', 'encoder not found', 'error initializing output stream',
        'experimental encoder', 'not currently supported in the selected encoder'))


def force_encoder_failure(command, plan):
    """Test-only: replace first actual encoder with a deliberately missing one."""
    cmd=list(command)
    for i,out in enumerate(plan):
        if not out.copy:
            flag=f'-c:a:{i}'
            pos=cmd.index(flag)
            cmd[pos+1]='media_buddy_test_missing_encoder'
            return cmd
    return cmd


def verify(probe, source_info, target, plan):
    result=probe_file(probe,target)
    audio=[s for s in result.get('streams',[]) if s.get('codec_type')=='audio']
    if len(audio)!=len(plan): raise RuntimeError(f'Expected {len(plan)} audio tracks, got {len(audio)}')
    source={s['index']:s for s in source_info['streams']}
    for i,(actual,wanted) in enumerate(zip(audio,plan)):
        expected=(source[wanted.source_index].get('codec_name') if wanted.copy else wanted.codec)
        if actual.get('codec_name')!=expected:
            raise RuntimeError(f'Audio #{i}: codec {actual.get("codec_name")} instead of {expected}')
        if wanted.target_channels is not None and actual.get('channels')!=wanted.target_channels:
            raise RuntimeError(f'Audio #{i}: {actual.get("channels")} channels instead of {wanted.target_channels}')
        if bool((actual.get('disposition') or {}).get('default'))!=wanted.default:
            raise RuntimeError(f'Audio #{i}: wrong default flag')
    v0=sum(s.get('codec_type')=='video' for s in source_info['streams'])
    v1=sum(s.get('codec_type')=='video' for s in result.get('streams',[]))
    if v0!=v1: raise RuntimeError(f'Video count changed {v0} -> {v1}')
    return audio


