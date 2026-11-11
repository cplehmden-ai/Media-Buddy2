"""Test 75: codec-aware FFmpeg encoder selection and capability probing.

A listed FFmpeg encoder is NOT proof of usable hardware. Optional smoke tests
perform an actual one-frame encode on the target machine.
"""
from __future__ import annotations
import subprocess
from dataclasses import dataclass

CODECS = {
    'h264': {'software':'libx264','qsv':'h264_qsv','nvenc':'h264_nvenc','amf':'h264_amf','vaapi':'h264_vaapi','videotoolbox':'h264_videotoolbox'},
    'hevc': {'software':'libx265','qsv':'hevc_qsv','nvenc':'hevc_nvenc','amf':'hevc_amf','vaapi':'hevc_vaapi','videotoolbox':'hevc_videotoolbox'},
    'av1':  {'software':'libsvtav1','qsv':'av1_qsv','nvenc':'av1_nvenc','amf':'av1_amf','vaapi':'av1_vaapi','videotoolbox':'av1_videotoolbox'},
}
CODEC_NAMES={'h264':'h264','hevc':'hevc','av1':'av1'}
HARDWARE_ORDER=('qsv','nvenc','amf','videotoolbox','vaapi')
class EncoderError(ValueError): pass

def installed_encoders(ffmpeg):
    run=subprocess.run([str(ffmpeg),'-hide_banner','-encoders'],capture_output=True,text=True,errors='replace',timeout=20)
    if run.returncode: raise EncoderError('Cannot list FFmpeg encoders: '+run.stderr[-500:])
    names=set()
    for line in run.stdout.splitlines():
        fields=line.strip().split()
        if len(fields)>=2 and len(fields[0])==6 and fields[0][0]=='V': names.add(fields[1])
    return names

def smoke_test(ffmpeg, encoder, *, seconds=1):
    # Smoke test verifies real usability; a driver can still fail for other resolutions/profiles.
    # Use small 128x128 frames; output goes to the null muxer.
    cmd=[str(ffmpeg),'-nostdin','-hide_banner','-loglevel','error','-f','lavfi','-i',
         'color=c=black:s=128x128:r=25','-frames:v','2','-an','-c:v',encoder]
    if encoder.endswith('_vaapi'):
        cmd += ['-vf','format=nv12,hwupload']
    cmd += ['-f','null','-']
    try:
        result=subprocess.run(cmd,capture_output=True,text=True,errors='replace',timeout=25)
        return {'usable':result.returncode==0,'error':result.stderr.strip()[-400:] if result.returncode else None}
    except (subprocess.TimeoutExpired,OSError) as exc:
        return {'usable':False,'error':str(exc)}

def capabilities(ffmpeg, *, verify_hardware=True):
    listed=installed_encoders(ffmpeg)
    result={}
    for codec,backends in CODECS.items():
        result[codec]={}
        for backend,name in backends.items():
            present=name in listed
            tested=present and (verify_hardware or backend=='software')
            check=smoke_test(ffmpeg,name) if tested else None
            result[codec][backend]={'encoder':name,'installed':present,
                'usable':check['usable'] if check else None,
                'error':check['error'] if check else None}
    return result

def resolve_encoder(ffmpeg, codec, backend='auto', *, hardware_enabled=True, requested=None,
                    verify_hardware=True):
    if codec not in CODECS: raise EncoderError('Unsupported output codec: '+str(codec))
    if backend not in ('auto',*CODECS[codec]): raise EncoderError('Unsupported encoder backend: '+str(backend))
    listed=installed_encoders(ffmpeg)
    candidates=CODECS[codec]
    if requested:
        if requested not in candidates.values(): raise EncoderError(f'{requested} is not an encoder for {codec}')
        backend=next(k for k,v in candidates.items() if v==requested)
    if backend not in ('auto','software') and not hardware_enabled:
        raise EncoderError('Hardware encoding is disabled in configuration')
    order=(list(HARDWARE_ORDER)+['software'] if hardware_enabled else ['software']) if backend=='auto' else [backend]
    problems=[]
    for kind in order:
        name=candidates.get(kind)
        if not name: continue
        if name not in listed:
            problems.append(name+': not included in FFmpeg'); continue
        if verify_hardware:
            check=smoke_test(ffmpeg,name)
            if not check['usable']:
                problems.append(name+': '+str(check['error']));continue
        return name,kind,problems
    raise EncoderError(f'No usable {codec} encoder: '+ '; '.join(problems))

def video_args(encoder,quality,preset='medium'):
    if encoder=='libx264' or encoder=='libx265':
        return ['-c:v',encoder,'-crf',str(quality),'-preset',preset], 'crf'
    if encoder=='libsvtav1':
        return ['-c:v',encoder,'-crf',str(quality),'-preset','6'], 'crf'
    if encoder.endswith('_qsv'):
        return ['-c:v',encoder,'-global_quality',str(quality)], 'global_quality'
    if encoder.endswith('_nvenc'):
        return ['-c:v',encoder,'-rc','vbr','-cq',str(quality),'-b:v','0'], 'cq'
    if encoder.endswith('_amf'):
        return ['-c:v',encoder,'-rc','cqp','-qp_i',str(quality),'-qp_p',str(quality)], 'cqp'
    if encoder.endswith('_videotoolbox'):
        return ['-c:v',encoder,'-q:v',str(quality)], 'q:v'
    if encoder.endswith('_vaapi'):
        return ['-vf','format=nv12,hwupload','-c:v',encoder,'-qp',str(quality)], 'qp'
    raise EncoderError('Unknown encoder: '+encoder)
