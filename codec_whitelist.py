"""Shared MediaInfo/FFprobe codec aliases and wizard copy/recode decisions."""

def canonical_codec(value):
    name = str(value or '').strip().casefold().replace('_', '').replace('-', '').replace(' ', '')
    aliases = {
        'avc': 'h264', 'avc1': 'h264', 'h264': 'h264',
        'hevc': 'hevc', 'h265': 'hevc', 'hvc1': 'hevc',
        'eac3': 'eac3', 'enhancedac3': 'eac3', 'ec3': 'eac3',
        'ac3': 'ac3', 'aac': 'aac', 'mp3': 'mp3', 'mpeg1layer3': 'mp3',
        'dts': 'dts', 'flac': 'flac', 'opus': 'opus',
        'mpeg4avc': 'h264', 'mpeg h264': 'h264',
    }
    return aliases.get(name, name)


def permitted(codec, enabled, whitelist):
    return not enabled or canonical_codec(codec) in {canonical_codec(c) for c in whitelist}


def wizard_video_mode(codec, bitrate, threshold, video_settings):
    if not permitted(codec, video_settings.get('VideoWhitelistEnabled', False),
                     video_settings.get('VideoCodecWhitelist', [])):
        return 'recode'
    if bitrate <= 0 and video_settings.get('RecodeIfBitrateZero', False):
        return 'recode'
    return 'copy' if threshold > 0 and bitrate > 0 and bitrate <= threshold else 'recode'
