"""Test 125: non-destructive editor for established video configuration."""
import copy
import json
from pathlib import Path
from PySide6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QFormLayout,QStackedWidget,QGroupBox,
    QLabel,QLineEdit,QCheckBox,QComboBox,QPlainTextEdit,QPushButton,QMessageBox,QScrollArea,QSpinBox,
    QTableWidget,QTableWidgetItem,QAbstractItemView,QFileDialog,QListWidget)
from batch_settings import read_settings,write_settings
from tool_manager import TOOL_NAMES, find_tool, tool_version
from configuration_store import effective, update_values, load_raw
from configuration_bootstrap import ensure_configuration
from gui_i18n import _, stage_label

ROOT=Path(__file__).resolve().parent
CONFIG=ROOT/'Media-Buddy.json'

class CodecPriorityEditor(QWidget):
    """Ordered codec keys; keeps unknown existing keys untouched."""
    def __init__(self, values, parent=None):
        super().__init__(parent)
        row=QHBoxLayout(self); row.setContentsMargins(0,0,0,0)
        self.items=QListWidget(); self.items.setMinimumHeight(205)
        self.items.addItems([str(v) for v in values]); row.addWidget(self.items,1)
        buttons=QVBoxLayout()
        up=QPushButton(_('Move up')); down=QPushButton(_('Move down'))
        up.clicked.connect(lambda:self._move(-1)); down.clicked.connect(lambda:self._move(1))
        buttons.addWidget(up); buttons.addWidget(down); buttons.addStretch()
        row.addLayout(buttons)
    def _move(self, delta):
        current=self.items.currentRow(); target=current+delta
        if current<0 or not 0<=target<self.items.count(): return
        item=self.items.takeItem(current); self.items.insertItem(target,item)
        self.items.setCurrentRow(target)
    def values(self):
        return [self.items.item(i).text() for i in range(self.items.count())]

class GlobalSettingsPage(QWidget):
    def __init__(self,parent=None):
        super().__init__(parent)
        self.controls=[]
        self.initial_values={}
        self.extra_initial_values={}
        self.extra_controls=[]
        self.preset_checks={}
        outer=QVBoxLayout(self)
        self.pages=QStackedWidget()
        outer.addWidget(self.pages)
        try:
            ensure_configuration(ROOT)
            self.data=effective(CONFIG)
        except Exception as exc:
            self.data={}; QMessageBox.warning(self,_('Configuration'),str(exc))
        self._assistants()
        self._video()
        self._metadata()
        self._filenames()
        self._audio()
        self._tools()
        page=QWidget()
        l=QVBoxLayout(page)
        l.addWidget(QLabel(_('This section will follow after video processing is complete.')))
        l.addStretch()
        self.pages.addWidget(page)
        self.initial_preset_checks={name:check.isChecked() for name,check in self.preset_checks.items()}
        self.initial_rating_enabled=self.rating_enabled.isChecked()
        self.initial_ratings=copy.deepcopy(self._rating_values())
        row=QHBoxLayout(); row.addStretch()
        save=QPushButton(_('Save settings'));save.clicked.connect(self.save);row.addWidget(save)
        outer.addLayout(row)

    def _tools(self):
        layout=self._tab('Tools')
        layout.addWidget(QLabel(_('Custom paths take precedence. Empty = automatic detection.')))
        layout.addWidget(QLabel(_('Existing Windows paths are preserved on Linux; PATH fallback is available.')))
        form=self._group(layout,_('External programs'))
        self.tool_rows=[]
        for name in TOOL_NAMES:
            configured=self._get(('Tools',name),'')
            line=QLineEdit(str(configured or ''))
            key=('Tools',name)
            self.controls.append((key,line.text))
            self.initial_values[key]=line.text()
            browse=QPushButton(_('Choose file'))
            browse.clicked.connect(lambda checked=False, edit=line: self._browse_tool(edit))
            status=QLabel('')
            status.setWordWrap(True)
            wrapper=QWidget(); column=QVBoxLayout(wrapper);column.setContentsMargins(0,0,0,0)
            row=QHBoxLayout();row.addWidget(line,1);row.addWidget(browse);column.addLayout(row)
            column.addWidget(status)
            form.addRow(name,wrapper)
            self.tool_rows.append((name,line,status))
        refresh=QPushButton(_('Check tools'))
        refresh.clicked.connect(self._refresh_tools)
        layout.addWidget(refresh)
        layout.addStretch()
        self._refresh_tools()

    def _browse_tool(self, edit):
        selected, selected_filter = QFileDialog.getOpenFileName(
            self, _('Select program'), edit.text())
        if selected:
            edit.setText(selected)
            self._refresh_tools()

    def _refresh_tools(self):
        for name,line,status in self.tool_rows:
            value=line.text().strip()
            path,source=find_tool(name,value)
            if path:
                # Show auto-detected paths directly, but never replace an explicit entry.
                if not line.text().strip():
                    line.setText(path)
                status.setText(f'{stage_label(source)}: {path}\n{tool_version(name,path)}')
            else:
                status.setText(f"{_('Unavailable')}: {stage_label(source)}")

    def _audio(self):
        layout=self._tab('Audio')
        form=self._group(layout,_('Artist artwork'))
        field=self._field(form,_('Artist artwork – root directory'),
                          ('Audio','ArtistArtworkPath'),'text','')
        row=QHBoxLayout()
        browse=QPushButton(_('Select directory'))
        browse.clicked.connect(lambda: self._browse_artist_artwork(field))
        row.addWidget(browse)
        row.addStretch()
        layout.addLayout(row)
        layout.addWidget(QLabel(_('Shared folder for music videos and future music management.')))
        layout.addStretch()

    def _browse_artist_artwork(self,field):
        selected=QFileDialog.getExistingDirectory(
            self,_('Artist artwork – root directory'),field.text())
        if selected:
            field.setText(selected)

    def _tab(self,name):
        scroll=QScrollArea();scroll.setWidgetResizable(True)
        page=QWidget(); layout=QVBoxLayout(page);layout.setContentsMargins(12,12,12,12)
        scroll.setWidget(page);self.pages.addWidget(scroll);return layout

    def _group(self,layout,name):
        group=QGroupBox(name);form=QFormLayout(group);layout.addWidget(group);return form

    def _get(self,path,default=None):
        val=self.data
        for part in path:
            if not isinstance(val,dict):return default
            val=val.get(part,default)
        return val

    def _field(self,form,label,path,kind='text',default=None,choices=None):
        value=self._get(path,default)
        if kind=='bool':
            widget=QCheckBox();widget.setChecked(bool(value));reader=widget.isChecked
        elif kind=='priority':
            widget=CodecPriorityEditor(value or []);reader=widget.values
        elif kind=='list':
            widget=QPlainTextEdit();widget.setMaximumHeight(90)
            widget.setPlainText('\n'.join(map(str,value or [])))
            reader=lambda w=widget:[s.strip() for s in w.toPlainText().splitlines() if s.strip()]
        elif kind=='choice':
            widget=QComboBox()
            for caption,key in choices:widget.addItem(caption,key)
            ix=widget.findData(value)
            if ix<0 and value is not None:widget.addItem(str(value),value);ix=widget.count()-1
            widget.setCurrentIndex(max(0,ix));reader=widget.currentData
        elif kind=='int':
            widget=QSpinBox();widget.setRange(0,1000000);widget.setValue(int(value or 0));reader=widget.value
        else:
            widget=QLineEdit(str(value if value is not None else ''));reader=widget.text
        form.addRow(label,widget)
        self.controls.append((path,reader))
        self.initial_values[path]=copy.deepcopy(reader())
        return widget

    def _assistants(self):
        layout=self._tab('Assistenten')
        form=self._group(layout,_('Wizard options'))
        self._field(form,_('Always encode when bitrate is 0'),('VideoSettings','RecodeIfBitrateZero'),'bool',False)
        self._field(form,_('Encode all except listed video codecs'),('VideoSettings','VideoWhitelistEnabled'),'bool',False)
        self._field(form,_('Allowed video codecs (one per line)'),('VideoSettings','VideoCodecWhitelist'),'list',[])
        self._field(form,_('Encode all except listed audio codecs'),('Recode','Audio','WhitelistEnabled'),'bool',False)
        self._field(form,_('Allowed audio codecs (one per line)'),('Recode','Audio','CodecWhitelist'),'list',[])
        layout.addWidget(QLabel(_('Audio wizard settings will follow with audio processing.')))
        layout.addStretch()

    def _video(self):
        layout=self._tab('Video')
        form=self._group(layout,_('File types'))
        extensions=self._get(('FileExtensions',),[])
        stubs=self._get(('VideoSettings','StubExtensions'),[x for x in extensions if x.lower().endswith(('.stub','.disc'))])
        videos=[x for x in extensions if x not in stubs]
        self.video_extensions=self._field(form,_('Video file extensions (one per line)'),('VideoSettings','VideoExtensions'),'list',videos)
        self.stub_extensions=self._field(form,_('Stub file extensions (one per line)'),('VideoSettings','StubExtensions'),'list',stubs)
        self._field(form,_('Output container'),('VideoSettings','OutputContainer'),'choice','mkv',
                    [('Matroska (.mkv)','mkv'),('MPEG-4 (.mp4)','mp4'),('MPEG-TS (.ts)','ts')])
        form=self._group(layout,_('Audio in video files'))
        self._field(form,_('Fallback audio codec'),('Recode','Audio','FallbackCodec'),'text','eac3')
        form=self._group(layout,_('Encoder'))
        self._field(form,_('Encoder backend'),('Recode','EncoderBackend'),'choice','auto',
                    [(_('Automatic'),'auto'),(_('Software'),'software'),('Intel Quick Sync','qsv'),('NVIDIA NVENC','nvenc'),('AMD AMF','amf')])
        self._field(form,_('Use hardware encoding'),('Recode','HardwareEncoding'),'bool',True)
        self._field(form,_('Software fallback'),('Recode','SoftwareFallback'),'bool',True)
        self._field(form,_('Automatically reduce quality level when needed'),('Recode','AutoQualityStepDown'),'bool',False)
        self._field(form,_('Allow Dolby Vision profile 7 to 8.1 conversion when re-encoding'),('Recode','DolbyVision','AllowProfile7To81Recode'),'bool',False)
        self._field(form,_('Halve 4K frame rate when needed'),('Recode','Half4KFramerate','Enabled'),'bool',False)
        form=self._group(layout,_('Optimizer'))
        self._field(form,_('Channels first, then codec'),('Optimizer','Audio','Priority'),'choice','channels',
                    [(_('Channels first'),'channels'),(_('Codec first'),'codec')])
        self._field(form,_('Audio codec priority (top = preferred)'),('Optimizer','Audio','CodecPriority'),'priority',[])
        form=self._group(layout,_('Protect audio tracks'))
        from audio_plan import PRESERVABLE, PROTECTION_MODES
        self._field(form,_('Protect audio formats (one codec per line)'),
                    ('Recode','Audio','PreserveFormats'),'list',['truehd_atmos','eac3_atmos'])
        layout.addWidget(QLabel(_('Supported format identifiers: ') + ', '.join(sorted(PRESERVABLE))))
        self._field(form,_('Protection mode'),('Recode','Audio','ProtectionMode'),'choice','additional',
                    [(_('Off'),'off'),(_('Keep an additional original audio track'),'additional'),
                     (_('Keep original audio track only'),'original_only')])
        self._field(form,_('Protect surround audio above 5.1'),('Recode','Audio','ProtectSurroundAbove51'),'bool',False)
        self._field(form,_('Protect lossless audio tracks'),('Recode','Audio','ProtectLossless'),'bool',False)
        self._field(form,_('Use everyday default profile'),('Recode','Audio','EverydayDefault'),'bool',True)
        self._field(form,_('Preserve source bitrate'),('Recode','Audio','PreserveSourceBitrate'),'bool',True)
        form=self._group(layout,_('Target audio bitrates (kbit/s)'))
        for codec,default in [('dts',768),('eac3',448),('ac3',448)]:
            self._field(form,codec.upper(),('Recode','Audio','CodecBitratesKbps',codec),'int',default)
        form=self._group(layout,_('Subtitles'))
        self._field(form,_('Mux external SRT into MKV'),('Recode','ExternalSubtitles','MuxIntoMKV'),'bool',False)
        layout.addStretch()

    def _extra_field(self,form,label,filename,path,kind='text',default=None):
        file=ROOT/filename
        try:
            data=json.loads(file.read_text(encoding='utf-8'))
        except (OSError,ValueError) as exc:
            form.addRow(label,QLabel(str(exc)))
            return
        value=data
        for key in path:
            value=value.get(key,default) if isinstance(value,dict) else default
        if kind=='bool':
            widget=QCheckBox()
            widget.setChecked(bool(value))
            reader=widget.isChecked
        else:
            widget=QLineEdit(str(value if value is not None else ''))
            reader=widget.text
        if path == ('Movieset_Artwork',):
            holder=QWidget()
            row=QHBoxLayout(holder)
            row.setContentsMargins(0,0,0,0)
            row.addWidget(widget,1)
            browse=QPushButton(_('Browse …'))
            browse.clicked.connect(lambda checked=False,w=widget:self._browse_movieset(w))
            row.addWidget(browse)
            form.addRow(label,holder)
        else:
            form.addRow(label,widget)
        self.extra_controls.append((file,path,reader))
        self.extra_initial_values[(file,path)]=copy.deepcopy(reader())

    def _browse_movieset(self,widget):
        # Keep manual entry available for currently disconnected NAS shares.
        selected=QFileDialog.getExistingDirectory(self,_('Select movie set artwork folder'),widget.text())
        if selected:
            widget.setText(selected)

    def _metadata(self):
        layout=self._tab('Metadaten')
        movie=self._group(layout,_('Movies'))
        self._extra_field(movie,_('Rename files'),'movie_scraper_config.json',('Filenaming','Rename','RenameFiles'),'bool',True)
        self._extra_field(movie,_('Movie rename pattern'),'movie_scraper_config.json',('Filenaming','Rename','Movie'))
        self._extra_field(movie,_('Central movie set artwork folder'),'movie_scraper_config.json',('Movieset_Artwork',))
        self._extra_field(movie,_('Download movie set artwork'),'movie_scraper_config.json',('Download_Movieset_Images',),'bool',True)
        series=self._group(layout,_('TV series'))
        self._field(series,_('Multi-episode NFO in legacy format'),('TVShow','MultiEpisode','Old'),'bool',True)
        self._field(series,_('Multi-episode NFO in Kodi 22 format'),('TVShow','MultiEpisode','New'),'bool',True)
        self._extra_field(series,_('Rename files'),'series_scraper_config.json',('Filenaming','Rename','RenameFiles'),'bool',True)
        self._extra_field(series,_('TV series rename pattern'),'series_scraper_config.json',('Filenaming','Rename','TvShow'))
        self._extra_field(series,_('Create season folders'),'series_scraper_config.json',('TVShow','CreateSeasonFolder'),'bool',True)
        self._extra_field(series,_('Season folder name'),'series_scraper_config.json',('TVShow','FolderSeason'))
        self._extra_field(series,_('Specials folder name'),'series_scraper_config.json',('TVShow','FolderSeasonSpecial'))
        self._extra_field(series,_('Create NFO for episodes not found online'),'series_scraper_config.json',('TVShow','Create_Episode_NFO_On_Missing_Info'),'bool',True)
        self._rating_editor(layout)
        layout.addWidget(QLabel(_('Sort titles and other legacy V1 options will be reviewed after the movie wizard.')))
        layout.addStretch()

    def _rating_editor(self,layout):
        group=QGroupBox(_('Age certification mapping'))
        area=QVBoxLayout(group)
        self.rating_enabled=QCheckBox(_('Convert certifications using the mapping table'))
        self.rating_enabled.setChecked(self._get(('Rating','OverwriteRating'),True))
        area.addWidget(self.rating_enabled)
        area.addWidget(QLabel(_('First matching rule wins. * wildcards are supported; separate multiple patterns with semicolons.')))
        self.rating_table=QTableWidget(0,2)
        self.rating_table.setHorizontalHeaderLabels([_('Target certification'),_('Source certifications / patterns')])
        self.rating_table.horizontalHeader().setStretchLastSection(True)
        self.rating_table.setColumnWidth(0,160)
        self.rating_table.setMinimumHeight(230)
        self.rating_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        area.addWidget(self.rating_table)
        for rule in self._get(('Rating','Ratings'),[]):
            if isinstance(rule,dict):
                self._add_rating_row(rule.get('SetRating',''),rule.get('ContainsRating',[]))
        buttons=QHBoxLayout()
        add=QPushButton(_('Add rule'));add.clicked.connect(lambda:self._add_rating_row())
        remove=QPushButton(_('Delete selected rule'));remove.clicked.connect(self._remove_rating_rows)
        german=QPushButton(_('German preset'));german.clicked.connect(self._load_german_rating_preset)
        for b in (add,remove,german):buttons.addWidget(b)
        area.addLayout(buttons)
        area.addWidget(QLabel(_('The scraper prefers available local certifications; mapping is a fallback.')))
        layout.addWidget(group)

    def _add_rating_row(self,target='',patterns=None):
        index=self.rating_table.rowCount()
        self.rating_table.insertRow(index)
        self.rating_table.setItem(index,0,QTableWidgetItem(str(target)))
        self.rating_table.setItem(index,1,QTableWidgetItem('; '.join(map(str,patterns or []))))

    def _remove_rating_rows(self):
        rows=sorted({x.row() for x in self.rating_table.selectedIndexes()},reverse=True)
        for row in rows:self.rating_table.removeRow(row)

    def _load_german_rating_preset(self):
        result=QMessageBox.question(self,_('German preset'),
            _('Replace the current mapping rules with the German preset?'),
            QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if result!=QMessageBox.StandardButton.Yes:return
        preset=[('FSK 0',['0','*-0','* 0','G','TV-G','TV-Y','NR']),
                ('FSK 6',['6','*-6','* 6','PG','TV-Y7','TV-Y7-FV','TV-PG']),
                ('FSK 12',['12','*-12','* 12','PG-13','TV-14']),
                ('FSK 16',['16','*-16','* 16','NC-17']),
                ('FSK 18',['18','*-18','* 18','R','X','TV-MA'])]
        self.rating_table.setRowCount(0)
        for target,patterns in preset:self._add_rating_row(target,patterns)

    def _rating_values(self):
        result=[]
        for row in range(self.rating_table.rowCount()):
            target=(self.rating_table.item(row,0).text() if self.rating_table.item(row,0) else '').strip()
            patterns=(self.rating_table.item(row,1).text() if self.rating_table.item(row,1) else '')
            patterns=[p.strip() for p in patterns.split(';') if p.strip()]
            if not target and not patterns:continue
            if not target or not patterns:
                raise ValueError(_('Each mapping rule needs a target certification and at least one source pattern.'))
            result.append({'SetRating':target,'ContainsRating':patterns})
        return result

    def _filenames(self):
        layout=self._tab('Dateinamen')
        form=self._group(layout,_('HTPC artwork naming presets'))
        active=self._get(('VideoSettings','ArtworkNamingPresets'),['kodi']) or ['kodi']
        for key,label in (
            ('kodi',_('Default (Kodi, MediaPortal 2, NextPVR, Plex …)')),
            ('emby','Emby / Jellyfin'),
            ('mediaportal1','MediaPortal 1')):
            check=QCheckBox(label)
            check.setChecked(key in active)
            check.toggled.connect(self._preset_changed)
            form.addRow(check)
            self.preset_checks[key]=check
        layout.addWidget(QLabel(_('At least one naming scheme must remain enabled.')))
        layout.addWidget(QLabel(_('Poster, disc art and clearlogo are saved under all required filenames.')))
        other=self._group(layout,_('Other videos – automatically generated artwork'))
        self._field(other,_('Poster filename'),('VideoSettings','OtherVideoArtworkNames','Poster'),
                    'text','{movie}-poster.jpg')
        self._field(other,_('Fanart filename'),('VideoSettings','OtherVideoArtworkNames','Fanart'),
                    'text','{movie}-fanart.jpg')
        layout.addWidget(QLabel(_('The {movie} placeholder represents the final video filename without its extension.')))
        layout.addWidget(QLabel(_('Artwork generation will be implemented with the Other Videos wizard.')))
        layout.addStretch()

    def _preset_changed(self,checked):
        active=[w for w in self.preset_checks.values() if w.isChecked()]
        for widget in self.preset_checks.values():
            widget.setEnabled(not (len(active)==1 and widget in active))

    def save(self):
        try:
            ensure_configuration(ROOT)
            data=effective(CONFIG)
            # Explicit Save also materializes missing GUI-backed keys; a normal
            # read remains non-destructive. Never overwrite an existing value
            # merely because the in-memory fallback differs.
            raw = load_raw(CONFIG)
            def missing(obj, path):
                for key in path:
                    if not isinstance(obj, dict) or key not in obj:
                        return True
                    obj = obj[key]
                return False
            changed = {path: reader() for path,reader in self.controls
                       if reader() != self.initial_values[path] or missing(raw, path)}
            for path,value in changed.items():
                target=data
                for key in path[:-1]:
                    if key not in target:target[key]={}
                    if not isinstance(target[key],dict):raise ValueError(f'Kein JSON-Objekt: {key}')
                    target=target[key]
                target[path[-1]]=value
            # Validate audio selections before writing anything.
            from audio_plan import PRESERVABLE, PROTECTION_MODES
            audio=data.get('Recode',{}).get('Audio',{})
            preserved=audio.get('PreserveFormats',[])
            if not isinstance(preserved,list) or len(preserved)!=len(set(preserved)) or not set(preserved)<=PRESERVABLE:
                raise ValueError(_('Invalid or duplicate protected audio formats. Allowed: ') + ', '.join(sorted(PRESERVABLE)))
            if audio.get('ProtectionMode','additional') not in PROTECTION_MODES:
                raise ValueError(_('Invalid audio protection mode.'))
            priority=data.get('Optimizer',{}).get('Audio',{}).get('CodecPriority',[])
            if len(priority)!=len(set(priority)):
                raise ValueError(_('Audio codec priority contains duplicate entries.'))
            # Validate templates now; generation itself is added with the Other Videos metadata module.
            names=data['VideoSettings']['OtherVideoArtworkNames']
            for name in ('Poster','Fanart'):
                template=names[name].strip()
                if (not template or '{movie}' not in template or
                        '/' in template or '\\' in template or
                        any(ch in template for ch in '<>:"|?*') or
                        template.count('{')!=1 or template.count('}')!=1 or
                        not template.lower().endswith(('.jpg','.jpeg','.png'))):
                    raise ValueError(_('Invalid artwork filename template: ') + name)
                names[name]=template
            if names['Poster'].casefold()==names['Fanart'].casefold():
                raise ValueError(_('Poster and fanart must use different filenames.'))
            video=data['VideoSettings']['VideoExtensions'];stub=data['VideoSettings']['StubExtensions']
            if set(s.lower() for s in video)&set(s.lower() for s in stub):
                raise ValueError(_('Video and stub file extensions must not overlap.'))
            if not video or not stub:raise ValueError(_('Both file extension lists must contain entries.'))
            # Keep existing consumers compatible: combined FileExtensions remains authoritative.
            if any(path in changed for path in (('VideoSettings','VideoExtensions'),('VideoSettings','StubExtensions'))):
                data['FileExtensions']=list(dict.fromkeys(video+stub))
            presets=[name for name,check in self.preset_checks.items() if check.isChecked()]
            if any(check.isChecked() != self.initial_preset_checks.get(name, False) for name,check in self.preset_checks.items()):
                data['VideoSettings']['ArtworkNamingPresets']=presets
                changed[('VideoSettings','ArtworkNamingPresets')]=presets
            if not presets or any(x not in ('kodi','emby','mediaportal1') for x in presets):
                raise ValueError(_('Invalid HTPC preset. Allowed: kodi, emby, mediaportal1.'))
            if (self.rating_enabled.isChecked() != self.initial_rating_enabled or self._rating_values() != self.initial_ratings
                    or missing(raw, ('Rating','OverwriteRating')) or missing(raw, ('Rating','Ratings'))):
                data.setdefault('Rating',{})['OverwriteRating']=self.rating_enabled.isChecked()
                data['Rating']['Ratings']=self._rating_values()
                changed[('Rating','Ratings')]=data['Rating']['Ratings']
            # Preserve unrelated scraper keys and only change fields shown in the GUI.
            updated={}
            for file,path,reader in self.extra_controls:
                if reader() == self.extra_initial_values[(file,path)] and not missing(load_raw(file), path):
                    continue
                if file not in updated:
                    try:
                        updated[file]=json.loads(file.read_text(encoding='utf-8-sig'))
                    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                        raise ValueError(_('Cannot read configuration file: ') + str(file) + '\n' + str(exc)) from exc
                    if not isinstance(updated[file],dict):
                        raise ValueError(_('Configuration is not a JSON object: ') + str(file))
                node=updated[file]
                for key in path[:-1]:
                    if key not in node: node[key]={}
                    if not isinstance(node[key],dict): raise ValueError('Kein JSON-Objekt: ' + str(key))
                    node=node[key]
                node[path[-1]]=reader()
            for file,values in updated.items():
                write_settings(file, values)
            if changed:
                write_settings(CONFIG,data)
        except Exception as exc:
            QMessageBox.critical(self,_('Save failed'),str(exc));return
        self.initial_values={path:copy.deepcopy(reader()) for path,reader in self.controls}
        self.extra_initial_values={(file,path):copy.deepcopy(reader()) for file,path,reader in self.extra_controls}
        self.initial_preset_checks={name:check.isChecked() for name,check in self.preset_checks.items()}
        self.initial_rating_enabled=self.rating_enabled.isChecked()
        self.initial_ratings=copy.deepcopy(self._rating_values())
        QMessageBox.information(self,_('Saved'),_('Settings saved.'))
