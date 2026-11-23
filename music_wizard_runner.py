"""Music wizard: safe copy/transcode/CUE, then scrape the entire destination."""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from music_recode_controller import EXTENSIONS
from music_recode_engine import PROFILES, recode_file
from music_cue import find_cue_for_image, safe_track_name
from tool_manager import get_tool
from activity_log import event as activity_event
from original_cleanup import start_run, add_success

LOSSLESS = {'flac','alac','ape','wavpack','wv','pcm_s16le','pcm_s24le','pcm_s32le','pcm_f32le','pcm_f64le','tta','tak','shorten','truehd','mlp'}

def _codec(ffprobe, path):
    result = subprocess.run([str(ffprobe), '-v','error','-select_streams','a:0',
        '-show_entries','stream=codec_name','-of','json',str(path)],capture_output=True,
        text=True,encoding='utf-8',errors='replace',stdin=subprocess.DEVNULL)
    if result.returncode:
        raise RuntimeError(f'FFprobe: {result.stderr[-600:]}')
    streams=json.loads(result.stdout).get('streams',[])
    if not streams or not streams[0].get('codec_name'):
        raise RuntimeError('No audio stream detected')
    return streams[0]['codec_name'].lower()

def _scrape(settings, target, update, stop_after_file, stop_immediately):
    opts=dict(settings.get('scraper_options',{}))
    opts['source']=str(target)
    activity_event('Music wizard scraper source (wizard output): '+str(target), 'INFO')
    with tempfile.TemporaryDirectory(prefix='mb_music_wizard_') as temp:
        marker=Path(temp)/'stop'
        env=os.environ.copy()
        env['MB_METADATA_STOP_FILE']=str(marker)
        env['PYTHONIOENCODING']='utf-8:replace'
        cmd=[sys.executable,'-u',str(Path(__file__).with_name('music_album_scraper.py')),
             '--options',json.dumps(opts,ensure_ascii=False)]
        proc=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                              text=True,encoding='utf-8',errors='replace',env=env)
        try:
            # Read output in a separate thread so immediate cancellation is responsive.
            import queue, threading
            lines=queue.Queue()
            def reader():
                for line in proc.stdout:
                    lines.put(line.rstrip())
                lines.put(None)
            t=threading.Thread(target=reader,daemon=True);t.start()
            while True:
                if stop_immediately():
                    marker.touch(exist_ok=True)
                    proc.terminate()
                    try: proc.wait(timeout=3)
                    except subprocess.TimeoutExpired: proc.kill()
                    break
                if stop_after_file():
                    marker.touch(exist_ok=True)
                try: line=lines.get(timeout=.15)
                except queue.Empty:
                    if proc.poll() is not None and not t.is_alive(): break
                    continue
                if line is None: break
                if line.startswith('[MB_METADATA_PROGRESS] '):
                    try:
                        progress=json.loads(line.split('] ',1)[1]);n=max(1,int(progress['total']))
                        update('scraper_progress',{'index':int(progress['current'])+1,
                                                   'total':n,'file':Path(progress.get('file','')).name},1,1)
                    except (ValueError,KeyError): pass
                elif line:
                    activity_event('Music scraper: '+line, 'ERROR' if '[ERROR]' in line else 'INFO')
                    if line.startswith(('[TRACK]', '[ALBUM]', '[SINGLES]', '[MUSIC ART] Combined artist NFO checked:', '[WARN]', '[ERROR]')):
                        update('scraper_log',line,1,1)
            return proc.wait()
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:proc.wait(timeout=3)
                except subprocess.TimeoutExpired:proc.kill();proc.wait()

def run_music_wizard(settings, update, stop_after_file, stop_immediately):
    source,target=Path(settings['source']).resolve(),Path(settings['target']).resolve()
    ffmpeg,ffprobe=get_tool('ffmpeg'),get_tool('ffprobe')
    if not ffmpeg or not ffprobe:
        return {'fatal':'FFmpeg and FFprobe are required.'}
    whitelist={str(c).strip().lower() for c in settings.get('music_whitelist',[]) if str(c).strip()}
    enabled=bool(settings.get('whitelist_enabled',True))
    files=sorted((f for f in source.rglob('*') if f.is_file() and f.suffix.lower() in EXTENSIONS | {'.ape','.wv','.mka','.alac'}),key=lambda x:str(x).casefold())
    success=failed=skipped=0
    start_run()
    for index,item in enumerate(files,1):
        if stop_after_file() or stop_immediately():break
        update('file',item.name,index,len(files))
        try:
            input_codec=_codec(ffprobe,item)
            keep=enabled and input_codec in whitelist
            profile=settings['lossless'] if input_codec in LOSSLESS else settings['lossy']
            codec=profile['codec']
            quality=int(profile['quality']);gain=float(profile['gain'])
            cue=find_cue_for_image(item)
            if cue:
                # Even whitelisted FLAC must be encoded when splitting an image.
                # Preserve a supported source format when on the whitelist.
                if keep:
                    codec={'flac':'flac','mp3':'mp3','opus':'opus','aac':'aac','vorbis':'vorbis'}.get(input_codec,codec)
                    gain=0.0
                album_dir=target/item.relative_to(source).parent/item.stem
                destinations=[album_dir/safe_track_name(track,PROFILES[codec].extension) for track in cue.tracks]
                if len(set(destinations))!=len(destinations):raise ValueError('Duplicate CUE track names')
                all_ok=True
                for j,(track,dst) in enumerate(zip(cue.tracks,destinations)):
                    if stop_after_file() or stop_immediately():all_ok=False;break
                    if dst.exists():
                        skipped+=1;continue
                    end=cue.tracks[j+1].start if j+1<len(cue.tracks) else None
                    error=None
                    for mode in ('output','input','filter','reblock'):
                        if stop_immediately():break
                        try:
                            recode_file(item,dst,codec=codec,quality=quality,gain_db=gain,
                                ffmpeg=ffmpeg,ffprobe=ffprobe,include_cover=False,
                                start_seconds=track.start,duration_seconds=end-track.start if end else None,
                                track_metadata=track.metadata,seek_mode=mode,
                                stop_event=settings['_immediate_cancel_event'],
                                progress=lambda pct,j=j,n=len(destinations):update('progress',{'percent':100*(j+pct/100)/n},index,len(files)))
                            error=None;success+=1;break
                        except Exception as exc:error=exc
                    if error:
                        failed+=1;all_ok=False
                        activity_event(f'Music wizard CUE track ERROR: {item} / {track.number}: {error}','ERROR')
                if all_ok and destinations:
                    # The existing deletion list validates the destination's existence.
                    add_success(item,destinations[0])
                    # The CUE itself is a separate original, only delete after explicit confirmation.
                    add_success(cue.cue,destinations[0])
                update('progress',{'percent':100},index,len(files))
                continue
            dst=target/item.relative_to(source)
            if not keep:dst=dst.with_suffix(PROFILES[codec].extension)
            if dst.exists():
                skipped+=1
                update('progress',{'percent':100},index,len(files));continue
            dst.parent.mkdir(parents=True,exist_ok=True)
            if keep:
                # Copy to a temporary sibling and publish atomically; never overwrite.
                temp=dst.with_name(dst.name+'.mb-partial')
                try:
                    with item.open('rb') as inp,temp.open('xb') as out:
                        while True:
                            if stop_immediately():raise InterruptedError('Cancelled')
                            chunk=inp.read(1024*1024)
                            if not chunk:break
                            out.write(chunk)
                    if dst.exists():raise FileExistsError(dst)
                    temp.rename(dst)
                finally:
                    temp.unlink(missing_ok=True)
            else:
                recode_file(item,dst,codec=codec,quality=quality,gain_db=gain,
                    ffmpeg=ffmpeg,ffprobe=ffprobe,include_cover=codec in ('mp3','flac','aac'),
                    stop_event=settings['_immediate_cancel_event'],
                    progress=lambda pct:update('progress',{'percent':pct},index,len(files)))
            success+=1
            add_success(item,dst)
        except Exception as exc:
            if stop_immediately():break
            failed+=1
            activity_event(f'Music wizard audio ERROR: {item}: {exc}','ERROR')
        update('progress',{'percent':100},index,len(files))
    aborted=stop_after_file() or stop_immediately()
    scraper_rc=0
    if not aborted:
        activity_event(f'Music wizard: starting scraper for complete destination: {target}')
        update('scraper_start',str(target),1,1)
        scraper_rc=_scrape(settings,target,update,stop_after_file,stop_immediately)
    return {'processed':success,'failed':failed,'skipped':skipped,'scraper_rc':scraper_rc,
            'aborted':bool(aborted or stop_after_file() or stop_immediately())}
