"""Lyrics sanitation and optional embedding for metadata-stage audio files."""
from __future__ import annotations
import html
from html.parser import HTMLParser
import re
from pathlib import Path

class _Plain(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out=[]; self.block=0
    def handle_starttag(self, tag, attrs):
        if tag.lower() in ('script','style','iframe','object','svg'):
            self.block+=1
        elif not self.block and tag.lower() in ('br','p','div','li'):
            self.out.append('\n')
    def handle_endtag(self, tag):
        if tag.lower() in ('script','style','iframe','object','svg'):
            self.block=max(0,self.block-1)
        elif not self.block and tag.lower() in ('p','div','li'):
            self.out.append('\n')
    def handle_data(self, data):
        if not self.block:self.out.append(data)
    def handle_entityref(self, name):
        if not self.block:self.out.append(html.unescape('&'+name+';'))
    def handle_charref(self, name):
        if not self.block:self.out.append(html.unescape('&#'+name+';'))

def sanitize(value):
    value=str(value).replace('\r\n','\n').replace('\r','\n')
    value=re.sub(r'(?is)<!--.*?-->','',value)
    value=re.sub(r'(?is)<\s*(?:script|style|iframe|object|svg)\b[^>]*>.*?<\s*/\s*(?:script|style|iframe|object|svg)\s*>','',value)
    if re.search(r'(?i)</?[a-z][a-z0-9]*(?:\s[^<>]*)?/?>',value):
        parser=_Plain();parser.feed(value);parser.close();value=''.join(parser.out)
    value=html.unescape(value)
    value=re.sub(r'\n{4,}','\n\n\n',value)
    return value.strip('\n')+'\n' if value.strip() else ''

def _embedded(audio):
    from mutagen.id3 import ID3
    from mutagen.mp4 import MP4
    if isinstance(audio.tags, ID3):
        frames=audio.tags.getall('USLT')
        return str(frames[0].text) if frames else ''
    if isinstance(audio,MP4):
        return str((audio.get('\xa9lyr') or [''])[0])
    tags=audio.tags
    if tags is not None:
        for key in ('LYRICS','UNSYNCEDLYRICS','SYNCEDLYRICS'):
            vals=tags.get(key,[]) if hasattr(tags,'get') else []
            if vals:return str(vals[0])
    return ''

def _put(audio, value):
    from mutagen.id3 import ID3, USLT
    from mutagen.mp4 import MP4
    if audio.tags is None:audio.add_tags()
    if isinstance(audio.tags,ID3):
        audio.tags.delall('USLT');audio.tags.add(USLT(encoding=3,lang='eng',desc='',text=value))
    elif isinstance(audio,MP4):
        audio['\xa9lyr']=[value]
    elif hasattr(audio.tags,'__setitem__'):
        audio.tags['LYRICS']=[value]
    else:
        raise ValueError('No supported lyrics tag interface')

def process(path, opts):
    if not (opts.get('embed_lyrics') or opts.get('clean_lyrics')):return
    try:
        from mutagen import File
        audio=File(str(path),easy=False)
        if audio is None:raise ValueError('Unsupported audio container')
        existing=_embedded(audio)
        lrc=path.with_suffix('.lrc'); txt=path.with_suffix('.txt')
        sidecar=next((p for p in (lrc,txt) if p.is_file()),None)
        raw=sidecar.read_text(encoding='utf-8-sig') if sidecar else existing
        clean=sanitize(raw) if raw else ''
        if sidecar and clean and clean!=raw:
            sidecar.write_text(clean,encoding='utf-8')
            print('[LYRICS CLEAN] External:',sidecar,flush=True)
        if not opts.get('embed_lyrics') and not opts.get('clean_lyrics'):
            return
        # An existing embedded lyric is never replaced by a different external text.
        target=sanitize(existing) if existing else (clean if opts.get('embed_lyrics') else '')
        if target and target!=existing:
            _put(audio,target);audio.save()
            print('[LYRICS TAG] Updated:',path.name,flush=True)
    except Exception as exc:
        print('[WARN] Lyrics tag processing failed:',path,exc,flush=True)
