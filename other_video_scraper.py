"""Test 138: independent other-video metadata/artwork generator.
Original videos are READ ONLY. External outputs are never silently overwritten.
"""
from __future__ import annotations
from linked_media_walk import linked_files
import json, random, subprocess, tempfile, math, os
from pathlib import Path
from datetime import datetime
import xml.etree.ElementTree as ET
from tool_manager import find_tool

VIDEO_EXT={'.mkv','.mp4','.avi','.mov','.m4v','.ts','.m2ts','.wmv','.webm','.mpg','.mpeg'}
ROOT=Path(__file__).resolve().parent

def tool(name):
    found, reason=find_tool(name)
    if not found:raise RuntimeError(f'{name} nicht gefunden: {reason}')
    return found

def run(args):
    p=subprocess.run([str(x) for x in args],capture_output=True,text=True,errors='replace',stdin=subprocess.DEVNULL)
    if p.returncode:raise RuntimeError(f'{Path(str(args[0])).name}: {p.stderr[-1200:]}')
    return p.stdout

def metadata(path):
    data=json.loads(run([tool('ffprobe'),'-v','error','-show_format','-show_streams','-of','json',path]))
    streams=data.get('streams',[])
    v=next((x for x in streams if x.get('codec_type')=='video'),None)
    if not v:raise RuntimeError('Keine Videospur gefunden')
    a=next((x for x in streams if x.get('codec_type')=='audio'),{})
    dur=float(data.get('format',{}).get('duration') or v.get('duration') or 0)
    if dur<=0:raise RuntimeError('Videolaufzeit unbekannt')
    rate=v.get('avg_frame_rate') or v.get('r_frame_rate') or '0/1'
    try: fps=round(float(rate.split('/')[0])/float(rate.split('/')[1]),3)
    except (ValueError,ZeroDivisionError,IndexError):fps=0
    ratio=v.get('display_aspect_ratio','')
    if not ratio:
        sar=v.get('sample_aspect_ratio','1:1').split(':')
        try:ratio=f'{round(v["width"]*int(sar[0])/int(sar[1]))}:{v["height"]}'
        except (KeyError,ValueError,ZeroDivisionError,IndexError):ratio=''
    return dict(duration=dur,width=v.get('width',0),height=v.get('height',0),fps=fps,ratio=ratio,
                codec=v.get('codec_name','unknown'),audio_codec=a.get('codec_name','unknown'),
                channels=a.get('channels',0),language=a.get('tags',{}).get('language','und'))

def magick(*args):run([tool('magick'),*args])

def images_for(path, meta, work, rng):
    count=rng.randint(10,20)
    shots=[]
    for i in range(1,count+1):
        timestamp=meta['duration']*i/(count+1)
        dst=work/f'still_{i:02}.jpg'
        try:run([tool('ffmpeg'),'-hide_banner','-loglevel','error','-ss',f'{timestamp:.3f}','-i',path,'-frames:v','1','-q:v','3','-y',dst])
        except RuntimeError:continue
        if dst.exists() and dst.stat().st_size>1000:shots.append(dst)
    if not shots:raise RuntimeError('Keine Standbilder extrahiert')
    rng.shuffle(shots)
    return shots

def fit_image(src,dst,w,h,cover=False):
    # cover=True is only for decorative backgrounds. Visible screenshots never stretch.
    if cover:
        magick(src,'-auto-orient','-resize',f'{w}x{h}^','-gravity','center','-extent',f'{w}x{h}',dst)
    else:
        magick(src,'-auto-orient','-resize',f'{w}x{h}','-background','#172636','-gravity','center','-extent',f'{w}x{h}',dst)

def backdrop_base(shots,work,template):
    dst=work/'back_base.png'
    if template:
        fit_image(template,dst,1920,1080,True)
    else:
        fit_image(shots[-1],dst,1920,1080,True)
        magick(dst,'-blur','0x9',dst)
    return dst

def cover_base(shots,work,template):
    dst=work/'cover_base.png'
    if template:fit_image(template,dst,1000,1500,True)
    else:
        fit_image(shots[-1],dst,1000,1500,True)
        magick(dst,'-blur','0x11',dst)
    return dst

def layer(src,work,name,w,h,angle=0,border=None):
    dst=work/f'{name}.png'
    # Collage tiles may be cropped at their edges, but must never stretch or show bars.
    fit_image(src,dst,w,h,True)
    # Configuration values are strings; convert to Path before checking existence.
    border = Path(border) if border else None
    if border and border.is_file():
        # Border templates are transparent PNG overlays, not decorative backgrounds.
        # Resizing via fit_image(..., cover=True) uses -extent, whose default
        # background turns transparent pixels opaque black in ImageMagick.
        # Scale the complete frame to the tile size and preserve its alpha.
        overlay=work/f'{name}_border.png'
        magick(border,'-auto-orient','-resize',f'{w}x{h}!',overlay)
        magick(dst,overlay,'-compose','over','-composite',dst)
    else:magick(dst,'-bordercolor','#e2e8ef','-border','7',dst)
    if angle:magick(dst,'-background','none','-rotate',str(angle),dst)
    return dst

def compose(base,overlays,dest):
    args=[str(base)]
    for image,x,y in overlays:
        args.extend(['(',str(image),')','-gravity','northwest','-geometry',f'+{x}+{y}','-compose','over','-composite'])
    args.append(str(dest));magick(*args)

def font_args(font):return ['-font',font] if font else []

def text_overlay(base,text,x,y,size,font,gravity='northwest'):
    # text is an argv item, not a shell fragment: special chars are safe.
    magick(base,*font_args(font),'-gravity',gravity,'-fill','white','-stroke','#13202c','-strokewidth','2',
           '-pointsize',str(size),'-annotate',f'+{x}+{y}',text,'-stroke','none','-annotate',f'+{x}+{y}',text,base)

def logo_path(name):
    """Return an existing artwork, or None for a dynamic template fallback."""
    d=ROOT/'Templates'/'Logos'
    for p in (d/f'{name}.png',d/f'{name.upper()}.png',d/f'{name.lower()}.png'):
        if p.is_file():return p
    return None

def ratio_logo(meta):
    try:
        x,y=(float(v) for v in meta['ratio'].split(':'));r=x/y
    except (ValueError,ZeroDivisionError,AttributeError):
        r=meta['width']/max(1,meta['height'])
    if 1.65<=r<=1.9:return '16_9'
    if 1.25<=r<=1.45:return '4_3'
    if 0.53<=r<=0.59:return '9:16'
    if 0.72<=r<=0.78:return '3:4'
    if 2.25<=r<=2.45:return '21:9'
    return meta.get('ratio') or f'{meta["width"]}:{meta["height"]}'

def language_logo(code):
    code=str(code or 'und').lower().strip()
    known={'de':'De','deu':'De','ger':'De','en':'En','eng':'En',
           'fr':'French','fra':'French','es':'Spanish','spa':'Spanish',
           'it':'Italian','ita':'Italian'}
    return known.get(code,code if code not in ('unknown','') else 'und')

def dynamic_logo(name,work,index):
    """Render missing logos on the V1 blank logo template, never alter it."""
    template=ROOT/'Templates'/'Logos'/'template.png'
    if not template.is_file():
        raise RuntimeError(f'Logo-Vorlage fehlt: {template}')
    label=str(name or 'und').strip()
    if len(label)>15:label=label[:15]
    pointsize=34 if len(label)<=4 else 27 if len(label)<=7 else 20
    out=work/f'generated_logo_{index}.png'
    magick(template,'-resize','150x90!','-gravity','center',
           '-font','DejaVu-Sans-Bold','-fill','white',
           '-stroke','#1b2228','-strokewidth','1',
           '-pointsize',str(pointsize),'-annotate','+0+0',label,out)
    return out

def video_logo(codec):
    return {'hevc':'H265','h264':'H264','mpeg4':'MPEG-4 Visual','vc1':'vc-1'}.get(str(codec).lower(),str(codec).upper())

def audio_logo(codec):
    return {'aac':'AAC','ac3':'AC-3','eac3':'E-AC-3','dts':'DTS','flac':'FLAC','mp3':'MPEG Audio'}.get(str(codec).lower(),str(codec).upper())

def poster_title_lines(title):
    """Wrap the title to two lines at word boundaries when possible."""
    import textwrap
    title=' '.join(str(title).split())
    if len(title)<=27:
        return title, 68
    lines=textwrap.wrap(title,width=28,break_long_words=False,break_on_hyphens=False)
    if len(lines)>2:
        # Prefer keeping the full title over truncating it. Fit long titles smaller.
        lines=textwrap.wrap(title,width=max(28,math.ceil(len(title)/2)),
                            break_long_words=False,break_on_hyphens=False)
    if len(lines)>2:
        return '\n'.join(lines), 38
    return '\n'.join(lines), 54 if max(map(len,lines))<=31 else 46


def poster(shots,meta,title,year,dst,work,options):
    """Test 138d: top-anchored frames; two side-by-side for portrait videos."""
    base=cover_base(shots,work,options.get('cover_template'))
    count=max(1,min(2,int(options.get('poster_count',1))))
    try:
        rx,ry=(float(v) for v in meta['ratio'].split(':'))
        aspect=rx/ry
    except (ValueError,ZeroDivisionError,AttributeError):
        aspect=meta['width']/max(1,meta['height'])
    if not (0.25<=aspect<=5):
        aspect=meta['width']/max(1,meta['height'])

    # Fixed title and footer protection zones. The top edge of the first
    # screenshot is identical for every aspect ratio.
    top=260
    bottom=1180
    available_h=bottom-top
    border_px=14  # layer() adds a 7px border on every side
    layouts=[]
    if aspect<0.85:
        # Two distinct portrait frames use the width without cropping.
        gap=24
        max_w=(940-gap)//2-border_px
        max_h=available_h-border_px
        w=min(max_w,round(max_h*aspect))
        h=min(max_h,round(w/aspect))
        total=2*(w+border_px)+gap
        x=(1000-total)//2
        layouts=[(x,top,w,h),(x+w+border_px+gap,top,w,h)]
    elif count==1:
        max_w=920-border_px
        max_h=available_h-border_px
        w=min(max_w,round(max_h*aspect))
        h=min(max_h,round(w/aspect))
        layouts=[((1000-w-border_px)//2,top,w,h)]
    else:
        # Two landscape/4:3 frames stacked, both within the image zone.
        gap=25
        max_h=(available_h-gap)//2-border_px
        max_w=920-border_px
        w=min(max_w,round(max_h*aspect))
        h=min(max_h,round(w/aspect))
        layouts=[((1000-w-border_px)//2,top,w,h),
                 ((1000-w-border_px)//2,top+h+border_px+gap,w,h)]

    if len(shots)<len(layouts):
        raise RuntimeError('Zu wenige unterschiedliche Standbilder für das Poster')
    overlays=[]
    border=options.get('border_template')
    for i,(x,y,w,h) in enumerate(layouts):
        image=layer(shots[i],work,f'poster_{i}',w,h,0,border)
        overlays.append((image,x,y))
    compose(base,overlays,base)

    title_lines,title_size=poster_title_lines(title)
    # Bold font is used unless the user explicitly selected a font.
    font=options.get('font') or 'DejaVu-Sans-Bold'
    text_overlay(base,title_lines,0,42,title_size,font,'north')
    mm=int(meta['duration']//60)
    duration_seconds=int(round(meta['duration']))
    duration_label=(f'{duration_seconds//3600:02d}:{(duration_seconds//60)%60:02d}:{duration_seconds%60:02d}'
                    if duration_seconds>=3600 else
                    f'{duration_seconds//60:02d}:{duration_seconds%60:02d}')
    tech=f'{duration_label}  •  {meta["width"]}×{meta["height"]}  •  {meta["fps"]:g} fps  •  {year}'
    text_overlay(base,tech,0,1285,30,options.get('font'),'north')
    labels=[video_logo(meta['codec']),ratio_logo(meta),language_logo(meta['language']),
            f'{meta["channels"]} channels',audio_logo(meta['audio_codec'])]
    overlay=[]
    for i,label in enumerate(labels):
        icon=logo_path(label)
        if icon and icon.is_file():
            img=work/f'logo_{i}.png'
            magick(icon,'-resize','140x85','-background','none','-gravity','center',
                   '-extent','150x90',img)
        else:
            img=dynamic_logo(label,work,i)
        overlay.append((img,105+i*160,1350))
    if overlay:compose(base,overlay,base)
    magick(base,'-quality','91',dst)

def fanart(shots,dst,work,options,meta=None):
    mode=int(options.get('fanart_count',6))
    # Portrait sources: keep the actual portrait aspect of every visible still.
    # The final artwork remains 1920x1080; landscape sources keep the old layouts.
    if meta and meta.get('height',0)>meta.get('width',0) and mode>=2:
        aspect=meta['width']/meta['height']
        try:
            rx,ry=(float(v) for v in str(meta.get('ratio','')).split(':'))
            if rx>0 and ry>0: aspect=rx/ry
        except (ValueError,ZeroDivisionError,AttributeError):
            pass
        if not (0.25<=aspect<1):
            aspect=meta['width']/meta['height']
        if not (0<aspect<1): aspect=9/16
        if mode>len(shots):
            raise RuntimeError(f'Zu wenige Standbilder für Fanart ({len(shots)} statt {mode})')
        base=backdrop_base(shots,work,options.get('backdrop_template'))
        border=options.get('border_template')
        # Test 141a: lively, upper-anchored portrait collage. Keep the original
        # aspect ratio, but stagger and rotate frames as in landscape collages.
        # Rotation expands the bounding box, so reserve space on all sides.
        # The slight overlaps deliberately vary with the number of frames.
        angles={2:[-6,6],3:[-7,4,7],4:[-7,5,-4,8],
                5:[-8,5,-3,7,-6],6:[-8,5,-4,7,-6,9]}
        offsets={2:[0,50],3:[15,75,0],4:[30,0,85,45],
                 5:[35,85,0,70,20],6:[45,0,75,20,95,35]}
        # Use the entire upper section, leaving visible blurred background
        # underneath; never squash or stretch the source frames.
        max_height={2:790,3:725,4:650,5:585,6:530}.get(mode,530)
        # Account for each rotated tile's enlarged canvas and frame overlap.
        max_angle=max(abs(a) for a in angles[mode])
        rotation_pad=math.ceil(max_height*math.sin(math.radians(max_angle)))+20
        usable_width=1920-120
        overlap_ratio=0.14 if mode>=4 else 0.09
        tile_h=max_height
        tile_w=round(tile_h*aspect)
        while (mode*tile_w-(mode-1)*round(tile_w*overlap_ratio)
               +2*rotation_pad)>usable_width:
            tile_h-=5
            tile_w=max(1,round(tile_h*aspect))
        step=tile_w-round(tile_w*overlap_ratio)
        total_width=tile_w+(mode-1)*step+2*rotation_pad
        left=(1920-total_width)//2+rotation_pad
        # The image's rotated bounding box extends above and to the sides.
        top=85+rotation_pad//2
        overlays=[]
        for i in range(mode):
            im=layer(shots[i],work,f'fanart_portrait_{i}',tile_w,tile_h,
                     angles[mode][i],border)
            # ImageMagick rotates around the center, expanding the PNG canvas.
            overlays.append((im,left+i*step-rotation_pad//2,
                             top+offsets[mode][i]-rotation_pad//2))
        compose(base,overlays,work/'fanart_done.png')
        magick(work/'fanart_done.png','-quality','91',dst)
        return
    if mode==1:
        base=backdrop_base(shots,work,None)
        main=work/'single.png';fit_image(shots[0],main,1920,1080)
        # Full un-cropped screenshot, blurred fill for non-16:9 formats.
        # Remove solid-color fit bars by making main image transparent outside the scaled area.
        magick(shots[0],'-auto-orient','-resize','1920x1080',main)
        magick(base,main,'-gravity','center','-compose','over','-composite','-quality','91',dst)
        return
    base=backdrop_base(shots,work,options.get('backdrop_template'))
    # Normalized positions: four/five-image layouts emphasize the upper part.
    positions={2:[(115,120,760,440,-5),(1000,115,760,440,5)],
      3:[(100,95,700,380,-3),(1100,100,700,380,3),(610,490,700,380,0)],
      4:[(90,80,770,390,-5),(1040,85,770,390,5),(170,500,750,370,4),(1010,505,750,370,-5)],
      5:[(75,70,565,345,-5),(675,65,565,345,3),(1275,70,565,345,5),(345,470,630,355,4),(1010,475,630,355,-4)],
      6:[(160,35,570,335,-7),(680,35,570,335,0),(1230,40,570,335,7),(125,375,570,335,8),(690,380,570,335,-3),(1250,370,570,335,17)]}
    overlays=[]
    border=options.get('border_template')
    for i,(x,y,w,h,a) in enumerate(positions[mode]):
        im=layer(shots[i],work,f'fanart_{i}',w,h,a,border)
        overlays.append((im,x,y))
    compose(base,overlays,work/'fanart_done.png')
    magick(work/'fanart_done.png','-quality','91',dst)

def write_nfo(path,title,year,genre,plot,mpaa):
    root=ET.Element('movie')
    for key,value in [('title',title),('originaltitle',title),('sorttitle',title),('year',year),('genre',genre),('mpaa',mpaa),('plot',plot)]:
        ET.SubElement(root,key).text=str(value or '')
    ET.indent(root,space='    ')
    stamp=datetime.now().astimezone().isoformat(timespec='seconds')
    xml=ET.tostring(root,encoding='unicode')
    path.write_text(f'<?xml version="1.0" encoding="utf-8"?>\n<!-- Created by Media-Buddy V2 Test 138 on {stamp} -->\n{xml}\n',encoding='utf-8')

def process(path,source,output,options=None,log=None):
    options=options or {};path=Path(path);source=Path(source);output=Path(output)
    log=log or (lambda msg:None)
    if not path.is_file():raise FileNotFoundError(path)
    meta=metadata(path);title=path.stem
    year=str(datetime.fromtimestamp(path.stat().st_mtime).year)
    genre=path.parent.name if options.get('genre_from_folder',True) and path.parent!=source else options.get('genre','')
    minutes=int(meta['duration']//60)
    tech=f'Video: {meta["codec"]}, {meta["width"]}x{meta["height"]}, {meta["fps"]:g} fps\nAudio: {meta["audio_codec"]}, {meta["channels"]} Kanäle, {meta["language"]}\nLaufzeit: {minutes} Minuten'
    plot=options.get('plot','').strip()
    if options.get('mediainfo_plot',True):plot=(plot+'\n\n' if plot else '')+tech
    dest=output/path.relative_to(source).parent
    dest.mkdir(parents=True,exist_ok=True)
    targets=[]
    if options.get('make_poster',True):targets.append(('Poster',dest/f'{title}-poster.jpg'))
    if options.get('make_fanart',True):targets.append(('Fanart',dest/f'{title}-fanart.jpg'))
    if options.get('make_nfo',True):targets.append(('NFO',dest/f'{title}.nfo'))
    # Existing files remain untouched, even when the user runs the batch twice.
    pending=[(name,p) for name,p in targets if not p.exists()]
    if not pending:
        log(f'Übersprungen (alle Ziele vorhanden): {path.name}')
        return 'skipped'
    log(f'Bearbeite: {path.name}')
    if any(name in ('Poster','Fanart') for name,_ in pending):
        with tempfile.TemporaryDirectory(prefix='mb138_') as tmp:
            work=Path(tmp);rng=random.Random();shots=images_for(path,meta,work,rng)
            # Reserve disjoint foreground stills for poster and fanart. A separate
            # background frame is selected when possible. Avoid repeating images.
            pc=(max(2,int(options.get('poster_count',1))) if
                (meta['height']>meta['width'] and any(n=='Poster' for n,_ in pending))
                else int(options.get('poster_count',1))) if any(n=='Poster' for n,_ in pending) else 0
            fc=int(options.get('fanart_count',6)) if any(n=='Fanart' for n,_ in pending) else 0
            needed=pc+fc
            if len(shots)<needed:
                raise RuntimeError(f'Zu wenige Standbilder ({len(shots)}) für {needed} unterschiedliche Motive')
            poster_shots=shots[:pc]
            fanart_shots=shots[pc:pc+fc]
            background_shots=shots[needed:] or shots
            for name,dst in pending:
                if name=='Poster':poster(poster_shots+background_shots,meta,title,year,dst,work,options);log(f'Poster: {dst.name}')
                if name=='Fanart':fanart(fanart_shots+background_shots,dst,work,options,meta);log(f'Fanart: {dst.name}')
    for name,dst in pending:
        if name=='NFO':write_nfo(dst,title,year,genre,plot,options.get('mpaa',''));log(f'NFO: {dst.name}')
    return 'ok'

def batch(source,output,options=None,log=None,should_stop=None):
    source=Path(source).resolve();output=Path(output).resolve()
    if not source.is_dir():raise ValueError('Quellordner fehlt')
    if source!=output:
        raise ValueError('Metadaten-Steuermodul erwartet denselben Videoordner für Einlesen und Ausgabe')
    files=sorted((p for p in linked_files(source) if p.is_file() and p.suffix.lower() in VIDEO_EXT),key=lambda p:str(p).casefold())
    counts={'ok':0,'skipped':0,'error':0}
    for index,p in enumerate(files,1):
        if should_stop and should_stop():break
        if log:log(f'[{index}/{len(files)}] {p.name}')
        try:result=process(p,source,output,options,log)
        except Exception as e:
            counts['error']+=1
            if log:log(f'FEHLER {p.name}: {e}')
        else:counts[result]+=1
    return counts
