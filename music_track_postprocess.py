"""Test 198: music track editor. No track NFO files are created."""
from __future__ import annotations
import io
import os
import subprocess
import tempfile
from pathlib import Path
from mutagen import File
from mutagen.id3 import ID3, APIC, TPE1, TPE2, TIT2, TALB, TDRC, TRCK, TPOS, TCON, TPUB, COMM
from mutagen.flac import Picture
from mutagen.mp4 import MP4, MP4Cover
from PySide6.QtCore import Qt, QSettings, QSize, QEvent, QUrl, QProcess, Signal, QTimer
from PySide6.QtGui import QPixmap, QDesktopServices
from PySide6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QGridLayout,QLabel,QPushButton,
    QFileDialog,QListWidget,QListWidgetItem,QLineEdit,QTextEdit,QStackedWidget,QMessageBox,QSplitter,QGroupBox,QCheckBox,
    QDialog,QDialogButtonBox,QDoubleSpinBox,QAbstractItemView,QSizePolicy)
from gui_i18n import _
from postprocess_entry import make_entry
from music_lyrics_offset import read_lyrics, shift_lrc, apply_offset
from music_track_rename import destination_for_tags, destination_for_single_artist, move_track_group
import json
from music_hierarchy_205 import MusicHierarchy205
from music_postprocess_layout import cover_side, ACTION_COLUMN_SPACING, ACTION_ROW_SPACING

EXTENSIONS={'.mp3','.flac','.ogg','.opus','.m4a','.mp4','.wav','.aiff','.aif'}
FIELDS=[('title',_('Title')),('artist',_('Track artists (separate multiple with ;)')),
        ('albumartist',_('Album artist')),('album',_('Album')),('genre',_('Genre')),
        ('label',_('Studio / Label'))]
COMPACT=[('date',_('Year')),('tracknumber',_('Track')),('discnumber',_('Disc')),('runtime',_('Runtime'))]
EASY={'title':'title','artist':'artist','albumartist':'albumartist','album':'album',
      'genre':'genre','label':'organization','date':'date','tracknumber':'tracknumber','discnumber':'discnumber'}
ID3_MAP={'title':'TIT2','artist':'TPE1','albumartist':'TPE2','album':'TALB','genre':'TCON',
         'label':'TPUB','date':'TDRC','tracknumber':'TRCK','discnumber':'TPOS'}
MP4_MAP={'title':'\xa9nam','artist':'\xa9ART','albumartist':'aART','album':'\xa9alb',
         'genre':'\xa9gen','date':'\xa9day','label':'----:com.apple.iTunes:LABEL'}

class ArtworkPreview(QLabel):
    def __init__(self, kind, callback):
        super().__init__()
        self.kind=kind;self.callback=callback
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(100,100)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Expanding)
        self.original_pixmap=QPixmap()
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet("border: none; background: transparent;")

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return max(100, width)

    def resizeEvent(self,event):
        # The containing editor assigns the same square size as the album editor.
        # Never resize the widget recursively from its own resize event.
        super().resizeEvent(event)
        self._rescale()

    def setArtwork(self, pix):
        self.original_pixmap=pix
        self._rescale()

    def _rescale(self):
        if not self.original_pixmap.isNull():
            target=self.size()-QSize(8,8)
            if target.width()>0 and target.height()>0:
                self.setPixmap(self.original_pixmap.scaled(target,Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation))
        else:
            self.setPixmap(QPixmap())
            self.setText(_('No artwork'))

    def mousePressEvent(self,event):
        if event.button()==Qt.MouseButton.LeftButton:self.callback(self.kind)
        else:super().mousePressEvent(event)

def _list(value):
    if value is None:return []
    if isinstance(value,(list,tuple)):return [str(x) for x in value]
    return [str(value)]

def _read(path):
    audio=File(str(path),easy=True)
    if audio is None:raise ValueError(_('Unsupported audio format.'))
    data={k:'; '.join(_list(audio.get(v,[]))) for k,v in EASY.items()}
    raw=File(str(path),easy=False)
    if isinstance(raw,MP4):
        for key,atom in MP4_MAP.items():
            if key=='label':continue
            if atom in raw:
                data[key]='; '.join(_list(raw[atom]))
        for key,atom in [('tracknumber','trkn'),('discnumber','disk')]:
            if raw.get(atom):data[key]=str(raw[atom][0][0])
    if isinstance(raw.tags,ID3):
        for key,frame in ID3_MAP.items():
            value=raw.tags.getall(frame)
            if value:data[key]='; '.join(str(x) for x in value[0].text)
    duration=getattr(getattr(raw,'info',None),'length',0) or 0
    data['runtime']=f'{int(duration)//3600:02d}:{int(duration)%3600//60:02d}:{int(duration)%60:02d}'
    data['comment']=''
    if isinstance(raw.tags,ID3):
        comments=raw.tags.getall('COMM')
        if comments:data['comment']=str(comments[0].text[0])
    elif isinstance(raw,MP4):data['comment']=str((raw.get('\xa9cmt') or [''])[0])
    elif raw.tags is not None:
        data['comment']='; '.join(_list(raw.tags.get('COMMENT',[])))
    return data

def _art(path,kind):
    raw=File(str(path),easy=False)
    if raw is None:return None
    if isinstance(raw.tags,ID3):
        pics=raw.tags.getall('APIC')
        matches=[p for p in pics if p.type==kind]
        return matches[0].data if matches else None
    if isinstance(raw,MP4):
        pics=raw.get('covr',[])
        return bytes(pics[0]) if kind==3 and pics else None
    if hasattr(raw,'pictures'):
        pics=[p for p in raw.pictures if p.type==kind]
        return pics[0].data if pics else None
    if raw.tags is not None:
        import base64
        for encoded in raw.tags.get('metadata_block_picture',[]):
            try:
                pic=Picture(base64.b64decode(encoded))
                if pic.type==kind:return pic.data
            except (ValueError,TypeError):pass
    return None

def _write(path, values, images, clean=False):
    """Write only managed fields normally; optional clean rebuilds tags, retaining chosen art."""
    raw=File(str(path),easy=False)
    if raw is None:raise ValueError(_('Unsupported audio format.'))
    if raw.tags is None:raw.add_tags()
    preserved_lyrics=[]
    if clean and isinstance(raw.tags,ID3):
        # Preserve both plain and time-synchronized lyrics during explicit cleanup.
        preserved_lyrics=[frame for frame in raw.tags.values() if frame.FrameID in ('USLT','SYLT')]
    elif clean and raw.tags is not None:
        preserved_lyrics={key:list(value) for key,value in raw.tags.items()
                          if str(key).lower() in ('lyrics','unsyncedlyrics','syncedlyrics','\xa9lyr')}
    if clean:
        # Preserve current artwork unless the user has explicitly replaced it.
        for kind in (3,):
            if kind not in images:images[kind]=_art(path,kind)
        raw.delete()
        raw=File(str(path),easy=False)
        if raw.tags is None:raw.add_tags()
    if isinstance(raw.tags,ID3):
        frame_classes={'title':TIT2,'artist':TPE1,'albumartist':TPE2,'album':TALB,
                       'genre':TCON,'label':TPUB,'date':TDRC,'tracknumber':TRCK,'discnumber':TPOS}
        for key,frame in ID3_MAP.items():
            raw.tags.delall(frame)
            value=values.get(key,'').strip()
            if value:raw.tags.add(frame_classes[key](encoding=3,text=[v.strip() for v in value.split(';') if v.strip()] if key in ('artist','genre') else [value]))
        if clean:
            for lyric_frame in preserved_lyrics:raw.tags.add(lyric_frame)
        raw.tags.delall('COMM')
        if values.get('comment','').strip():raw.tags.add(COMM(encoding=3,lang='eng',desc='',text=[values['comment']]))
        for kind,content in images.items():
            for frame in list(raw.tags.getall('APIC')):
                if frame.type==kind:raw.tags.delall('APIC:'+frame.desc)
            if content:raw.tags.add(APIC(encoding=3,mime='image/jpeg' if content[:3]==b'\xff\xd8\xff' else 'image/png',type=kind,desc='Cover' if kind==3 else 'Disc',data=content))
    elif isinstance(raw,MP4):
        if clean and isinstance(preserved_lyrics,dict):
            for key,value in preserved_lyrics.items():raw[key]=value
        for key,atom in MP4_MAP.items():
            if key=='label':continue
            value=values.get(key,'').strip()
            if value:raw[atom]=[value]
            elif atom in raw:del raw[atom]
        for key,atom in [('tracknumber','trkn'),('discnumber','disk')]:
            value=values.get(key,'').strip()
            if value:raw[atom]=[(int(value.split('/')[0]),0)]
            elif atom in raw:del raw[atom]
        if values.get('comment','').strip():raw['\xa9cmt']=[values['comment']]
        elif '\xa9cmt' in raw:del raw['\xa9cmt']
        if 3 in images:
            content=images[3]
            if content:raw['covr']=[MP4Cover(content,imageformat=MP4Cover.FORMAT_JPEG if content[:3]==b'\xff\xd8\xff' else MP4Cover.FORMAT_PNG)]
            elif 'covr' in raw:del raw['covr']
        if images.get(6):raise ValueError(_('Disc artwork is not supported by this audio format.'))
    else:
        if clean and isinstance(preserved_lyrics,dict):
            for key,value in preserved_lyrics.items():raw.tags[key]=value
        if not hasattr(raw.tags,'__setitem__'):raise ValueError(_('This audio format does not support editing these tags.'))
        for key,tag in EASY.items():
            value=values.get(key,'').strip()
            if value:raw.tags[tag.upper()]=[v.strip() for v in value.split(';') if v.strip()] if key in ('artist','genre') else [value]
            elif tag.upper() in raw.tags:del raw.tags[tag.upper()]
        value=values.get('comment','').strip()
        if value:raw.tags['COMMENT']=[value]
        elif 'COMMENT' in raw.tags:del raw.tags['COMMENT']
        if images:
            if hasattr(raw,'clear_pictures') and hasattr(raw,'add_picture'):
                existing=[p for p in raw.pictures if p.type not in images]
                raw.clear_pictures()
                for pic in existing:raw.add_picture(pic)
                for kind,content in images.items():
                    if content:
                        pic=Picture();pic.type=kind;pic.mime='image/jpeg' if content[:3]==b'\xff\xd8\xff' else 'image/png';pic.data=content
                        raw.add_picture(pic)
            else:
                raise ValueError(_('Embedded artwork editing is not supported by this audio format.'))
    raw.save()

class MusicTrackPostprocess(QWidget):
    scrape_status = Signal(str)
    def __init__(self,parent=None):
        super().__init__(parent)
        self.tracks=[];self.current=None;self.initial={};self.images={};self.loading=False;self.players=[];self.scrape_proc=None
        main=QVBoxLayout(self);main.setContentsMargins(5,5,5,5)
        self.pages=QStackedWidget();main.addWidget(self.pages)
        self.folder_edit=QLineEdit(QSettings('BuddyTools','Media-Buddy-V2').value('postprocess/music_folder',''))
        self.pages.addWidget(make_entry(self.folder_edit,self.browse,self.open_folder,_('Post-processing: Music'),_('Music folder')))
        edit=QWidget();layout=QVBoxLayout(edit);layout.setContentsMargins(4,4,4,4)
        self.track_heading=QLabel(_('Tracks'))
        self.track_heading.setStyleSheet('font-weight:bold;font-size:16px;')
        layout.addWidget(self.track_heading)
        split=QSplitter(Qt.Orientation.Horizontal);layout.addWidget(split)
        self.track_list=QListWidget();self.track_list.setMinimumWidth(260)
        self.track_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.track_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        split.addWidget(self.track_list)
        center=QWidget();cl=QVBoxLayout(center);cl.setContentsMargins(2,2,2,2)
        grid=QGridLayout();grid.setSpacing(5);grid.setColumnStretch(1,1);self.inputs={}
        for row,(key,label) in enumerate(FIELDS):
            inp=QLineEdit();inp.setMinimumHeight(26);self.inputs[key]=inp
            grid.addWidget(QLabel(label),row,0);grid.addWidget(inp,row,1)
        cl.addLayout(grid)
        compact=QGridLayout();compact.setSpacing(5)
        for column,(key,label) in enumerate(COMPACT):
            box=QVBoxLayout();box.addWidget(QLabel(label));inp=QLineEdit();inp.setMinimumHeight(26)
            if key=='runtime':inp.setReadOnly(True)
            self.inputs[key]=inp;box.addWidget(inp);compact.addLayout(box,0,column)
        cl.addLayout(compact)
        cl.addWidget(QLabel(_('Description / Comment')))
        self.comment=QTextEdit();self.comment.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Expanding);cl.addWidget(self.comment,1)
        split.addWidget(center)
        right=QWidget();rl=QVBoxLayout(right);rl.setContentsMargins(4,4,4,4);rl.setSpacing(5)
        self.previews={}
        # Two separate vertical layout regions: the artwork can never cover the buttons.
        art_area=QWidget();art_layout=QVBoxLayout(art_area)
        art_layout.setContentsMargins(0,0,0,0);art_layout.setSpacing(4)
        preview=ArtworkPreview(3,self.select_art)
        preview.setMinimumSize(100,100)
        preview.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Preferred)
        art_layout.addWidget(preview,0,Qt.AlignmentFlag.AlignHCenter)
        self.previews[3]=preview
        rl.addWidget(art_area,1)
        rl.addStretch(1)
        self.track_cover_right=right
        right.installEventFilter(self)
        actions_area=QWidget();actions=QGridLayout(actions_area)
        actions.setContentsMargins(0,0,0,0)
        actions.setHorizontalSpacing(ACTION_COLUMN_SPACING)
        actions.setVerticalSpacing(ACTION_ROW_SPACING)
        image_search=QPushButton(_('Image search'));image_search.clicked.connect(self.search_images);actions.addWidget(image_search,0,0)
        artist=QPushButton(_('Edit artist'));artist.clicked.connect(self.edit_artist);actions.addWidget(artist,0,1)
        lyrics=QPushButton(_('Edit song lyrics'));lyrics.clicked.connect(self.edit_lyrics);actions.addWidget(lyrics,1,0)
        clean=QPushButton(_('Rebuild tags'));clean.clicked.connect(self.rebuild_current);actions.addWidget(clean,1,1)
        play=QPushButton(_('Play'));play.clicked.connect(self.play);actions.addWidget(play,2,0)
        rescrape=QPushButton(_('Rescrape track'));rescrape.clicked.connect(self.rescrape_track);actions.addWidget(rescrape,2,1)
        self.rescrape_button=rescrape
        back=QPushButton(_('Back'));back.clicked.connect(self.leave);actions.addWidget(back,3,0,1,2)
        actions.setColumnStretch(0,1);actions.setColumnStretch(1,1)
        rl.addWidget(actions_area,0)
        split.addWidget(right);split.setChildrenCollapsible(False);split.setStretchFactor(0,3);split.setStretchFactor(1,5);split.setStretchFactor(2,4);split.setSizes([300,500,380])
        self.pages.addWidget(edit);self.track_list.currentRowChanged.connect(self.switch_track)
        self.track_list.itemDoubleClicked.connect(self.play_selected_track)
        self.hierarchy=MusicHierarchy205(self)
        self.pages.addWidget(self.hierarchy)
        self.hierarchy.open_tracks.connect(self.show_album_tracks)


    def browse(self):
        folder=QFileDialog.getExistingDirectory(self,_('Select music folder'),self.folder_edit.text())
        if folder:self.folder_edit.setText(folder)

    def open_folder(self,folder_override=None):
        if isinstance(folder_override,bool):folder_override=None
        folder=Path(folder_override if folder_override is not None else self.folder_edit.text().strip())
        if not folder.is_dir():QMessageBox.warning(self,_('Music'),_('The selected folder does not exist.'));return
        QSettings('BuddyTools','Media-Buddy-V2').setValue('postprocess/music_folder',str(folder))
        self.folder_edit.setText(str(folder))
        self.tracks=sorted((p for p in folder.rglob('*') if p.is_file() and p.suffix.lower() in EXTENSIONS),key=lambda p:str(p).casefold())
        self.hierarchy.load(self.tracks,folder)
        self.pages.setCurrentWidget(self.hierarchy)
        return

    @staticmethod
    def _track_label(track, info):
        number=(info.get('tracknumber') or '').split('/',1)[0].strip()
        if number.isdigit():
            number=f'{int(number):02d}'
        else:
            import re
            match=re.match(r'^(\d{1,3})\s*[-–.]\s*',track.stem)
            number=f'{int(match.group(1)):02d}' if match else ''
        title=info.get('title') or track.stem
        return f'{number} - {title}' if number else title

    def play_selected_track(self,item):
        index=self.track_list.row(item)
        if index<0 or index>=len(self.tracks):return
        if self.track_list.currentRow()!=index:
            self.track_list.setCurrentRow(index)
        if self.current==self.tracks[index] and self.save_current():
            self.play()

    def show_album_tracks(self, selected):
        if not self.save_current():return
        self.current=None;self.loading=True;self.track_list.clear()
        self.tracks=list(selected)
        for track in self.tracks:
            try:
                info=_read(track);display=self._track_label(track,info)
            except Exception:display=track.stem
            item=QListWidgetItem(display);item.setToolTip(display+"\n"+str(track));self.track_list.addItem(item)
        self.loading=False
        self.pages.setCurrentIndex(1);self.track_list.setCurrentRow(0 if self.tracks else -1)
        QTimer.singleShot(0,self._size_track_cover)
        if not self.tracks:QMessageBox.information(self,_('Music'),_('No audio tracks found.'))

    def _size_track_cover(self):
        """Use the same cover geometry as the album editor (Test 212)."""
        if not hasattr(self,'track_cover_right') or self.pages.currentIndex()!=1:
            return
        right=self.track_cover_right
        preview=self.previews[3]
        side=cover_side(right)
        # QLabel's pixmap sizeHint can otherwise collapse its width while
        # the old height remains fixed, producing a tall narrow preview.
        if preview.width()!=side or preview.height()!=side:
            preview.setFixedSize(side,side)
        preview._rescale()

    def eventFilter(self,obj,event):
        if obj is getattr(self,'track_cover_right',None) and event.type()==QEvent.Type.Resize:
            QTimer.singleShot(0,self._size_track_cover)
        return super().eventFilter(obj,event)

    def switch_track(self,index):
        if self.loading:return
        previous=self.current
        if previous and not self.save_current():
            self.loading=True;self.track_list.setCurrentRow(self.tracks.index(previous));self.loading=False;return
        self.current=self.tracks[index] if 0<=index<len(self.tracks) else None
        try:self.initial=_read(self.current) if self.current else {}
        except Exception as exc:
            QMessageBox.warning(self,_('Music'),str(exc));self.initial={}
        for key,inp in self.inputs.items():
            value=self.initial.get(key,'')
            if key=='albumartist' and not value:
                value=self.initial.get('artist','')
            if key=='date':
                from music_hierarchy_205 import _display_date
                value=_display_date(value)
            inp.setText(value)
        self.comment.setPlainText(self.initial.get('comment',''))
        self.images={};self.refresh_art()

    def refresh_art(self):
        for kind,widget in self.previews.items():
            try:content=self.images[kind] if kind in self.images else (_art(self.current,kind) if self.current else None)
            except Exception:content=None
            pix=QPixmap()
            if content:pix.loadFromData(content)
            widget.setArtwork(pix)

    def select_art(self,kind):
        if not self.current:return
        src,selected_filter=QFileDialog.getOpenFileName(self,_('Select artwork'),'','Images (*.jpg *.jpeg *.png)')
        if not src:return
        try:
            data=Path(src).read_bytes();pix=QPixmap();assert pix.loadFromData(data)
            self.images[kind]=data;self.refresh_art()
        except (OSError,AssertionError):QMessageBox.warning(self,_('Artwork'),_('Could not read the image.'))

    def _values(self):
        result={k:inp.text().strip() for k,inp in self.inputs.items() if k!='runtime'}
        from music_hierarchy_205 import _storage_date
        result['date']=_storage_date(result.get('date',''))
        result['comment']=self.comment.toPlainText().strip()
        # Kodi groups loose tracks under Singles using the album performer.
        # If none was supplied, the track performer is the safe fallback.
        if not result.get('albumartist') and result.get('artist'):
            result['albumartist']=result['artist']
        return result

    def save_current(self,clean=False):
        if not self.current:return True
        values=self._values()
        if not clean and all(values.get(k,'')==self.initial.get(k,'') for k in values) and not self.images:return True
        try:
            for key in ('tracknumber','discnumber'):
                if values[key] and not values[key].split('/')[0].isdigit():raise ValueError(_('Track and disc numbers must be numeric.'))
            _write(self.current,values,dict(self.images),clean=clean)
            # A saved album.nfo is authoritative for release identifiers. In
            # particular, intentionally empty IDs must not survive a rebuild.
            album_nfo=self.current.parent/'album.nfo'
            if album_nfo.is_file():
                from music_hierarchy_205 import _nfo, _update_album_audio_tags
                album_data=_nfo(album_nfo)
                _update_album_audio_tags(self.current,{
                    'musicbrainzalbumid':album_data.get('musicbrainzalbumid',''),
                    'musicbrainzreleasegroupid':album_data.get('musicbrainzreleasegroupid','')})
            # Rename only after the tags have been saved successfully. All same-stem
            # support files move together; collisions are rejected without overwriting.
            try:
                from configuration_store import effective
                config=effective(Path(__file__).with_name('Media-Buddy.json'))
                patterns=config.get('Audio',{}).get('Music',{})
                if not isinstance(patterns,dict):patterns={}
                destination=destination_for_tags(self.current,values,patterns)
                destination=destination_for_single_artist(destination,values,patterns)
                # Explicit album assignment of a loose single: move into an album folder.
                # Existing album folders and their editions are never relocated here.
                if values.get('album','').strip() and not (self.current.parent/'album.nfo').exists():
                    root=getattr(self.hierarchy,'root',None)
                    if root and self.current.parent.resolve().parent==Path(root).resolve():
                        from music_track_rename import safe_name
                        album_folder=self.current.parent/safe_name(values['album'])
                        destination=album_folder/destination.name
                old=self.current
                if destination!=old:
                    self.current=move_track_group(old,destination)
                    index=self.tracks.index(old)
                    self.tracks[index]=self.current
                    display=self._track_label(self.current,values)
                    item=self.track_list.item(index)
                    item.setText(display);item.setToolTip(display+'\n'+str(self.current))
            except Exception as rename_error:
                QMessageBox.warning(self,_('Rename failed'),
                    _('Audio tags were saved, but the file could not be renamed:')+'\n'+str(rename_error))
            self.initial=_read(self.current);self.images={};self.refresh_art();return True
        except Exception as exc:
            QMessageBox.warning(self,_('Save failed'),_('Could not save audio tags:')+'\n'+str(exc));return False

    def rescrape_track(self):
        if not self.current or self.scrape_proc is not None:return
        if not self.save_current():return
        track=self.current
        if not track.is_file():return
        from configuration_store import effective
        from PySide6.QtCore import QSettings
        import sys
        music=effective(Path(__file__).with_name('Media-Buddy.json')).get('Audio',{}).get('Music',{})
        settings=QSettings('BuddyTools','Media-Buddy-V2')
        def enabled(key,default):
            return str(settings.value('music_scraper/'+key,str(default).lower())).lower()=='true'
        album_nfo=track.parent/'album.nfo'
        in_album=album_nfo.is_file()
        opts=dict(single_track=str(track),source=str(track.parent),
                  lookup=enabled('lookup',True),write_tags=enabled('tagging',False),
                  lyrics=enabled('lyrics',True),plain_lyrics=enabled('plain',True),
                  embed_lyrics=enabled('embed_lyrics',False),clean_lyrics=enabled('clean_lyrics',False),
                  album_artwork=False if in_album else enabled('album_artwork',False),
                  embed_album_cover=False if in_album else enabled('album_artwork',False),
                  artist_artwork=False if in_album else enabled('artist_artwork',False),
                  music_nfo=False if in_album else enabled('music_nfo',False),
                  combined_artist_nfo=False if in_album else enabled('combined_artist_nfo',False),
                  artist_artwork_root=effective(Path(__file__).with_name('Media-Buddy.json')).get('Audio',{}).get('ArtistArtworkPath',''),
                  audio_extensions=music.get('Extensions',['.mp3','.flac','.wav','.m4a','.ogg','.mka']))
        proc=QProcess(self)
        self.scrape_proc=proc
        self.rescrape_button.setEnabled(False)
        self.scrape_status.emit(_('Scraping track: {track}').format(track=track.stem))
        proc.setProgram(sys.executable)
        proc.setArguments(['-u',str(Path(__file__).with_name('music_album_scraper.py')),
                           '--options',json.dumps(opts,ensure_ascii=False)])
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        def finished(exit_code,status):
            output=bytes(proc.readAllStandardOutput()).decode('utf-8','replace')
            self.scrape_proc=None
            self.rescrape_button.setEnabled(True)
            proc.deleteLater()
            if exit_code==0 and status==QProcess.ExitStatus.NormalExit and in_album:
                try:
                    from music_hierarchy_205 import _nfo, _apply_album_to_track
                    album_data=_nfo(album_nfo)
                    artist_name=track.parent.parent.name
                    _apply_album_to_track(track,album_data,artist_name,track.parent/'folder.jpg')
                except Exception as album_error:
                    output += '\nAlbum synchronization failed: '+str(album_error)
                    exit_code=1
            if self.current==track:
                try:
                    self.initial=_read(track)
                    for key,inp in self.inputs.items():
                        inp.setText(self.initial.get(key,'') if key!='albumartist' else
                                    (self.initial.get(key,'') or self.initial.get('artist','')))
                    self.comment.setPlainText(self.initial.get('comment',''))
                    self.refresh_art()
                except Exception:pass
            if exit_code==0 and status==QProcess.ExitStatus.NormalExit:
                self.scrape_status.emit(_('Track scraped: {track}').format(track=track.stem))
            else:
                self.scrape_status.emit(_('Track scraping failed: {track}').format(track=track.stem))
                QMessageBox.warning(self,_('Rescrape track'),
                    _('Track scraper failed:')+'\n'+output[-1800:])
        proc.finished.connect(finished)
        proc.start()
        if not proc.waitForStarted(1500):
            self.scrape_proc=None;self.rescrape_button.setEnabled(True)
            self.scrape_status.emit(_('Track scraping failed: {track}').format(track=track.stem))
            QMessageBox.warning(self,_('Rescrape track'),_('Could not start music scraper.'))
            proc.deleteLater()

    def rebuild_current(self):
        if not self.current:return
        result=QMessageBox.question(self,_('Rebuild tags'),_('Remove all existing tags and write only the displayed metadata and artwork? This cannot be undone.'),QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)
        if result==QMessageBox.StandardButton.Yes:self.save_current(clean=True)

    def edit_lyrics(self):
        if not self.current:return
        if not self.save_current():return
        try:
            original, origin=read_lyrics(self.current)
            if not original:
                QMessageBox.information(self,_('Song lyrics'),_('No lyrics available for this track.'))
                return
            shifted, count=shift_lrc(original,0)
            if not count:
                QMessageBox.information(self,_('Song lyrics'),_('No synchronized timestamps found in the lyrics.'))
                return
        except Exception as exc:
            QMessageBox.warning(self,_('Song lyrics'),str(exc));return
        dialog=QDialog(self);dialog.setWindowTitle(_('Synchronize song lyrics'))
        dialog.setMinimumWidth(440)
        dl=QVBoxLayout(dialog)
        dl.addWidget(QLabel(_('Shift all synchronized lyrics timestamps (positive = later, negative = earlier).')))
        offset=QDoubleSpinBox();offset.setRange(-3600,3600);offset.setDecimals(1)
        offset.setSingleStep(0.1);offset.setSuffix(' s');offset.setValue(0)
        dl.addWidget(offset)
        preview=QLabel();preview.setWordWrap(True);dl.addWidget(preview)
        def update_preview(value):
            result, _count=shift_lrc(original,value)
            sample=next((line for line in result.splitlines() if '[' in line and ':' in line),'')
            preview.setText(_('Preview:')+' '+sample[:180])
        offset.valueChanged.connect(update_preview);update_preview(0)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept);buttons.rejected.connect(dialog.reject)
        dl.addWidget(buttons)
        if dialog.exec()!=QDialog.DialogCode.Accepted or offset.value()==0:return
        try:
            changed=apply_offset(self.current,offset.value())
            QMessageBox.information(self,_('Song lyrics'),_('Updated synchronized lyrics timestamps:')+' '+str(changed))
        except Exception as exc:
            QMessageBox.warning(self,_('Song lyrics'),_('Could not synchronize song lyrics:')+'\n'+str(exc))

    def search_images(self):
        # Prefer the track artist. Untagged tracks can still be searched by
        # deriving the artist from the conventional "Artist - Title" filename.
        from PySide6.QtCore import QUrlQuery
        artist=self.inputs['artist'].text().strip()
        if not artist and self.current:
            stem=self.current.stem.strip()
            for separator in (' – ', ' - '):
                if separator in stem:
                    artist=stem.split(separator,1)[0].strip()
                    break
        if not artist:
            QMessageBox.information(self,_('Image search'),_('No track artist available for image search.'))
            return
        url=QUrl('https://www.google.com/search')
        query=QUrlQuery();query.addQueryItem('tbm','isch');query.addQueryItem('q',artist)
        url.setQuery(query)
        if not QDesktopServices.openUrl(url):
            QMessageBox.warning(self,_('Image search'),_('Could not open the web browser.'))

    def edit_artist(self):
        if not self.save_current():return
        if self.current:
            info=_read(self.current)
            self.hierarchy.show_track_artists([self.current], info.get('albumartist',''))
            self.pages.setCurrentWidget(self.hierarchy)

    def _cleanup_players(self):
        self.players=[proc for proc in self.players if proc.poll() is None]

    def play(self):
        if not self.current:return
        try:
            from tool_manager import get_tool
            self._cleanup_players()
            proc=subprocess.Popen([str(get_tool('mpv')),'--force-window=yes','--autofit-smaller=600x600','--autofit-larger=900x800',str(self.current)],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,cwd=str(self.current.parent))
            self.players.append(proc)
        except Exception as exc:QMessageBox.warning(self,_('Play'),str(exc))

    def leave(self):
        if self.save_current():
            self.current=None
            self.hierarchy.refresh_after_tracks()
            self.pages.setCurrentWidget(self.hierarchy)

    def closeEvent(self,event):
        if not self.save_current():event.ignore();return
        for proc in self.players:
            if proc.poll() is None:
                try:proc.terminate()
                except OSError:pass
        self.players.clear()
        super().closeEvent(event)
