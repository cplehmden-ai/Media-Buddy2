"""Test 100-4: read-only audio policy planner with configurable original protection.

No stream is changed here. Engine must follow output.copy, not the UI 'copy' mode.
"""
from __future__ import annotations
from dataclasses import dataclass
import re
from codec_whitelist import canonical_codec

CODECS = {'ac3', 'eac3', 'aac', 'mp3', 'flac', 'opus', 'dts'}
PRESERVABLE = {'truehd_atmos', 'eac3_atmos', 'truehd', 'dtsx', 'dts_hd_ma', 'auro3d'}
ENCODER_NAMES = {'dts': 'dca'}
PROTECTION_MODES = {'off', 'additional', 'original_only'}
# Conservative codec constraints for FFmpeg native encoders; unknown layouts must be checked at execution.
MAX_CHANNELS = {'ac3': 6, 'eac3': 6, 'mp3': 2, 'dts': 6}

@dataclass(frozen=True)
class AudioOutput:
    source_index: int
    codec: str
    copy: bool
    default: bool
    purpose: str
    reason: str = ''
    source_channels: int | None = None
    target_channels: int | None = None
    channel_note: str = ''


def classify(stream: dict) -> tuple[str, bool]:
    codec = str(stream.get('codec_name') or '').lower()
    profile = str(stream.get('profile') or '').lower()
    tags = stream.get('tags') or {}
    title = str(tags.get('title') or '').lower()
    hint = f'{profile} {title}'
    if 'auro-3d' in hint or 'auro 3d' in hint or 'auro3d' in hint:
        return 'auro3d', True
    if codec == 'truehd':
        return ('truehd_atmos', False) if 'atmos' in hint else ('truehd', True)
    if codec == 'eac3':
        return ('eac3_atmos', False) if 'atmos' in hint else ('eac3', True)
    if codec == 'dts':
        if 'dts:x' in hint or 'dts-x' in hint or 'dts x' in hint:
            return 'dtsx', False
        if 'dts-hd ma' in hint or 'master audio' in hint:
            return 'dts_hd_ma', True
        return 'dts', True
    return codec, False


def channel_details(stream: dict) -> tuple[int | None, int | None, int | None, str]:
    """Return total, main, LFE, note. Never guess LFE from a bare channel count."""
    try:
        total = int(stream.get('channels'))
        if total < 1: total = None
    except (TypeError, ValueError):
        total = None
    layout = str(stream.get('ch_layout') or stream.get('channel_layout') or '').strip()
    if not layout or layout.lower() in ('unknown', 'unspecified'):
        return total, None, None, 'channel layout unknown; main/LFE protection not inferred'
    # ffprobe legacy layouts: 5.1(side), 7.1, 5.1.2, 5.2, etc.
    m = re.fullmatch(r'(\d+)\.(\d+)(?:\.(\d+))?(?:\([^)]*\))?', layout)
    if m:
        main = int(m.group(1)) + int(m.group(3) or 0)
        lfe = int(m.group(2))
        if total is not None and main + lfe != total:
            return total, None, None, f'layout/count mismatch: {layout}'
        return total, main, lfe, layout
    # Explicit FFmpeg native channel names, separated by '+'.
    if '+' in layout:
        names = [x.strip().upper() for x in layout.split('+')]
        if all(re.fullmatch(r'[A-Z][A-Z0-9]*', x) for x in names):
            lfe = sum(x.startswith('LFE') for x in names)
            if total is None or len(names) == total:
                return total or len(names), len(names) - lfe, lfe, layout
    return total, None, None, f'unrecognized channel layout: {layout}'


def plan_audio(streams: list[dict], config: dict, processing_enabled: bool,
               encoders: set[str] | None = None) -> list[AudioOutput]:
    preferred = str(config.get('AudioCodec', 'copy')).lower()
    fallback = str(config.get('FallbackCodec', 'eac3')).lower()
    keep = set(config.get('PreserveFormats', []))
    mode = str(config.get('ProtectionMode', 'additional')).lower()
    protect_surround = config.get('ProtectSurroundAbove51', False)
    protect_lossless = config.get('ProtectLossless', False)
    whitelist_on = config.get('WhitelistEnabled', False)
    whitelist = {canonical_codec(c) for c in config.get('CodecWhitelist', [])}
    blacklist = {canonical_codec(c) for c in config.get('CodecBlacklist', [])}
    daily_default = config.get('EverydayDefault', True)
    if preferred != 'copy' and preferred not in CODECS: raise ValueError('Invalid AudioCodec')
    if fallback not in CODECS: raise ValueError('Invalid FallbackCodec')
    if not keep <= PRESERVABLE: raise ValueError('Invalid PreserveFormats')
    if mode not in PROTECTION_MODES: raise ValueError('Invalid ProtectionMode')
    if any(not isinstance(x, bool) for x in (whitelist_on, daily_default, protect_surround, protect_lossless)):
        raise ValueError('Protection/whitelist/default switches must be boolean')
    if encoders is None: encoders = CODECS.copy()  # preview only
    def allowed(codec: str) -> bool:
        return canonical_codec(codec) not in blacklist and (not whitelist_on or canonical_codec(codec) in whitelist)
    outputs: list[AudioOutput] = []
    for stream in streams:
        index = int(stream['index'])
        codec = str(stream.get('codec_name') or '').lower()
        kind, uncertain = classify(stream)
        total, main, lfe, layout_note = channel_details(stream)
        protect_reasons = []
        if kind in keep: protect_reasons.append(f'format={kind}')
        if protect_surround and main is not None and main > 5:
            protect_reasons.append(f'{main} main channels ({lfe} LFE)')
        if protect_lossless and (codec in ('flac', 'truehd', 'alac', 'wavpack', 'ape', 'pcm_s16le', 'pcm_s24le', 'pcm_s32le') or kind == 'dts_hd_ma' or kind == 'dtsx'):
            protect_reasons.append('lossless source format')
        protected = mode != 'off' and bool(protect_reasons)
        if mode == 'original_only' and protected:
            if codec in blacklist:
                raise ValueError(f'Stream #{index}: protected original codec {codec} explicitly blacklisted')
            outputs.append(AudioOutput(index, 'copy', True, False, 'original',
                'original-only protection: ' + ', '.join(protect_reasons), total, total, layout_note))
            continue
        desired = codec if preferred == 'copy' else preferred
        need_encode = processing_enabled or preferred != 'copy' or not allowed(codec)
        if not need_encode:
            outputs.append(AudioOutput(index, 'copy', True, False, 'original', 'no processing required', total, total, layout_note))
            continue
        if desired not in CODECS or desired not in encoders or not allowed(desired):
            if not allowed(fallback):
                raise ValueError(f'Stream #{index}: fallback codec {fallback} forbidden by codec rules (fallback required)')
            if fallback not in encoders:
                raise ValueError(f'Stream #{index}: fallback codec {fallback} encoder unavailable (fallback required)')
            desired = fallback
            reason = f'fallback: {codec} cannot be encoded or is disallowed; target={fallback}'
        else:
            cause = ('audio filters enabled' if processing_enabled else
                     'explicit fixed codec selection' if preferred != 'copy' else
                     'codec restriction')
            reason = f'encode source codec {codec} as {desired}: {cause}'
        if uncertain: reason += f'; immersive metadata uncertain ({kind})'
        max_channels = MAX_CHANNELS.get(desired)
        target = total
        channel_note = layout_note
        if total is not None and max_channels is not None and total > max_channels:
            target = max_channels
            channel_note = f'REQUIRES DOWNMIX: {total} -> {target} channels; explicit layout/mix validation before encoding'
        if protected and mode == 'additional' and codec in blacklist:
            raise ValueError(f'Stream #{index}: protected original codec {codec} explicitly blacklisted')
        outputs.append(AudioOutput(index, desired, False, False,
            'everyday' if protected and mode == 'additional' else 'processed',
            reason, total, target, channel_note))
        if protected and mode == 'additional':
            outputs.append(AudioOutput(index, 'copy', True, False, 'original',
                'original protection: ' + ', '.join(protect_reasons), total, total, layout_note))
    if outputs:
        default_idx = 0 if daily_default else next((i for i, o in enumerate(outputs) if o.purpose == 'original'), 0)
        outputs = [AudioOutput(o.source_index, o.codec, o.copy, i == default_idx,
                              o.purpose, o.reason, o.source_channels, o.target_channels, o.channel_note)
                   for i, o in enumerate(outputs)]
    return outputs
