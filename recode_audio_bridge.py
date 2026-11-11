"""Read-only bridge between production stream optimizer and Test 100-10 audio policy.

No FFmpeg execution or mutation. Integration must consume this plan when both
normal recode and segmented cut command builders support individual audio outputs.
"""
from __future__ import annotations
from audio_plan import plan_audio, CODECS


def plan_selected_audio(metadata: dict, selected_indexes: list[int], options: dict,
                        audio_config: dict, processing_enabled: bool) -> list:
    streams = [s for s in metadata.get('streams', []) if s.get('codec_type') == 'audio'
               and s.get('index') in selected_indexes]
    if len(streams) != len(selected_indexes):
        raise ValueError('Optimizer selected an unavailable audio stream')
    return plan_audio(streams, audio_config, processing_enabled, encoders=set(CODECS))


def describe_audio_plan(plan: list) -> list[dict]:
    return [{'source_index': x.source_index, 'codec': x.codec, 'copy': x.copy,
             'default': x.default, 'purpose': x.purpose,
             'source_channels': x.source_channels, 'target_channels': x.target_channels,
             'channel_note': x.channel_note} for x in plan]
