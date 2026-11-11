"""Test 154a: correct artwork-to-preview mapping."""
from __future__ import annotations
from gui_i18n import _
from postprocess_save_guard import can_autosave, report_save_failure

from postprocess_entry import make_entry
import os
from pathlib import Path
import xml.etree.ElementTree as ET
from PySide6.QtCore import Qt, QSettings, QThread, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QGridLayout,QLabel,QPushButton,QFileDialog,QListWidget,QLineEdit,QTextEdit,QStackedWidget,QGroupBox,QScrollArea,QMessageBox,QSplitter,QSizePolicy)
from series_postprocess import ArtworkPreview,media_patterns,is_media
from date_normalizer import normalize_date,InvalidDate
from linked_media_walk import linked_files

FIELDS=[('title',_('Title')),('sorttitle',_('Sort title')),('originaltitle',_('Original title')),('tagline',_('Tagline')),('genre',_('Genre')),('studio',_('Studio')),('country',_('Country')),('premiered',_('Released')),('rating',_('Rating')),('mpaa',_('Content rating')),('status',_('Status')),('year',_('Year')),('director',_('Director')),('credits',_('Writer'))]
FILM_ART=[('banner.jpg', _('Banner')),('fanart.jpg', _('Fanart')),('landscape.jpg', _('Landscape')),('clearart.png', _('Clearart')),('clearlogo.png', _('Clearlogo')),('discart.png', _('Discart')),('poster.jpg', _('Poster')),('keyart.jpg', _('Keyart'))]
SET_ART=[('poster.jpg', _('Poster')),('fanart.jpg', _('Fanart')),('banner.jpg', _('Banner')),('clearlogo.png', _('Clearlogo')),('clearart.png', _('Clearart')),('landscape.jpg', _('Landscape')),('keyart.jpg', _('Keyart'))]

def scan_movies(root):
    patterns=media_patterns()
    found=[]
    for path in linked_files(root):
        if path.is_file() and is_media(path,patterns) and not path.stem.lower().endswith('-trailer'):
            nfo=path.with_suffix('.nfo')
            if not nfo.is_file() and (path.parent/'movie.nfo').is_file(): nfo=path.parent/'movie.nfo'
            if nfo.is_file(): found.append((path,nfo))
    return sorted(found,key=lambda item:item[0].stem.casefold())

def parse_nfo(path):
    from movie_scraper import read_movie_nfo_tolerant
    tree=read_movie_nfo_tolerant(path)
    if tree.getroot().tag!='movie': raise ValueError(_('No movie NFO: {path}').format(path=path))
    return tree

class TagWorker(QThread):
    done=Signal(str)
    def __init__(self,video,tool,parent=None): super().__init__(parent); self.video=video; self.tool=tool
    def run(self):
        from matroska_tagger import tag_movies
        try:
            result=tag_movies(self.video,mkvpropedit=self.tool)
            self.done.emit(_('Retagging: {status} – {message}').format(status=result.status, message=result.message))
        except Exception as exc: self.done.emit(_('Retagging failed: {exc}').format(exc=exc))

class FilmArtworkPreview(ArtworkPreview):
    """Transparent, type-aware image preview without letterboxing."""
    def __init__(self, filename, title, parent=None):
        super().__init__(filename, title, parent)
        self.image.setStyleSheet('QPushButton { background: transparent; border: none; color: #666; padding: 0px; }')
        self.image.setMinimumSize(0, 0)
        self.image.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)

    def refresh_icon(self):
        if self.pixmap.isNull():
            self.image.setIcon(QIcon())
            self.image.setText(_('Artwork missing – click to select'))
            return
        self.image.setText('')
        size = self.image.size()
        if size.width() <= 2 or size.height() <= 2:
            return
        # Fanart, banner and logos fill the assigned rectangle, intentionally
        # stretching malformed sources so incorrect artwork is obvious.
        stretch = self.original_filename in ('fanart.jpg', 'clearart.png', 'clearlogo.png')
        scaled = self.pixmap.scaled(size, (Qt.AspectRatioMode.IgnoreAspectRatio if stretch
                                         else Qt.AspectRatioMode.KeepAspectRatio),
                                    Qt.TransformationMode.SmoothTransformation)
        self.image.setIcon(QIcon(scaled))
        self.image.setIconSize(scaled.size())


class FilmArtworkPage(QWidget):
    """Geometry-driven preview grid: image dimensions cannot push widgets away."""
    def __init__(self, defs, store, is_set, parent=None):
        super().__init__(parent)
        self.is_set = is_set
        self.previews = {}
        for filename, title in defs:
            if filename == 'poster.jpg':
                continue
            preview = FilmArtworkPreview(filename, title, self)
            preview.original_filename = filename
            store.append(preview)
            self.previews[filename] = preview
        self.setMinimumSize(0, 0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        w, h = self.width(), self.height()
        if w < 20 or h < 20:
            return
        gap = 7
        # Geometry must be based on *width* first. Never stretch a correct
        # 16:9 fanart vertically just because the window became taller.
        fan_zone_w = round((w-gap)*0.70)
        logo_w = w-gap-fan_zone_w
        banner = self.previews.get('banner.jpg')
        source = banner.pixmap if banner is not None else None
        if source is not None and not source.isNull() and source.width() > 0:
            banner_ratio = source.height()/source.width()
        else:
            banner_ratio = 0.18  # Only a fallback for missing banner artwork.
        natural_banner_h = round(w*banner_ratio)
        # Preserve space for the 16:9 fanart and the bottom row in short windows.
        bottom_min = 60
        mid_min = 55
        banner_h = max(18, min(natural_banner_h, h-bottom_min-mid_min-2*gap))
        available = max(1,h-banner_h-2*gap)
        # The bottom row has priority for the remainder, but the fanart never
        # grows beyond the 16:9 height implied by its actual displayed width.
        mid_h = min(round(fan_zone_w*9/16), max(mid_min,available-round(h*0.28)))
        mid_h = min(mid_h, max(1,available-bottom_min))
        bottom_h = max(1,available-mid_h)
        # If the window is too short, reduce the fanart width instead of
        # stretching it or clipping it. Center it in its allocated zone.
        fan_w = min(fan_zone_w, round(mid_h*16/9))
        mid_h = min(mid_h,round(fan_w*9/16))
        bottom_h = max(1,available-mid_h)
        fan_x = 0  # Align fanart with the left edge of landscape.
        bottom_y = banner_h+gap+mid_h+gap
        def place(name,x,y,width,height):
            art=self.previews.get(name)
            if art is not None:
                art.setGeometry(int(x),int(y),max(1,int(width)),max(1,int(height)))
                art.refresh_icon()
        place('banner.jpg',0,0,w,banner_h)
        mid_y=banner_h+gap
        place('fanart.jpg',fan_x,mid_y,fan_w,mid_h)
        # Shared proportional fitting. No dark letterboxing or distorted artwork.
        def fit(name, x, y, zone_w, zone_h, side='left'):
            art = self.previews.get(name)
            if art is None:
                return
            pix = art.pixmap
            if pix.isNull() or pix.width() < 1 or pix.height() < 1:
                place(name, x, y, zone_w, zone_h)
                return
            ratio = pix.width() / pix.height()
            width = min(zone_w, max(1, round(zone_h * ratio)))
            height = min(zone_h, max(1, round(width / ratio)))
            if side == 'right':
                actual_x = x + zone_w - width
            elif side == 'center':
                actual_x = x + (zone_w - width) // 2
            else:
                actual_x = x
            place(name, actual_x, y + (zone_h-height)//2, width, height)

        if self.is_set:
            # Moviesets have no discart. Keep banner, fanart and poster at the
            # same positions as on the movie artwork page. Keyart takes the
            # upper-right position, logos share the freed lower-right space.
            fit('keyart.jpg', fan_zone_w+gap, mid_y, logo_w, mid_h, 'right')
            bottom_left_w = fan_zone_w
            fit('landscape.jpg', 0, bottom_y, bottom_left_w, bottom_h, 'left')
            logo_x = fan_zone_w + gap
            logo_bottom_h = max(1, bottom_h)
            clear_h = max(1, round((logo_bottom_h-gap)*0.57))
            place('clearart.png', logo_x, bottom_y, logo_w, clear_h)
            place('clearlogo.png', logo_x, bottom_y+clear_h+gap,
                  logo_w, max(1, logo_bottom_h-clear_h-gap))
        else:
            # Existing Test 153e layout remains completely unchanged.
            clear_h=round((mid_h-gap)*0.57)
            place('clearart.png',fan_zone_w+gap,mid_y,logo_w,clear_h)
            place('clearlogo.png',fan_zone_w+gap,mid_y+clear_h+gap,logo_w,mid_h-clear_h-gap)
            left_w = round((w-2*gap)*0.50)
            other_w = (w-2*gap-left_w)//2
            fit('landscape.jpg', 0, bottom_y, left_w, bottom_h, 'left')
            fit('keyart.jpg', left_w+gap, bottom_y, other_w, bottom_h, 'center')
            fit('discart.png', left_w+gap+other_w+gap, bottom_y,
                w-(left_w+other_w+2*gap), bottom_h, 'right')


class MoviePostprocess(QWidget):
    def __init__(self,parent=None):
        super().__init__(parent); self.movies=[]; self.current=None; self.tree=None; self.loading=False; self.worker=None
        root=QVBoxLayout(self); root.setContentsMargins(5,5,5,5)
        self.pages=QStackedWidget(); root.addWidget(self.pages)
        self.folder_edit=QLineEdit(QSettings('BuddyTools','Media-Buddy-V2').value('postprocess/movies_folder',''))
        entry=make_entry(self.folder_edit,self.browse,self.open_folder,_('Post-processing: Movies'),_('Movie folder'))
        self.pages.addWidget(entry)
        edit=QWidget(); layout=QVBoxLayout(edit);layout.setContentsMargins(0,0,0,0)
        split=QSplitter(Qt.Orientation.Horizontal);layout.addWidget(split,1)
        self.list=QListWidget();self.list.setMinimumWidth(190);split.addWidget(self.list)
        right=QWidget();rl=QVBoxLayout(right);rl.setContentsMargins(4,0,4,0)
        # Independent editor and poster/action rail: form may use full height.
        content=QHBoxLayout();content.setContentsMargins(0,0,0,0);content.setSpacing(10)
        self.views=QStackedWidget();content.addWidget(self.views,1)
        self.inputs={};self.id_inputs={};self.art=[];self.set_art=[]
        self._poster_cache={}
        self.views.addWidget(self._metadata())
        self.views.addWidget(self._artpage(FILM_ART,self.art,False))
        self.views.addWidget(self._artpage(SET_ART,self.set_art,True))
        rail=QWidget();rail.setMinimumWidth(195);rail.setMaximumWidth(290)
        rail_layout=QVBoxLayout(rail);rail_layout.setContentsMargins(0,0,0,0);rail_layout.setSpacing(7)
        self.metadata_poster=ArtworkPreview('poster.jpg', _('Poster'))
        self.metadata_poster.keep_aspect=True
        self.metadata_poster.image.setStyleSheet('QPushButton { background: transparent; border: none; color: #666; }')
        rail_layout.addWidget(self.metadata_poster,1)
        nav=QGridLayout();nav.setContentsMargins(0,0,0,0);nav.setSpacing(4)
        for i,name in enumerate((_('Metadata'),_('Fanart'),_('Movie collection'))):
            b=QPushButton(name);b.setMinimumWidth(0)
            b.clicked.connect(lambda checked=False,index=i:self.change_view(index));nav.addWidget(b,0,i)
        rail_layout.addLayout(nav)
        actions=QGridLayout();actions.setContentsMargins(0,0,0,0);actions.setSpacing(4)
        self.tag=QPushButton(_('Retag'));self.tag.clicked.connect(self.retag);actions.addWidget(self.tag,0,0)
        play=QPushButton(_('Play'));play.clicked.connect(self.play);actions.addWidget(play,0,1)
        back=QPushButton(_('Back'));back.clicked.connect(self.leave);actions.addWidget(back,0,2)
        for button in (self.tag,play,back):button.setMinimumWidth(0)
        rail_layout.addLayout(actions)
        content.addWidget(rail,0)
        rl.addLayout(content,1)
        split.addWidget(right);split.setSizes([250,750])
        self.pages.addWidget(edit);self.list.currentRowChanged.connect(self.switch_movie)
    def _metadata(self):
        """Compact asymmetrical two-column form; plot uses remaining editor height."""
        page=QWidget()
        vertical=QVBoxLayout(page)
        vertical.setContentsMargins(2,2,0,2)
        vertical.setSpacing(5)
        columns=QHBoxLayout();columns.setSpacing(10)
        left=QWidget();left_grid=QGridLayout(left)
        right=QWidget();right_grid=QGridLayout(right)
        for grid in (left_grid,right_grid):
            grid.setContentsMargins(0,0,0,0)
            grid.setHorizontalSpacing(5)
            grid.setVerticalSpacing(5)
            grid.setColumnStretch(1,1)
        long_fields=[('title',_('Title')),('sorttitle',_('Sort title')),
                     ('originaltitle',_('Original title')),('tagline',_('Tagline')),
                     ('set_name',_('Movie collection')),('genre',_('Genre')),('studio',_('Studio'))]
        short_fields=[('imdb','IMDb-ID'),('tmdb','TMDB-ID'),
                      ('mpaa',_('Content rating')),('country',_('Country')),
                      ('premiered',_('Released')),('rating',_('Rating')),
                      ('status',_('Status')),('year',_('Year'))]
        self.inputs={};self.id_inputs={}
        for grid,items in ((left_grid,long_fields),(right_grid,short_fields)):
            for row,(key,title) in enumerate(items):
                field=QLineEdit();field.setMinimumWidth(0);field.setFixedHeight(26)
                (self.id_inputs if key in ('imdb','tmdb','set_name') else self.inputs)[key]=field
                label=QLabel(title);label.setMinimumWidth(0)
                grid.addWidget(label,row,0)
                grid.addWidget(field,row,1)
        columns.addWidget(left,6);columns.addWidget(right,4)
        vertical.addLayout(columns)
        vertical.addWidget(QLabel(_('Description')))
        self.plot=QTextEdit();self.plot.setMinimumHeight(95)
        vertical.addWidget(self.plot,3)
        vertical.addWidget(QLabel(_('Collection description')))
        self.set_plot=QTextEdit();self.set_plot.setMinimumHeight(50)
        vertical.addWidget(self.set_plot,1)
        return page
    def _artpage(self, defs, store, is_set):
        return FilmArtworkPage(defs, store, is_set)

    def browse(self):
        folder=QFileDialog.getExistingDirectory(self,_('Select movie folder'),self.folder_edit.text())
        if folder:self.folder_edit.setText(folder)
    def report(self,message):
        host=self.window()
        if hasattr(host,'set_work_status'):host.set_work_status('phase',message)
    def open_folder(self,folder_override=None):
        if isinstance(folder_override,bool):folder_override=None
        if not self.save_current():return
        folder=Path(folder_override if folder_override is not None else self.folder_edit.text().strip())
        if not folder.is_dir():QMessageBox.warning(self,_('Movies'),_('The selected folder does not exist.'));return
        self.folder_edit.setText(str(folder));QSettings('BuddyTools','Media-Buddy-V2').setValue('postprocess/movies_folder',str(folder))
        self.movies=scan_movies(folder);self.current=None;self.tree=None;self.loading=True
        self.list.clear();self.list.addItems([v.stem for v,n in self.movies]);self.loading=False
        self.pages.setCurrentIndex(1);self.list.setCurrentRow(0 if self.movies else -1)
        if not self.movies:self.report(_('No movies with an NFO were found.'))
    def _set_folder(self):
        if self.tree is None:return None
        root=self.tree.getroot();name=(root.findtext('set/name') or '').strip()
        if not name:return None
        try:
            from configuration_store import effective
            from movie_scraper import movieset_base,movieset_location
            cfg=effective(Path(__file__).resolve().parent/'movie_scraper_config.json')
            base=movieset_base(cfg)
            if base is None:return None
            loc=movieset_location(base,name,str(cfg.get('ReplaceChar','#')))
            if loc[0]=='local':return loc[1]
            self.report(_('Movie collection artwork over SMB is currently unavailable in the editor on Linux.'))
        except Exception as exc:self.report(_('Movie collection artwork: {exc}').format(exc=exc))
        return None
    def _refresh_art(self):
        from configuration_store import effective
        from movie_scraper import ARTWORK_SPECS, MOVIESET_SPECS, artwork_target
        from artwork_naming import names_for
        cfg=effective(Path(__file__).resolve().parent/'movie_scraper_config.json')
        names=cfg.get('Filenaming',{}).get('Movie',{})
        if self.current:
            video=self.current[0]
            folder=video.parent
            media_count=sum(1 for x in folder.iterdir() if x.is_file() and is_media(x,media_patterns()))
            for art in self.art:
                kind=art.original_filename.split('.')[0]
                spec=ARTWORK_SPECS.get(kind)
                if spec is None:continue
                switch,name_key,default,_=spec
                candidates=[]
                for name in [*names_for(kind),str(names.get(name_key,default)),default]:
                    if name and name not in candidates:candidates.append(name)
                targets=[]
                for name in candidates:
                    # The scraper supports both one-film-per-folder and shared-folder mode.
                    for mode in (True,False):
                        target=artwork_target(video,kind,name,mode)
                        if target not in targets:targets.append(target)
                selected=next((x for x in targets if x.is_file()),None)
                if selected is None:
                    selected=artwork_target(video,kind,candidates[0],media_count==1)
                art.filename=selected.name
                art.display(selected.parent)
        else:
            for art in self.art:art.display(None)
        # Das Filmplakat ist kein Teil der wechselnden Seiten.
        # Es wird einmal im rechten Bereich geladen und für Seitenwechsel behalten.
        if self.current:
            video=self.current[0]
            spec=ARTWORK_SPECS.get('poster')
            configured=str(names.get(spec[1],spec[2])) if spec else 'poster.jpg'
            options=list(dict.fromkeys([*names_for('poster'),configured,'poster.jpg']))
            targets=[]
            for name in options:
                for mode in (True,False):
                    target=artwork_target(video,'poster',name,mode)
                    if target not in targets:targets.append(target)
            selected=next((x for x in targets if x.is_file()),targets[0])
            self.metadata_poster.filename=selected.name
            self.metadata_poster.display(selected.parent)
        else:
            self.metadata_poster.display(None)
        self._film_poster=(self.metadata_poster.filename,self.metadata_poster.folder,self.metadata_poster.pixmap)

        sf=self._set_folder()
        for art in self.set_art:
            kind=art.original_filename.split('.')[0]
            spec=MOVIESET_SPECS.get(kind)
            if spec:
                switch,default,_=spec
                configured=str(names.get(switch,default))
                options=list(dict.fromkeys([configured,default]))
                chosen=next((n for n in options if sf and (sf/n).is_file()),options[0])
                art.filename=chosen
            art.display(sf)
    def switch_movie(self,index):
        if self.loading:return
        if not self.save_current():return
        self.current=self.movies[index] if 0<=index<len(self.movies) else None;self.tree=None
        self._poster_cache.clear()
        for field in list(self.inputs.values())+list(self.id_inputs.values()):field.clear()
        self.plot.clear();self.set_plot.clear()
        if self.current:
            try:
                self.tree=parse_nfo(self.current[1]);root=self.tree.getroot()
                for key,field in self.inputs.items():field.setText(root.findtext(key) or '')
                self.plot.setPlainText(root.findtext('plot') or '')
                self.set_plot.setPlainText(root.findtext('set/overview') or '')
                self.id_inputs['set_name'].setText(root.findtext('set/name') or '')
                for el in root.findall('uniqueid'):
                    kind=el.get('type','').lower()
                    if kind in self.id_inputs:self.id_inputs[kind].setText(el.text or '')
                self.report(_('Movie loaded: {filename}').format(filename=self.current[0].name))
            except (OSError,ValueError,ET.ParseError) as exc:
                QMessageBox.warning(self,_('Movie NFO'),str(exc))
        self._refresh_art()
        if self.views.currentIndex()==2:self.change_view(2)
    def save_current(self):
        if self.current is None or self.tree is None:return True
        root=self.tree.getroot()
        try:
            for key,field in self.inputs.items():
                value=field.text()
                if key=='premiered' and value!=(root.findtext(key) or ''):
                    try:value=normalize_date(value)
                    except InvalidDate as exc:
                        self.report(_('Invalid date, existing value preserved: {exc}').format(exc=exc))
                        value=root.findtext(key) or ''
                nodes=root.findall(key)
                if nodes: nodes[0].text=value
                elif value:ET.SubElement(root,key).text=value
            for key,value in (('plot',self.plot.toPlainText()),('set/name',self.id_inputs['set_name'].text()),('set/overview',self.set_plot.toPlainText())):
                node=root.find(key)
                if node is None:
                    if key.startswith('set/'):
                        parent=root.find('set')
                        if parent is None:parent=ET.SubElement(root,'set')
                        node=ET.SubElement(parent,key.split('/')[1])
                    else:node=ET.SubElement(root,key)
                node.text=value
            for kind in ('imdb','tmdb'):
                value=self.id_inputs[kind].text().strip()
                node=next((e for e in root.findall('uniqueid') if e.get('type','').lower()==kind),None)
                if node is not None:node.text=value
                elif value:ET.SubElement(root,'uniqueid',{'type':kind}).text=value
            path=self.current[1]
            if not can_autosave(self, path, self.report):return True
            temp=path.with_suffix('.nfo.tmp')
            self.tree.write(temp,encoding='utf-8',xml_declaration=True)
            os.replace(temp,path);return True
        except (OSError,ValueError) as exc:
            report_save_failure(self, self.current[1], exc, _('Could not save movie NFO'), self.report);return True
    def change_view(self,index):
        if not self.save_current():return
        self.views.setCurrentIndex(index)
        # Beim Ansichtswechsel wird dieselbe Poster-Instanz benutzt.
        # Nur die bereits eingelesenen Pixmaps werden ausgetauscht.
        if index==2:
            poster=next((a for a in self.set_art if a.original_filename=='poster.jpg'),None)
            if poster is None:
                # Die Movieset-Posterdatei liegt nicht als separates Widget vor.
                folder=self._set_folder()
                from PySide6.QtGui import QPixmap
                from configuration_store import effective
                from movie_scraper import MOVIESET_SPECS
                cfg=effective(Path(__file__).resolve().parent/'movie_scraper_config.json')
                names=cfg.get('Filenaming',{}).get('Movie',{})
                spec=MOVIESET_SPECS.get('poster')
                configured=str(names.get(spec[0],spec[1])) if spec else 'poster.jpg'
                options=list(dict.fromkeys([configured,'poster.jpg']))
                path=next((folder/n for n in options if folder and (folder/n).is_file()),None)
                key=str(path) if path else ''
                if key not in self._poster_cache:
                    self._poster_cache[key]=QPixmap(key) if key else QPixmap()
                pix=self._poster_cache[key]
                self.metadata_poster.filename=path.name if path else configured
                self.metadata_poster.folder=folder
                self.metadata_poster.pixmap=pix
                self.metadata_poster.refresh_icon()
        elif hasattr(self,'_film_poster'):
            name,folder,pix=self._film_poster
            self.metadata_poster.filename=name
            self.metadata_poster.folder=folder
            self.metadata_poster.pixmap=pix
            self.metadata_poster.refresh_icon()
    def retag(self):
        if not self.current or not self.save_current():return
        if self.current[0].suffix.lower() != '.mkv':
            self.report(_('Retagging: Only Matroska files can be tagged.'))
            return
        from tool_manager import get_tool
        try: executable=get_tool('mkvpropedit')
        except (OSError,ValueError,FileNotFoundError) as exc:
            QMessageBox.warning(self,_('Retag'),str(exc));return
        self.worker=TagWorker(self.current[0],executable,self);self.worker.done.connect(self.tag_done)
        self.tag.setEnabled(False);self.worker.start();self.report(_('Retagging …'))
    def tag_done(self,message):self.tag.setEnabled(True);self.report(message)
    def play(self):
        if not self.current:return
        video=self.current[0]
        if video.suffix.lower() in ('.stub','.disc'):
            QMessageBox.information(self,_('Play'),_('Placeholder files cannot be played.'));return
        try:
            import subprocess
            from tool_manager import get_tool
            subprocess.Popen([get_tool('mpv'),str(video)])
        except (OSError,ValueError) as exc:QMessageBox.warning(self,_('Play'),str(exc))
    def leave(self):
        if self.save_current():self.pages.setCurrentIndex(0)
