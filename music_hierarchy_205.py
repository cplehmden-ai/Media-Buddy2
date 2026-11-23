"""Test 205: music navigation and local NFO/artwork preview; no collection-wide edits."""
from pathlib import Path
from urllib.parse import quote
from datetime import datetime
from mutagen import File as AudioFile
from mutagen.id3 import ID3, TXXX, TDRC
from mutagen.mp4 import MP4
import xml.etree.ElementTree as ET
from PySide6.QtCore import Qt, Signal, QUrl, QTimer, QEvent
from PySide6.QtGui import QPixmap, QDesktopServices
from PySide6.QtWidgets import (QWidget,QHBoxLayout,QVBoxLayout,QGridLayout,QLabel,QListWidget,QListWidgetItem,
    QPushButton,QLineEdit,QTextEdit,QFileDialog,QMessageBox,QSplitter,QStackedWidget,QSizePolicy,QInputDialog,QScrollArea)
from gui_i18n import _
from music_postprocess_layout import cover_side, ACTION_COLUMN_SPACING, ACTION_ROW_SPACING

ART={'folder.jpg':(1,1),'fanart.jpg':(16,9),'banner.jpg':(1000,185),'clearlogo.png':(4,1)}

def _nfo(path):
    try:
        root=ET.parse(path).getroot()
        return {k:root.findtext(k,'') or '' for k in ('name','title','artist','album','musicbrainzartistid','musicbrainzalbumid','musicbrainzreleasegroupid','releasedate','year','genre','style','born','formed','died','disbanded','biography','description','review')}
    except (OSError,ET.ParseError):return {}

def _artist_id_from_nfo(path):
    try:
        root=ET.parse(path).getroot()
        for element in root.iter():
            if element.tag.lower() in ('musicbrainzartistid','musicbrainz_artistid') and (element.text or '').strip():
                return element.text.strip()
            if element.tag.lower()=='uniqueid' and (element.get('type') or '').lower() in ('musicbrainz','musicbrainzartist','musicbrainzartistid') and (element.text or '').strip():
                return element.text.strip()
    except (OSError,ET.ParseError):pass
    return ''

def _write_nfo(path,kind,values):
    try:root=ET.parse(path).getroot() if path.exists() else ET.Element(kind)
    except ET.ParseError:root=ET.Element(kind)
    for key,value in values.items():
        el=root.find(key)
        if el is None:
            if not value.strip():continue
            el=ET.SubElement(root,key)
        el.text=value.strip()
    path.parent.mkdir(parents=True,exist_ok=True)
    ET.indent(root, space='  ')
    ET.ElementTree(root).write(path,encoding='utf-8',xml_declaration=True)

def _display_date(value):
    value=(value or '').strip()
    if value.count('-')==2:
        try:
            datetime.strptime(value,'%Y-%m-%d')
            # The GUI translation catalog determines the display language.
            return datetime.strptime(value,'%Y-%m-%d').strftime('%d.%m.%Y') if _('Year')=='Jahr' else value
        except ValueError:pass
    if value.count('-')==1 and _('Year')=='Jahr':
        parts=value.split('-')
        if len(parts[0])==4 and len(parts[1])==2:return parts[1]+'.'+parts[0]
    return value

def _storage_date(value):
    value=(value or '').strip()
    for fmt in ('%d.%m.%Y','%Y-%m-%d'):
        try:return datetime.strptime(value,fmt).strftime('%Y-%m-%d')
        except ValueError:pass
    if len(value)==7 and value[2]=='.' and value[:2].isdigit() and value[3:].isdigit():return value[3:]+'-'+value[:2]
    if len(value)==4 and value.isdigit():return value
    if len(value)==7 and value[4]=='-' and value[:4].isdigit() and value[5:].isdigit():return value
    if not value:return ''
    raise ValueError(_('Invalid release date'))

def _storage_artist_date(value):
    value=(value or '').strip()
    try:return _storage_date(value)
    except ValueError:return value

def _apply_album_to_track(path, album_data, album_artist, cover_path=None):
    """Synchronize album-level tags without replacing track identity or lyrics."""
    from music_track_postprocess import _read, _write
    values = _read(path)
    if album_data.get('title') or album_data.get('name') or album_data.get('album'):
        values['album'] = (album_data.get('title') or album_data.get('name') or album_data.get('album')).strip()
    if album_artist:
        values['albumartist'] = album_artist
    if album_data.get('genre'):
        values['genre'] = album_data['genre']
    if album_data.get('releasedate') or album_data.get('year'):
        values['date'] = album_data.get('releasedate') or album_data.get('year')
    images = {3: cover_path.read_bytes()} if cover_path and cover_path.is_file() else {}
    _write(path, values, images, clean=False)
    _update_album_audio_tags(path, {
        'musicbrainzalbumid': album_data.get('musicbrainzalbumid', ''),
        'musicbrainzreleasegroupid': album_data.get('musicbrainzreleasegroupid', ''),
    })

def _update_album_audio_tags(path, changes):
    """Update ONLY edited album identifiers/date; preserve all other audio tags."""
    raw=AudioFile(str(path),easy=False)
    if raw is None:raise ValueError(_('Unsupported audio format.'))
    if raw.tags is None:raw.add_tags()
    id3names={'musicbrainzalbumid':'MusicBrainz Album Id',
              'musicbrainzreleasegroupid':'MusicBrainz Release Group Id'}
    if isinstance(raw.tags,ID3):
        for key,value in changes.items():
            if key=='releasedate':
                raw.tags.delall('TDRC')
                if value:raw.tags.add(TDRC(encoding=3,text=[value]))
            else:
                desc=id3names[key]
                for frame in list(raw.tags.getall('TXXX')):
                    if frame.desc.casefold().replace(' ','') in (desc.casefold().replace(' ',''),key.casefold(),'musicbrainzreleaseid' if key=='musicbrainzalbumid' else '__none__'):
                        raw.tags.delall('TXXX:'+frame.desc)
                if value:raw.tags.add(TXXX(encoding=3,desc=desc,text=[value]))
    elif isinstance(raw,MP4):
        atoms={'musicbrainzalbumid':'----:com.apple.iTunes:MusicBrainz Album Id',
               'musicbrainzreleasegroupid':'----:com.apple.iTunes:MusicBrainz Release Group Id',
               'releasedate':'\xa9day'}
        for key,value in changes.items():
            atom=atoms[key]
            if value:raw[atom]=[value] if key=='releasedate' else [value.encode('utf-8')]
            elif atom in raw:del raw[atom]
    else:
        for key,value in changes.items():
            tag='DATE' if key=='releasedate' else 'MUSICBRAINZ_ALBUMID' if key=='musicbrainzalbumid' else 'MUSICBRAINZ_RELEASEGROUPID'
            if value:raw.tags[tag]=[value]
            elif tag in raw.tags:del raw.tags[tag]
    raw.save()

class Preview(QLabel):
    clicked = Signal()
    def mousePressEvent(self,event):
        if self.property("album_clickable") and event.button()==Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)
    def __init__(self,name,ratio):
        super().__init__();self.name=name;self.ratio=ratio;self.original=QPixmap();self.path=None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumHeight(70 if name=='banner.jpg' else 110)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Preferred)
        self.setStyleSheet('border:1px solid palette(mid);background:palette(base);')
        self.setText(name)
    def show_file(self,path):
        self.path=path
        self.original=QPixmap(str(path)) if path and path.is_file() else QPixmap()
        # A clearlogo may contain a large transparent canvas. Trim only the
        # in-memory preview, never the source artwork on disk. Cache this once
        # per file load, not on every window resize.
        if self.name=='clearlogo.png' and not self.original.isNull():
            self.original=self._visible_clearlogo(self.original)
        self.resizeEvent(None)

    @staticmethod
    def _visible_clearlogo(pixmap):
        image=pixmap.toImage()
        if not image.hasAlphaChannel():
            return pixmap
        # Analyze a reduced copy so huge PNG files do not cause slow scans.
        scan=image.scaled(512,512,Qt.AspectRatioMode.KeepAspectRatio,
                          Qt.TransformationMode.FastTransformation)
        left,top,right,bottom=scan.width(),scan.height(),-1,-1
        for y in range(scan.height()):
            for x in range(scan.width()):
                if scan.pixelColor(x,y).alpha()>8:
                    left=min(left,x);right=max(right,x)
                    top=min(top,y);bottom=max(bottom,y)
        if right<left or bottom<top:
            return pixmap
        # Scale the bounds back to the original image, retaining a 1px safety
        # margin to avoid clipping antialiased edges.
        sx=pixmap.width()/scan.width();sy=pixmap.height()/scan.height()
        x=max(0,int(left*sx)-1);y=max(0,int(top*sy)-1)
        x2=min(pixmap.width(),int((right+1)*sx)+2)
        y2=min(pixmap.height(),int((bottom+1)*sy)+2)
        return pixmap.copy(x,y,max(1,x2-x),max(1,y2-y))
    def resizeEvent(self,event):
        if not self.original.isNull():
            self.setPixmap(self.original.scaled(max(1,self.width()),max(1,self.height()),Qt.AspectRatioMode.IgnoreAspectRatio if self.property('artist_fixed_ratio') else Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation))
        else:
            self.setPixmap(QPixmap())
            self.setText(_('No artwork') if self.property('album_clickable') else _('No artwork'))
        if event is not None:super().resizeEvent(event)

class ArtistArtworkArea(QWidget):
    """Independent artwork geometry; no layout or hidden banner reserves space.

    The poster and fanart fill the usable vertical area whenever their aspect
    ratios and the available width allow it. Clearlogo uses the left-hand
    space beside the poster without changing either image's geometry.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.slots = {}
        self.setMinimumSize(0, 0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)

    def resizeEvent(self, event):
        if event is not None:
            super().resizeEvent(event)
        width, height = max(0, self.width()), max(0, self.height())
        gap = 8
        # The fanart gets the full width (16:9). The square poster takes the
        # remaining height, but is capped by the width of the artwork pane.
        fan_h = min(round(width * 9 / 16), max(0, (height-gap)//2))
        poster_h = min(width, max(0, height-gap-fan_h))
        # If a narrow pane prevents using all the height, keep the two images
        # aligned to the top and bottom edges rather than centering the group.
        fan_w = min(width, round(fan_h * 16 / 9))
        poster_w = poster_h
        poster_y = 0
        fan_y = max(0, height-fan_h)
        geometry = {
            'folder.jpg': (max(0,width-poster_w),poster_y,poster_w,poster_h),
            'fanart.jpg': (max(0,width-fan_w),fan_y,fan_w,fan_h),
        }
        # Give the clearlogo the ENTIRE rectangle beside the poster. The
        # preview preserves the visible artwork's real aspect ratio, so both
        # wide wordmarks and nearly square logos can use the available height.
        # It is independent of the poster/fanart geometry.
        logo_w=max(0,width-poster_w-8)
        geometry['clearlogo.png']=(0,0,logo_w,max(0,poster_h))
        for filename, rect in geometry.items():
            preview = self.slots.get(filename)
            if preview is None or preview.parentWidget() is not self:
                continue
            preview.setGeometry(*rect)
            if rect[2] and rect[3]:
                preview.show()
                preview.resizeEvent(None)
            else:
                preview.hide()

class MusicHierarchy205(QWidget):
    open_tracks=Signal(object)
    def __init__(self,owner):
        super().__init__(owner);self.owner=owner;self.root=None;self.records=[];self.artist=None;self.album=None;self.album_key=None;self.mode='artists';self.dirty=False;self.edit_target=None;self.edit_kind=None;self.loading_fields=False;self.artist_scope=None;self.external_back=None
        outer=QVBoxLayout(self);outer.setContentsMargins(4,4,4,4)
        header=QHBoxLayout();self.heading=QLabel();self.heading.setStyleSheet('font-weight:bold;font-size:16px;');header.addWidget(self.heading,1)
        outer.addLayout(header)
        split=QSplitter(Qt.Orientation.Horizontal);outer.addWidget(split,1)
        self.items=QListWidget();self.items.setMinimumWidth(260);self.items.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.items.setTextElideMode(Qt.TextElideMode.ElideRight);self.items.currentRowChanged.connect(self.select);self.items.itemDoubleClicked.connect(self.enter)
        split.addWidget(self.items)
        center=QWidget();cl=QVBoxLayout(center);cl.setContentsMargins(4,4,4,4)
        self.fields={};self.field_labels={}
        # Artist and album metadata use separate visible arrangements while
        # sharing the same editor widgets and autosave behavior.
        self.artist_name=QLineEdit();self.fields['name']=self.artist_name
        # Banner is a separate center-column editor, never part of the right
        # artwork viewport. Its height yields to the biography's minimum.
        self.banner_container=QWidget()
        # Do not put the pixmap QLabel in a layout: its pixmap-dependent
        # sizeHint can enlarge the middle column and clip the banner.
        # Position the preview explicitly inside this dedicated container.
        self.banner_preview=Preview('banner.jpg',ART['banner.jpg'])
        self.banner_preview.setParent(self.banner_container)
        self.banner_preview.setProperty('album_clickable',True)
        self.banner_preview.setProperty('artist_fixed_ratio',True)
        self.banner_preview.setCursor(Qt.CursorShape.PointingHandCursor)
        self.banner_preview.setStyleSheet('background:transparent;border:none;')
        self.banner_preview.clicked.connect(lambda:self.change_image('banner.jpg'))
        self.banner_container.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Fixed)
        self.banner_container.setMinimumWidth(0)
        cl.addWidget(self.banner_container)
        cl.addWidget(self.artist_name)
        for key,label in [('musicbrainzartistid',_('MusicBrainz artist ID')),
                          ('musicbrainzalbumid',_('MusicBrainz Release ID')),
                          ('musicbrainzreleasegroupid',_('MusicBrainz Release Group ID')),
                          ('releasedate',_('Release date')),('genre',_('Genre'))]:
            lab=QLabel(label);self.field_labels[key]=lab;cl.addWidget(lab)
            inp=QLineEdit();self.fields[key]=inp;cl.addWidget(inp)
        self.artist_dates=QWidget()
        dates=QGridLayout(self.artist_dates);dates.setContentsMargins(0,0,0,0)
        dates.setHorizontalSpacing(6);dates.setVerticalSpacing(2)
        for column,(key,label) in enumerate((('born',_('Born')),('died',_('Died')),
                                             ('formed',_('Formed')),('disbanded',_('Disbanded')))):
            lab=QLabel(label);self.field_labels[key]=lab;dates.addWidget(lab,0,column)
            field=QLineEdit();self.fields[key]=field;dates.addWidget(field,1,column)
            field.setMinimumWidth(0)
        cl.addWidget(self.artist_dates)
        self.description_label=QLabel(_('Biography / Description'));cl.addWidget(self.description_label)
        self.description=QTextEdit();cl.addWidget(self.description,1)
        self.center_widget=center
        self.center_layout=cl
        center.installEventFilter(self)
        self.description.setMinimumHeight(max(48,2*self.description.fontMetrics().lineSpacing()+18))

        # Metadata is autosaved on selection changes and when leaving the page.
        self.open_next=QPushButton(_('Open albums'));self.open_next.clicked.connect(self.enter)
        self.track_artists=QPushButton(_('Track artists'));self.track_artists.clicked.connect(self.show_album_artists)
        self.links=QHBoxLayout();self.link_buttons=[]
        for label,base in [('Wikipedia','https://wikipedia.org/wiki/'),('Discogs','https://discogs.com/artist/')]:
            b=QPushButton(label);b.clicked.connect(lambda checked=False,base=base:self.open_link(base));self.links.addWidget(b);self.link_buttons.append(b)
        # The artist links will be positioned after the artwork layout is finalized.
        split.addWidget(center)
        for field in self.fields.values():field.textEdited.connect(self.mark_dirty)
        self.description.textChanged.connect(self.mark_dirty)

        right=QWidget();rl=QVBoxLayout(right);rl.setContentsMargins(4,4,4,4)
        # Artist artwork occupies a separate scrollable viewport. The action
        # panel is a fixed-height sibling, never a child of the artwork area.
        self.artwork_scroll=QScrollArea()
        self.artwork_scroll.setWidgetResizable(True)
        self.artwork_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.artwork_scroll.setContentsMargins(0,0,0,0)
        self.artwork_scroll.setViewportMargins(0,0,0,0)
        self.artwork_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.artwork_content=ArtistArtworkArea()
        self.previews={};self.art_rows={};self.art_buttons={}
        for name,ratio in ART.items():
            pv=self.banner_preview if name=='banner.jpg' else Preview(name,ratio);self.previews[name]=pv
            pv.setProperty('album_clickable',True)
            pv.setProperty('artist_fixed_ratio',name in ('folder.jpg','fanart.jpg'))
            pv.setCursor(Qt.CursorShape.PointingHandCursor)
            pv.clicked.connect(lambda n=name:self.change_image(n))
            pv.setStyleSheet('background:transparent;border:none;')
            pv.setMinimumSize(0,0)
            pv.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Ignored)
            if name!='banner.jpg':pv.setParent(self.artwork_content)
            if name in ('folder.jpg','fanart.jpg','clearlogo.png'):
                self.artwork_content.slots[name]=pv
        self.artwork_scroll.setWidget(self.artwork_content)
        self.artwork_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        rl.addWidget(self.artwork_scroll,1)
        # Reserve exactly four button rows, matching the album editor's footer.
        self.right_actions=QVBoxLayout()
        self.right_actions.setContentsMargins(0,0,0,0)
        rl.addLayout(self.right_actions)
        self.album_actions=QWidget()
        album_grid=QGridLayout(self.album_actions)
        album_grid.setContentsMargins(0,0,0,0)
        album_grid.setHorizontalSpacing(ACTION_COLUMN_SPACING)
        album_grid.setVerticalSpacing(ACTION_ROW_SPACING)
        self.album_image_search=QPushButton(_('Image search'))
        self.album_image_search.clicked.connect(self.search_album_images)
        self.album_discogs=QPushButton('Discogs')
        self.album_discogs.clicked.connect(self.open_album_artist_discogs)
        self.album_rescrape=QPushButton(_('Rescrape album'))
        self.album_rebuild=QPushButton(_('Rebuild tags'))
        self.album_rescrape.clicked.connect(self.rescrape_selected_album)
        self.album_rebuild.clicked.connect(self.rebuild_selected_album_tags)
        album_grid.addWidget(self.album_image_search,0,0)
        album_grid.addWidget(self.album_discogs,0,1)
        album_grid.addWidget(self.album_rescrape,1,0)
        album_grid.addWidget(self.album_rebuild,1,1)
        album_grid.addWidget(self.open_next,2,0)
        album_grid.addWidget(self.track_artists,2,1)
        self.album_back=QPushButton(_('Back'))
        self.album_back.clicked.connect(self.go_back)
        album_grid.addWidget(self.album_back,3,0,1,2)
        self.right_actions.addWidget(self.album_actions)
        self.artist_actions=QWidget()
        artist_grid=QGridLayout(self.artist_actions)
        artist_grid.setContentsMargins(0,0,0,0)
        artist_grid.setHorizontalSpacing(ACTION_COLUMN_SPACING)
        artist_grid.setVerticalSpacing(ACTION_ROW_SPACING)
        # No invisible spacer rows: the artwork receives all space above
        # the two real navigation rows, which stay pinned to the bottom.
        artist_grid.addWidget(self.link_buttons[0],0,0)
        artist_grid.addWidget(self.link_buttons[1],0,1)
        self.artist_open_next=QPushButton(_('Open albums'))
        self.artist_open_next.clicked.connect(self.enter)
        artist_grid.addWidget(self.artist_open_next,1,0)
        self.artist_back=QPushButton(_('Back'))
        self.artist_back.clicked.connect(self.go_back)
        artist_grid.addWidget(self.artist_back,1,1)
        self.artist_actions.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Fixed)
        self.right_actions.addWidget(self.artist_actions)
        self.album_actions.hide()
        self.right_widget=right
        self.album_right_layout=rl
        split.addWidget(right)
        split.setChildrenCollapsible(False);split.setStretchFactor(0,3);split.setStretchFactor(1,5);split.setStretchFactor(2,4)
        split.setSizes([300,475,405]);self.refresh()

    def load(self,tracks,root):
        from music_track_postprocess import _read
        self.artist_scope=None;self.external_back=None
        self.root=Path(root);self.records=[]
        for path in tracks:
            try:data=_read(path)
            except Exception:data={}
            artist=(data.get('albumartist') or data.get('artist') or path.parent.name).strip()
            album=(data.get('album') or '').strip()
            self.records.append((path,artist,album,data))
        self.mode='artists';self.artist=None;self.album=None;self.refresh()

    def load_video_artists(self, names, music_root, back_callback):
        """Open the shared artist editor with only this video's contributors."""
        self.root=Path(music_root)
        self.records=[]
        self.artist_scope=list(dict.fromkeys(name.strip() for name in names if name.strip()))
        self.external_back=back_callback
        self.artist=None;self.album=None;self.album_key=None
        self.mode='artists';self.dirty=False
        self.refresh()

    def _artists(self):
        if self.artist_scope is not None:
            return sorted(self.artist_scope,key=str.casefold)
        return sorted({r[1] for r in self.records},key=str.casefold)
    def _record_album_folder(self, path):
        """A release is stored in its own physical album folder."""
        folder=path.parent
        for ancestor in [folder,*folder.parents]:
            if ancestor == self.root.parent:break
            if (ancestor/'album.nfo').is_file():return ancestor
        return folder

    def _record_release_id(self, record):
        """Prefer a release ID in the file tags; fall back to this folder's NFO."""
        path=record[0]
        nfo_path=self._record_album_folder(path)/'album.nfo'
        if nfo_path.is_file():
            return _nfo(nfo_path).get('musicbrainzalbumid','').strip()
        try:
            raw=AudioFile(str(path),easy=False)
            tags=raw.tags if raw else None
            if isinstance(tags,ID3):
                for frame in tags.getall('TXXX'):
                    if frame.desc.casefold().replace(' ','') in ('musicbrainzalbumid','musicbrainzreleaseid'):
                        if frame.text:return str(frame.text[0]).strip()
            elif isinstance(raw,MP4):
                for atom in ('----:com.apple.iTunes:MusicBrainz Album Id','----:com.apple.iTunes:MusicBrainz Release Id'):
                    if raw.get(atom):
                        value=raw[atom][0]
                        return (value.decode('utf-8','replace') if isinstance(value,bytes) else str(value)).strip()
            elif tags is not None:
                for tag in ('MUSICBRAINZ_ALBUMID','MUSICBRAINZ_RELEASEID'):
                    if tag in tags:
                        value=tags[tag]
                        return str(value[0] if isinstance(value,(list,tuple)) else value).strip()
        except Exception:pass
        return _nfo(self._record_album_folder(path)/'album.nfo').get('musicbrainzalbumid','').strip()

    def _record_key(self,record):
        path,artist,album,_=record
        # Both the folder and the release ID are part of the identity. The
        # release-group ID is intentionally NOT used for grouping.
        folder=self._record_album_folder(path)
        nfo=folder/'album.nfo'
        if nfo.is_file():
            # A physical, managed album is one release even when incoming
            # tracks still have stale single IDs or incomplete date tags.
            metadata=_nfo(nfo)
            return (str(folder.resolve()),metadata.get('musicbrainzalbumid','').strip(),
                    (metadata.get('title') or metadata.get('album') or album).strip())
        return (str(folder.resolve()),self._record_release_id(record),album)

    def _albums(self):
        groups={}
        for record in self.records:
            if record[1]!=self.artist:continue
            key=self._record_key(record)
            groups.setdefault(key,[]).append(record)
        counts={}
        for key in groups:
            title=key[2] or _('Singles')
            counts[title]=counts.get(title,0)+1
        result=[]
        for key in groups:
            title=key[2] or _('Singles')
            if counts[title]>1:
                folder=Path(key[0]).name
                suffix=folder if folder!=title else (key[1][:8] if key[1] else str(len(result)+1))
                title=f'{title} — {suffix}'
                if any(label==title for label,existing_key in result):
                    title+=f' [{key[1][:8] if key[1] else len(result)+1}]'
            result.append((title,key))
        return sorted(result,key=lambda pair:(pair[0].casefold(),pair[1][0].casefold()))

    def _current_names(self):
        if self.mode=='artists':return self._artists()
        if self.mode=='albums':return self._albums()
        if self.mode=='track_artists':
            return sorted({x[3].get('artist','').strip() for x in self._album_records() if x[3].get('artist','').strip()},key=str.casefold)
        return []

    def _album_records(self):
        return [r for r in self.records if r[1]==self.artist and self._record_key(r)==self.album_key]

    def refresh(self):
        self.items.blockSignals(True);self.items.clear()
        for entry in self._current_names():
            if self.mode=='albums':
                label,key=entry
                item=QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole,key)
                item.setToolTip(str(Path(key[0]))+'\n'+(_('MusicBrainz Release ID')+': '+key[1] if key[1] else ''))
                self.items.addItem(item)
            else:self.items.addItem(entry)
        self.items.blockSignals(False)
        titles={'artists':_('Album artists'),'albums':_('Albums'),'track_artists':_('Track artists')}
        self.heading.setText(titles[self.mode]+((' – '+self.artist) if self.mode=='albums' and self.artist else ''))
        album_mode=self.mode=='albums'
        self.album_actions.setVisible(album_mode)
        self.artist_actions.setVisible(not album_mode)
        self.open_next.setText(_('Open tracks'))
        for key in ('musicbrainzalbumid','musicbrainzreleasegroupid','releasedate'):
            self.fields[key].setVisible(album_mode);self.field_labels[key].setVisible(album_mode)
        self.fields['musicbrainzartistid'].setVisible(not album_mode)
        self.field_labels['musicbrainzartistid'].setVisible(not album_mode)
        self.artist_dates.setVisible(not album_mode)
        for b in self.link_buttons:b.setVisible(not album_mode)
        self.description_label.setText(_('Description') if album_mode else _('Biography / Description'))
        # Album artwork is only its cover. Hide the entire unused artist-artwork
        # rows (including their buttons), not merely the empty image previews.
        self.artwork_scroll.setVisible(not album_mode)
        self.banner_container.setVisible(not album_mode)
        if not album_mode:QTimer.singleShot(0,self._size_artist_banner)
        cover=self.previews['folder.jpg']
        cover.setProperty('album_clickable',True)
        cover.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents,False)
        cover.setCursor(Qt.CursorShape.PointingHandCursor)
        cover.setStyleSheet('background:transparent;border:none;')
        if album_mode:
            if cover.parentWidget() is self.artwork_content:
                cover.setParent(self.right_widget)
                self.album_right_layout.insertWidget(1,cover,1)
            cover.setMinimumSize(0,0)
            cover.setMaximumSize(16777215,16777215)
            cover.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Preferred)
            self.album_right_layout.setAlignment(cover,Qt.AlignmentFlag.AlignHCenter)
            QTimer.singleShot(0,self._size_album_cover)
        else:
            if cover.parentWidget() is self.right_widget:
                self.album_right_layout.removeWidget(cover)
                cover.setParent(self.artwork_content)
            cover.setMinimumSize(0,0)
            cover.setMaximumSize(16777215,16777215)
            cover.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Ignored)
            cover.show()
            QTimer.singleShot(0,lambda:self.artwork_content.resizeEvent(None))
        self.items.setCurrentRow(0 if self.items.count() else -1)
        self.select(self.items.currentRow())
    def _size_album_cover(self):
        if self.mode!='albums':return
        cover=self.previews['folder.jpg']
        # Square preview without a surrounding rectangle. Leave enough room for
        # the navigation buttons, including Back at the bottom.
        size=cover_side(self.right_widget)
        if cover.width()!=size or cover.height()!=size:
            cover.setFixedSize(size,size)
        cover.resizeEvent(None)

    def eventFilter(self,obj,event):
        if obj is getattr(self,'center_widget',None) and event.type()==QEvent.Type.Resize:
            if getattr(self,'mode',None)!='albums':
                QTimer.singleShot(0,self._size_artist_banner)
        return super().eventFilter(obj,event)

    def _size_artist_banner(self):
        if not hasattr(self,'banner_container') or self.mode=='albums':return
        center=self.center_widget
        if center.width()<1 or center.height()<1:return
        margins=self.center_layout.contentsMargins()
        available_width=max(1,center.width()-margins.left()-margins.right())
        # All other editor rows keep their size. Reserve two full biography
        # lines plus the QTextEdit frame before allocating banner height.
        fixed_other=0
        for i in range(self.center_layout.count()):
            item=self.center_layout.itemAt(i)
            widget=item.widget()
            if widget is None or widget is self.banner_container or widget is self.description or not widget.isVisible():continue
            fixed_other+=widget.sizeHint().height()
        visible_count=sum(1 for i in range(self.center_layout.count()) if
            (self.center_layout.itemAt(i).widget() is not None and
             self.center_layout.itemAt(i).widget().isVisible()))
        spacing=self.center_layout.spacing()*max(0,visible_count-1)
        minimum_bio=max(self.description.minimumHeight(),2*self.description.fontMetrics().lineSpacing()+18)
        available_height=max(0,center.height()-margins.top()-margins.bottom()-fixed_other-spacing-minimum_bio)
        # Keep an accessible placeholder even at small window heights.
        banner_height=max(26,min(round(available_width*185/1000),available_height))
        banner_width=min(available_width,max(1,round(banner_height*1000/185)))
        # Use the *actual* container width, not a pixmap-derived size hint.
        # A geometry-managed preview cannot push outside the middle column.
        container_width=max(1,self.banner_container.width())
        banner_width=min(container_width,banner_width)
        banner_height=min(banner_height,max(1,round(banner_width*185/1000)))
        if self.banner_container.height()!=banner_height:
            self.banner_container.setFixedHeight(banner_height)
        x=max(0,(container_width-banner_width)//2)
        self.banner_preview.setGeometry(x,0,banner_width,banner_height)
        self.banner_preview.resizeEvent(None)

    def resizeEvent(self,event):
        super().resizeEvent(event)
        if getattr(self,'mode',None)!='albums' and hasattr(self,'banner_container'):
            QTimer.singleShot(0,self._size_artist_banner)
        if getattr(self,'mode',None)=='albums' and hasattr(self,'right_widget'):
            QTimer.singleShot(0,self._size_album_cover)

    def _artist_folder(self,name):
        if not self.root:return None
        # Music scraper stores the central Artists directory beside Audio.
        candidates=[self.root/name,self.root.parent/'Artists'/name,self.root/'Artists'/name]
        for c in candidates:
            if c.is_dir():return c
        return candidates[0]
    def _album_folder(self):
        return Path(self.album_key[0]) if self.album_key else None
    def _target(self):
        if self.mode in ('artists','track_artists'):
            name=self.items.currentItem().text() if self.items.currentItem() else None
            return self._artist_folder(name) if name else None,'artist'
        if self.mode=='albums':
            # A loose single may have no ALBUM tag at all. It still needs a
            # writable edit target so saving can create the album folder/NFO.
            return self._album_folder(),'album'
        return None,'album'
    def mark_dirty(self,*args):
        if not self.loading_fields:self.dirty=True
    def select(self,row):
        if self.dirty and not self.save_nfo():
            # Keep the UI navigable after a failed write; do not retrigger the
            # same modal warning for every selection signal.
            self.dirty=False
        self.loading_fields=True
        if self.mode=='albums':
            item=self.items.item(row) if row>=0 else None
            self.album=item.text() if item else None
            self.album_key=item.data(Qt.ItemDataRole.UserRole) if item else None
        target,kind=self._target();values=_nfo(target/(kind+'.nfo')) if target else {}
        name=self.items.item(row).text() if row>=0 and self.items.item(row) else ''
        self.fields['name'].setText(values.get('name') or values.get('title') or values.get('album') or name)
        artist_id=values.get('musicbrainzartistid','')
        if kind=='artist' and target and not artist_id:
            artist_id=_artist_id_from_nfo(target/'artist.nfo')
            if not artist_id:
                # Use the embedded MusicBrainz artist ID only as a fallback.
                for path,record_artist,album,data in self.records:
                    if record_artist==name:
                        artist_id=(data.get('musicbrainzalbumartistid') or data.get('musicbrainzartistid') or '').strip()
                        if artist_id:break
        self.fields['musicbrainzartistid'].setText(artist_id)
        for key in ('born','died','formed','disbanded'):
            self.fields[key].setText(_display_date(values.get(key,'')))
        self.fields['musicbrainzalbumid'].setText(values.get('musicbrainzalbumid','') if target and (target/'album.nfo').is_file() and kind=='album' else (values.get('musicbrainzalbumid') or (self.album_key[1] if self.mode=='albums' and self.album_key else '')))
        self.fields['musicbrainzreleasegroupid'].setText(values.get('musicbrainzreleasegroupid',''))
        self.fields['releasedate'].setText(_display_date(values.get('releasedate') or values.get('year','')))
        self.fields['genre'].setText(values.get('genre',''))
        self.description.setPlainText(values.get('biography') or values.get('description') or values.get('review') or '')
        for filename,pv in self.previews.items():
            pv.show_file(target/filename if target and (kind=='artist' or filename=='folder.jpg') else None)
            pv.setVisible(kind=='artist' or filename=='folder.jpg')
        self.dirty=False;self.edit_target=target;self.edit_kind=kind
        self.original_values=values;self.loading_fields=False
    def _rename_managed_album_folder(self, folder, title, date):
        """Rename the existing release folder after its naming metadata changes."""
        if not folder or not (folder/'album.nfo').is_file():
            return folder
        destination=self._album_destination(folder.parent,title,date)
        if destination is None or destination.resolve()==folder.resolve():
            return folder
        if destination.exists():
            raise FileExistsError(str(destination))
        folder.rename(destination)
        return destination

    def _album_destination(self, artist_folder, title, date):
        """Use the configured music folder naming pattern, not a hardcoded name."""
        from configuration_store import effective
        from music_track_rename import safe_name
        music=effective(Path(__file__).with_name('Media-Buddy.json')).get('Audio',{}).get('Music',{})
        if not isinstance(music,dict):music={}
        pattern=str(music.get('AlbumFolderPattern','{year} - {album}') or '').strip()
        if not pattern:return None
        year=date[:4] if date else ''
        name=safe_name(pattern.format(album=title,year=year,artist=self.artist or '',
                                      albumartist=self.artist or '',title='',track='',disc=''))
        return artist_folder/name

    def save_nfo(self):
        if not self.dirty:return True
        target,kind=self.edit_target,self.edit_kind
        if not target:return True
        values={'name' if kind=='artist' else 'title':self.fields['name'].text(),
                'genre':self.fields['genre'].text()}
        if kind=='artist':
            values['musicbrainzartistid']=self.fields['musicbrainzartistid'].text()
            values['biography']=self.description.toPlainText()
            for key in ('born','died','formed','disbanded'):
                values[key]=_storage_artist_date(self.fields[key].text())
        else:
            try:date=_storage_date(self.fields['releasedate'].text())
            except ValueError:
                QMessageBox.warning(self,_('Save failed'),_('Invalid release date'));return False
            values.update({'musicbrainzalbumid':self.fields['musicbrainzalbumid'].text(),
                           'musicbrainzreleasegroupid':self.fields['musicbrainzreleasegroupid'].text(),
                           'releasedate':date,'year':date[:4],
                           'description':self.description.toPlainText()})
        try:
            if kind=='album':
                records=list(self._album_records())
                old=self.original_values
                old_title=(old.get('title') or old.get('album') or (self.album_key[2] if self.album_key else '')).strip()
                new_title=values['title'].strip()
                if not new_title:raise ValueError(_('Album title must not be empty.'))
                # A single in the artist root has no release-specific NFO.
                # Never write a shared album.nfo into that root.
                is_loose=(self.root is not None and target.resolve()==Path(self.root).resolve()/ (self.artist or '')
                          and not (target/'album.nfo').exists())
                # Also accept a single artist folder whose name is not identical
                # to its tag (e.g. an artist folder naming pattern).
                if not is_loose and records and not (target/'album.nfo').exists():
                    is_loose=all(r[0].parent.resolve()==target.resolve() for r in records) and \
                             len(records)==1 and target.parent.resolve()==Path(self.root).resolve()
                changed_title=new_title.casefold()!=old_title.casefold()
                if changed_title and is_loose:
                    # The old single release identifiers must not silently become
                    # the identifiers of the newly assembled album.
                    old_release=(old.get('musicbrainzalbumid') or (self.album_key[1] if self.album_key else '')).strip()
                    if values['musicbrainzalbumid'].strip()==old_release:
                        values['musicbrainzalbumid']=''
                    if values['musicbrainzreleasegroupid'].strip()==old.get('musicbrainzreleasegroupid','').strip():
                        values['musicbrainzreleasegroupid']=''
                destination=target
                moves=[]
                if is_loose and changed_title and new_title.casefold() not in ('single','singles',_('Singles').casefold()):
                    destination=self._album_destination(target,new_title,date)
                    if destination is None:
                        raise ValueError(_('Configure an album folder pattern before creating an album.'))
                    if destination.resolve()==target.resolve():
                        raise ValueError(_('Album folder must differ from the artist folder.'))
                    # When the user assigns a loose single to an existing album,
                    # an omitted date must not create a second physical release.
                    # Match the authoritative album.nfo, not a year in the path.
                    candidates=[]
                    for child in target.iterdir():
                        if not child.is_dir() or not (child/'album.nfo').is_file():
                            continue
                        info=_nfo(child/'album.nfo')
                        if (info.get('title') or info.get('album') or '').strip().casefold()==new_title.casefold():
                            candidates.append(child)
                    if len(candidates)==1:
                        destination=candidates[0]
                    elif len(candidates)>1 and not (destination/'album.nfo').is_file():
                        raise ValueError(_('Multiple album editions match this title. Select the release explicitly.'))
                    # Joining an existing album is a normal postprocessing operation.
                    # Reuse its NFO instead of treating it as a file collision.
                    existing_nfo=_nfo(destination/'album.nfo') if (destination/'album.nfo').is_file() else {}
                    if existing_nfo:
                        # The destination album owns its release identifiers and
                        # metadata; do not replace them with the source single's.
                        for field in ('musicbrainzalbumid','musicbrainzreleasegroupid',
                                      'releasedate','year','genre','description'):
                            if existing_nfo.get(field,'').strip():
                                values[field]=existing_nfo[field]
                        date=values.get('releasedate','') or values.get('year','')
                    # Do not combine other singles implicitly; only the selected group.
                    from music_track_rename import move_track_group
                    moves=[]
                    try:
                        for path,artist,album,data in records:
                            moved=move_track_group(path,destination/path.name)
                            moves.append((path,moved))
                    except Exception:
                        for src,dst in reversed(moves):
                            move_track_group(dst,src)
                        raise
                changes={key:values[key].strip() for key in ('musicbrainzalbumid','musicbrainzreleasegroupid','releasedate')
                         if values[key].strip() != (old.get(key,'') or (old.get('year','') if key=='releasedate' else '')).strip()}
                if kind=='album':
                    changes['musicbrainzalbumid']=values['musicbrainzalbumid'].strip()
                    changes['musicbrainzreleasegroupid']=values['musicbrainzreleasegroupid'].strip()
                if changed_title and is_loose:
                    changes['musicbrainzalbumid']=values['musicbrainzalbumid'].strip()
                    changes['musicbrainzreleasegroupid']=values['musicbrainzreleasegroupid'].strip()
                from music_track_postprocess import _read, _write
                for path,artist,album,data in records:
                    current_path=next((dst for src,dst in moves if src==path),path) if is_loose else path
                    if is_loose and moves:
                        tags=_read(current_path)
                        tags.update({'album':new_title,'genre':values['genre'],
                                     'date':date,'musicbrainzalbumid':values['musicbrainzalbumid'],
                                     'musicbrainzreleasegroupid':values['musicbrainzreleasegroupid']})
                        _write(current_path,tags,{})
                    elif changes:
                        _update_album_audio_tags(current_path,changes)
                _write_nfo(destination/'album.nfo',kind,values)
                # A newly known year or changed title also updates the existing
                # physical folder name, not merely its NFO and audio tags.
                if (destination/'album.nfo').is_file() and destination != target or \
                        (destination/'album.nfo').is_file() and not is_loose:
                    destination=self._rename_managed_album_folder(
                        destination,new_title,values.get('releasedate') or values.get('year',''))
                # Some earlier postprocessing paths left a nested artist directory
                # in a newly created album. Only remove it when it contains exact
                # byte-for-byte duplicates of artist files already at artist root.
                if destination != target:
                    nested=destination/(self.artist or '')
                    if nested.is_dir():
                        artist_files={'artist.nfo','folder.jpg','fanart.jpg','banner.jpg','clearlogo.png'}
                        children=list(nested.iterdir())
                        if children and all(f.is_file() and f.name.lower() in artist_files and
                                            (target/f.name).is_file() and
                                            f.read_bytes()==(target/f.name).read_bytes() for f in children):
                            for f in children:f.unlink()
                            nested.rmdir()
                self._reload_album_from_disk()
                self.original_values=dict(values);self.dirty=False
                # Refresh the album list and preserve the new physical album identity.
                if self.mode=='albums':
                    self.refresh()
                    for i in range(self.items.count()):
                        key=self.items.item(i).data(Qt.ItemDataRole.UserRole)
                        if key and Path(key[0]).resolve()==destination.resolve() and key[2]==new_title:
                            self.items.setCurrentRow(i);break
                return True
            _write_nfo(target/(kind+'.nfo'),kind,values)
            self.original_values=dict(values);self.dirty=False
            return True
        except Exception as exc:
            QMessageBox.warning(self,_('Save failed'),str(exc));return False
    def change_image(self,name):
        target,kind=self._target()
        if not target or (kind=='album' and name!='folder.jpg'):return
        src,selected_filter=QFileDialog.getOpenFileName(self,_('Select artwork'),'','Images (*.jpg *.jpeg *.png)')
        if not src:return
        import shutil
        dst=target/name
        try:
            target.mkdir(parents=True,exist_ok=True)
            if Path(src).resolve()!=dst.resolve():
                shutil.copy2(src,dst)
            self.previews[name].show_file(dst)
        except Exception as exc:QMessageBox.warning(self,_('Artwork'),str(exc))
    def _reload_album_from_disk(self):
        """Refresh the album membership and tags before opening its tracks."""
        if not self.root or not self.root.is_dir():return
        from music_track_postprocess import _read, EXTENSIONS
        updated=[]
        for path in self.root.rglob('*'):
            if not path.is_file() or path.suffix.lower() not in EXTENSIONS:continue
            try:data=_read(path)
            except Exception:data={}
            artist=(data.get('albumartist') or data.get('artist') or path.parent.name).strip()
            album=(data.get('album') or '').strip()
            updated.append((path,artist,album,data))
        self.records=sorted(updated,key=lambda r:str(r[0]).casefold())

    def _load_scoped_artist_tracks(self, artist_name):
        """Load existing audio tracks for album navigation, without changing artist scope."""
        from music_track_postprocess import _read, EXTENSIONS
        self.records=[]
        if not self.root or not self.root.is_dir():return
        folder=self._artist_folder(artist_name)
        if not folder or not folder.is_dir():return
        for path in folder.rglob('*'):
            if not path.is_file() or path.suffix.lower() not in EXTENSIONS:continue
            try:data=_read(path)
            except Exception:data={}
            album=(data.get('album') or '').strip()
            self.records.append((path,artist_name,album,data))

    def enter(self,*args):
        if not self.items.currentItem() or (self.dirty and not self.save_nfo()):return
        if self.mode=='artists':
            if self.artist_scope is not None:
                self._load_scoped_artist_tracks(self.items.currentItem().text())
            else:
                self._reload_album_from_disk()
            self.artist=self.items.currentItem().text();self.mode='albums';self.refresh()
        elif self.mode=='albums':
            # Never reuse stale records: files may have been deleted, renamed or
            # retagged externally while the editor was open.
            previous_folder=Path(self.album_key[0]).resolve() if self.album_key else None
            self._reload_album_from_disk()
            if previous_folder:
                matches=[self._record_key(r) for r in self.records
                         if r[1]==self.artist and self._record_album_folder(r[0]).resolve()==previous_folder]
                if matches:self.album_key=matches[0]
            selected=[r[0] for r in self._album_records()]
            if selected:self.open_tracks.emit(selected)
            else:
                self.refresh()
                QMessageBox.information(self,_('Music'),_('No audio tracks found.'))
    def show_album_artists(self):
        if self.dirty and not self.save_nfo():return
        if self.mode=='albums' and self.album:self.mode='track_artists';self.refresh()
    def show_track_artists(self,tracks,albumartist):
        # Direct track editor shortcut: keep the selected track in a temporary album scope.
        self.records=[r for r in self.records if r[0] in tracks] or self.records
        if self.records:
            self.artist=self.records[0][1];self.album=self.records[0][2] or _('Singles');self.album_key=self._record_key(self.records[0])
        self.mode='track_artists';self.refresh()
    def refresh_after_tracks(self):
        """Rescan renamed/retagged tracks when the track editor closes."""
        if self.mode!='albums':
            return
        previous_folder=Path(self.album_key[0]).resolve() if self.album_key else None
        self._reload_album_from_disk()
        self.refresh()
        if previous_folder:
            for row in range(self.items.count()):
                key=self.items.item(row).data(Qt.ItemDataRole.UserRole)
                if key and Path(key[0]).resolve()==previous_folder:
                    self.items.setCurrentRow(row)
                    break

    def go_back(self):
        if self.dirty and not self.save_nfo():return
        if self.mode=='track_artists':self.mode='albums';self.refresh()
        elif self.mode=='albums':self._reload_album_from_disk();self.mode='artists';self.refresh()
        elif self.external_back is not None:self.external_back()
        else:self.owner.pages.setCurrentIndex(0)
    def open_link(self,base):
        if not self.items.currentItem() or self.mode=='albums':return
        from urllib.parse import quote
        QDesktopServices.openUrl(QUrl(base+quote(self.items.currentItem().text(),safe='')))

    def search_album_images(self):
        if self.mode!='albums' or not self.album_key:return
        # The selected release is identified by its album key, not a release group.
        title=self.fields['name'].text().strip() or self.album_key[2]
        query=' '.join(part for part in (self.artist or '',title) if part)
        if query:
            QDesktopServices.openUrl(QUrl('https://www.google.com/search?tbm=isch&q='+quote(query)))

    def open_album_artist_discogs(self):
        if self.mode!='albums' or not self.artist:return
        QDesktopServices.openUrl(QUrl('https://discogs.com/artist/'+quote(self.artist,safe='')))

    def _selected_release_files(self):
        """Restrict mutations to the physical directory AND selected release identity."""
        if self.mode != 'albums' or not self.album_key:
            raise ValueError(_('Select an album first.'))
        folder = self._album_folder()
        if not folder or not folder.is_dir():
            raise ValueError(_('Album folder not found.'))
        records = self._album_records()
        files = sorted({r[0] for r in records if r[0].is_file() and r[0].resolve().is_relative_to(folder.resolve())})
        if not files:
            raise ValueError(_('No audio tracks found.'))
        return folder, files

    def rescrape_selected_album(self):
        """Refresh release-level metadata only; never reassign tracks or editions."""
        try:
            import re
            folder, files = self._selected_release_files()
            nfo = folder / 'album.nfo'
            local = _nfo(nfo)
            album_title=(self.fields['name'].text().strip() or local.get('title') or self.album_key[2]).strip()
            from music_album_scraper import Client
            client=Client()
            matches=client.search(self.artist or '',album_title)
            existing=(local.get('musicbrainzalbumid') or self.album_key[1]).strip()
            choices=[]; by_label={}
            for item in matches:
                rid=str(item.get('id','')).strip()
                if not rid:continue
                label=f"{item.get('title','')} | {item.get('date','')} | {item.get('country','')} | {rid}"
                choices.append(label);by_label[label]=rid
            if existing and existing not in by_label.values():
                label=f"{_('Current release')} | {existing}"
                choices.insert(0,label);by_label[label]=existing
            if not choices:
                raise ValueError(_('No MusicBrainz releases found. Check the album title.'))
            chosen,ok=QInputDialog.getItem(self,_('Select MusicBrainz release'),
                _('Choose the correct release for this album edition:'),choices,0,False)
            if not ok:return
            release_id=by_label[chosen]
            reply=QMessageBox.question(self,_('Rescrape album'),
                _('Apply the selected MusicBrainz release ID to this album and its tracks?')+'\n'+str(folder),
                QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)
            if reply!=QMessageBox.StandardButton.Yes:return
            release=client.release(release_id)
            if not release or str(release.get('id','')).lower()!=release_id.lower():
                raise ValueError(_('The selected MusicBrainz release could not be retrieved.'))
            group = release.get('release-group') or {}
            new_date = release.get('date','') or ''
            # Keep local edition title, description, genre and artwork.
            updates={'musicbrainzalbumid':release_id,
                     'musicbrainzreleasegroupid':group.get('id','') or ''}
            if new_date:
                updates['releasedate']=new_date
                updates['year']=new_date[:4]
            # Persist the selected release before synchronizing its physical tracks.
            _write_nfo(nfo,'album',updates)
            # Cover Art Archive provides release-specific front covers. Artwork
            # failure must not roll back a successful metadata refresh.
            try:
                from PySide6.QtCore import QSettings
                setting=QSettings('BuddyTools','Media-Buddy-V2').value('music_scraper/album_artwork','false')
                artwork_enabled=str(setting).lower()=='true'
                if artwork_enabled and not (folder/'folder.jpg').exists():
                    response=client.session.get('https://coverartarchive.org/release/'+release_id+'/front-500',timeout=(10,25))
                    if response.ok and response.headers.get('Content-Type','').lower().startswith('image/'):
                        (folder/'folder.jpg').write_bytes(response.content)
            except Exception as cover_error:
                print('[WARN] Album cover download:',cover_error,flush=True)
            # Apply the authoritative album.nfo and available cover to every
            # track, while preserving recording IDs, lyrics and track numbering.
            current_album = _nfo(nfo)
            cover_path = folder/'folder.jpg'
            errors = []
            for audio_path in files:
                try:
                    _apply_album_to_track(audio_path, current_album, self.artist or '', cover_path)
                except Exception as tag_error:
                    errors.append(f'{audio_path.name}: {tag_error}')
            # Refresh the physical folder name once the release date is known.
            new_folder=self._rename_managed_album_folder(
                folder,(current_album.get('title') or current_album.get('album') or album_title),
                current_album.get('releasedate') or current_album.get('year',''))
            self._reload_album_from_disk()
            self.refresh()
            for row in range(self.items.count()):
                key=self.items.item(row).data(Qt.ItemDataRole.UserRole)
                if key and Path(key[0]).resolve()==new_folder.resolve():
                    self.items.setCurrentRow(row)
                    break
            if errors:
                QMessageBox.warning(self,_('Rescrape album'),
                    _('Some tracks could not be updated:')+'\n'+'\n'.join(errors))
            QMessageBox.information(self,_('Rescrape album'),
                _('MusicBrainz release IDs updated for the selected album.'))
        except Exception as exc:
            QMessageBox.warning(self,_('Rescrape album'),str(exc))

    def rebuild_selected_album_tags(self):
        """Reapply album-wide metadata to the selected files without deleting track IDs."""
        try:
            folder, files = self._selected_release_files()
            nfo = _nfo(folder/'album.nfo')
            if not nfo:
                raise ValueError(_('No album.nfo found. Save album metadata first.'))
            reply=QMessageBox.question(self,_('Rebuild tags'),
                _('Rewrite album-level tags for the selected release? Track titles, track numbers, disc numbers, recording IDs, lyrics and embedded artwork will be preserved.') + '\n' + str(folder),
                QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)
            if reply!=QMessageBox.StandardButton.Yes:return
            from music_track_postprocess import _read, _write
            from datetime import datetime
            logdir=Path(__file__).resolve().parent/'Log'
            logdir.mkdir(exist_ok=True)
            logfile=logdir/('album_tags_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'.log')
            successes=0;errors=[]
            # Deliberately use non-destructive tag updating: a destructive clean
            # would erase recording IDs and other custom metadata.
            with logfile.open('w',encoding='utf-8') as log:
                log.write('Album: '+str(folder)+'\nRelease ID: '+str(self.album_key[1])+'\n')
                for file in files:
                    try:
                        _apply_album_to_track(file,nfo,self.artist or '',folder/'folder.jpg')
                        successes+=1;log.write('OK '+str(file)+'\n')
                    except Exception as exc:
                        errors.append(file.name+': '+str(exc));log.write('ERROR '+str(file)+': '+str(exc)+'\n')
                log.write(f'Result: {successes} OK, {len(errors)} errors\n')
            self._reload_album_from_disk()
            self.select(self.items.currentRow())
            QMessageBox.information(self,_('Rebuild tags'),
                f'{successes} '+_('tracks updated')+f', {len(errors)} '+_('errors')+'\n'+str(logfile))
        except Exception as exc:
            QMessageBox.warning(self,_('Rebuild tags'),str(exc))
