"""Media-Buddy V2: deterministic audio/subtitle selection, Test 73.

Only selects stream indexes. Never edits files or transcodes. Shared by recode/remux/wizards.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
import re

LANG_ALIASES = {"de":"deu","ger":"deu","deu":"deu","en":"eng","eng":"eng",
                "it":"ita","ita":"ita","fr":"fra","fre":"fra","fra":"fra",
                "es":"spa","spa":"spa","pt":"por","por":"por","ja":"jpn","jpn":"jpn",
                "nl":"nld","dut":"nld","nld":"nld","und":"und"}

def normalize_language(value):
    if not value: return 'und'
    key=str(value).lower().replace('_','-').split('-')[0]
    return LANG_ALIASES.get(key,key)

def _stream_lang(stream):
    tags=stream.get('tags') or {}
    return normalize_language(tags.get('language') or tags.get('LANGUAGE'))

def _forced(stream):
    # disposition is the authoritative flag; title is not a reliable substitute
    return bool((stream.get('disposition') or {}).get('forced'))

def _codec_rank(stream, priority):
    codec=str(stream.get('codec_name') or '').lower()
    profile=str(stream.get('profile') or '').lower()
    title=str((stream.get('tags') or {}).get('title') or '').lower()
    # Atmos is only claimed when explicit metadata says so, not based on E-AC-3/TrueHD alone.
    atmos=('atmos' in title or 'atmos' in profile)
    if atmos and codec=='truehd': kind='truehd_atmos'
    elif atmos and codec=='eac3': kind='eac3_atmos'
    else: kind=codec
    keys=[str(v).lower() for v in priority]
    return keys.index(kind) if kind in keys else len(keys)

def _channels(stream):
    try: return max(0,int(stream.get('channels') or 0))
    except (TypeError,ValueError): return 0

def _rank(stream, audio_options):
    codec=_codec_rank(stream,audio_options.get('CodecPriority',[]))
    channel=-_channels(stream)
    if audio_options.get('Priority','channels')=='codec': return (codec,channel,int(stream['index']))
    return (channel,codec,int(stream['index']))

def _languages(mapping,primary):
    result={}
    for language,count in mapping.items():
        lang=primary if language.lower()=='default' else normalize_language(language)
        if lang in result: raise ValueError(f'Duplicate language selection: {lang}')
        result[lang]=count
    return result

def select_streams(metadata,optimizer_config,metadata_language='deu'):
    """Return stream indexes and diagnostics. Preserves input order in final map.

    For >=2 audio streams: choose per-language counts; fallback only if no requested
    audio selected; if still none, choose best ranked source audio. One audio bypasses.
    Subtitle forced/normal quotas are independent and never fall back.
    """
    streams=metadata.get('streams',[])
    audios=[s for s in streams if s.get('codec_type')=='audio']
    subs=[s for s in streams if s.get('codec_type')=='subtitle']
    primary=normalize_language(metadata_language)
    if not isinstance(optimizer_config,dict): raise ValueError('Optimizer must be an object')
    enabled=optimizer_config.get('Enabled',True)
    if not isinstance(enabled,bool): raise ValueError('Optimizer.Enabled must be boolean')
    aopts=optimizer_config.get('Audio',{})
    sopts=optimizer_config.get('Subtitles',{})
    if not isinstance(aopts,dict) or not isinstance(sopts,dict): raise ValueError('Optimizer Audio/Subtitles must be objects')
    if aopts.get('Priority','channels') not in ('channels','codec'): raise ValueError('Audio.Priority must be channels or codec')
    amap=_languages(aopts.get('Languages',{'default':1}),primary)
    for lang,n in amap.items():
        if type(n) is not int or n<0: raise ValueError(f'Invalid audio count for {lang}')
    fallbacks=[normalize_language(x) for x in aopts.get('FallbackLanguages',['eng'])]
    submap=_languages(sopts.get('Languages',{'default':{'Normal':0,'Forced':1}}),primary)
    for lang,quota in submap.items():
        if not isinstance(quota,dict) or any(type(quota.get(k,0)) is not int or quota.get(k,0)<0 for k in ('Normal','Forced')):
            raise ValueError(f'Invalid subtitle count for {lang}')
    audio_reason='disabled' if not enabled else 'selected'
    subtitle_reason='disabled' if not enabled else 'selected'
    if not enabled:
        chosen_a=audios[:]; chosen_s=subs[:]
    else:
        if len(audios)<=1:
            chosen_a=audios[:]
            audio_reason='single_audio_bypass' if audios else 'no_audio'
        else:
            chosen_a=[]
            for lang,count in amap.items():
                matching=sorted((s for s in audios if _stream_lang(s)==lang),key=lambda s:_rank(s,aopts))
                chosen_a.extend(matching[:count])
            if not chosen_a:
                for lang in fallbacks:
                    matching=sorted((s for s in audios if _stream_lang(s)==lang),key=lambda s:_rank(s,aopts))
                    if matching:
                        chosen_a=matching[:max(1,int(aopts.get('FallbackCount',1)))]
                        audio_reason='language_fallback'
                        break
            if not chosen_a:
                chosen_a=[min(audios,key=lambda s:_rank(s,aopts))]
                audio_reason='emergency_fallback'
        if not subs:
            chosen_s=[]
            subtitle_reason='no_subtitles'
        else:
            chosen_s=[]
            for lang,quota in submap.items():
                matching=[s for s in subs if _stream_lang(s)==lang]
                for is_forced,limit in ((False,quota.get('Normal',0)),(True,quota.get('Forced',0))):
                    chosen_s.extend([s for s in matching if _forced(s)==is_forced][:limit])
    aset={int(s['index']) for s in chosen_a}
    sset={int(s['index']) for s in chosen_s}
    return {'audio_indexes':[int(s['index']) for s in audios if int(s['index']) in aset],
            'subtitle_indexes':[int(s['index']) for s in subs if int(s['index']) in sset],
            'audio_reason':audio_reason,'subtitle_reason':subtitle_reason,
            'primary_language':primary,
            'removed_audio_indexes':[int(s['index']) for s in audios if int(s['index']) not in aset],
            'removed_subtitle_indexes':[int(s['index']) for s in subs if int(s['index']) not in sset]}
