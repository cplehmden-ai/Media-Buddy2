"""Shared seven-step quality profiles for the quality tester and future recode integration.

Values for libx264/libx265/hevc_qsv come from the Test 96 matrix.
Other values are provisional and NOT perceptually calibrated.
"""
from dataclasses import dataclass

LABELS = ('Extrem kleine Datei', 'Sehr kleine Datei', 'Kleine Datei',
          'Ausgewogen', 'Hohe Qualität', 'Sehr hohe Qualität', 'Extrem hohe Qualität')

@dataclass(frozen=True)
class Profile:
    ffmpeg_name: str
    mode: str
    values: tuple
    preset: str = ''
    calibrated: bool = False

PROFILES = {
    'x264': Profile('libx264', 'crf', (35,32,29,26,23,20,17), 'medium', True),
    'x265': Profile('libx265', 'crf', (37,34,31,28,25,22,19), 'medium', True),
    'h264_qsv': Profile('h264_qsv', 'global_quality', (35,32,29,26,23,20,17)),
    'hevc_qsv': Profile('hevc_qsv', 'global_quality', (35,32,29,26,23,20,17), calibrated=True),
    'av1_qsv': Profile('av1_qsv', 'global_quality', (35,32,29,26,23,20,17)),
    'h264_nvenc': Profile('h264_nvenc', 'cq', (36,33,30,27,24,21,18), 'p5'),
    'hevc_nvenc': Profile('hevc_nvenc', 'cq', (36,33,30,27,24,21,18), 'p5'),
    'av1_nvenc': Profile('av1_nvenc', 'cq', (36,33,30,27,24,21,18), 'p5'),
    'h264_amf': Profile('h264_amf', 'qp', (36,33,30,27,24,21,18)),
    'hevc_amf': Profile('hevc_amf', 'qp', (36,33,30,27,24,21,18)),
    'av1_amf': Profile('av1_amf', 'qp', (36,33,30,27,24,21,18)),
    'svt_av1': Profile('libsvtav1', 'crf', (48,44,40,36,32,28,24), '6'),
}


def codec_args(name, level, bit_depth):
    """Return an encoder-specific FFmpeg argv fragment; no shell interpolation."""
    p = PROFILES[name]
    value = p.values[level - 1]
    args = ['-c:v', p.ffmpeg_name]
    if p.preset:
        args += ['-preset', p.preset]
    if p.mode == 'crf':
        args += ['-crf', str(value)]
    elif p.mode == 'global_quality':
        args += ['-global_quality', str(value)]
    elif p.mode == 'cq':
        args += ['-rc', 'vbr', '-cq', str(value), '-b:v', '0']
    elif p.mode == 'qp':
        # AMF constant-QP controls are per frame type; all values provisional.
        args += ['-rc', 'cqp', '-qp_i', str(value), '-qp_p', str(value)]
    # H.264 10-bit is not broadly hardware-decodable, and HW H264 encoders typically only accept 8-bit.
    is_h264 = name == 'x264' or name.startswith('h264_')
    depth = 8 if is_h264 else bit_depth
    if name.endswith('_qsv'):
        pix = 'p010le' if depth == 10 else 'nv12'
    elif name.endswith('_nvenc') or name.endswith('_amf'):
        pix = 'p010le' if depth == 10 else 'yuv420p'
    else:
        pix = 'yuv420p10le' if depth == 10 else 'yuv420p'
    args += ['-pix_fmt', pix]
    return args, value, depth
