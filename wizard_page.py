"""Test 112: dauerhaft sichtbarer Arbeitsbereich und einklappbare Einstellungen."""
from __future__ import annotations
from activity_log import event as activity_event
import json
import threading
from pathlib import Path
from PySide6.QtCore import Qt, QThread, Signal, QTimer, QRect
from PySide6.QtGui import QPainter, QColor, QFont
from PySide6.QtWidgets import QStyle, QStyleOptionProgressBar
from PySide6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QGridLayout,QGroupBox,QLabel,QLineEdit,
    QBoxLayout,QPushButton,QCheckBox,QComboBox,QSpinBox,QDoubleSpinBox,QFileDialog,QScrollArea,QMessageBox,QToolButton,QFrame,QSizePolicy,QTableWidget,QTableWidgetItem,QHeaderView,QProgressBar)
from gui_i18n import _, navigation_label, stage_label

from system_actions import available_actions, perform_action

QUALITY_LABELS = (
    _('Extremely small file'),
    _('Very small file'),
    _('Small file'),
    _('Balanced'),
    _('High quality'),
    _('Very high quality'),
    _('Extremely high quality'),
)

class ContrastProgressBar(QProgressBar):
    """Paint centered progress text in two colors, clipped at the fill edge."""

    def paintEvent(self, event):
        option = QStyleOptionProgressBar()
        self.initStyleOption(option)
        option.textVisible = False
        painter = QPainter(self)
        self.style().drawControl(QStyle.ControlElement.CE_ProgressBar, option, painter, self)

        if self.maximum() <= self.minimum():
            fraction = 0.0
        else:
            fraction = max(0.0, min(1.0,
                (self.value() - self.minimum()) / (self.maximum() - self.minimum())))
        # The stylesheet uses a 2px border; match the chunk's inner geometry.
        inside = self.rect().adjusted(2, 2, -2, -2)
        edge = inside.left() + round(inside.width() * fraction)
        text = self.text()
        font = QFont(self.font())
        font.setBold(True)
        painter.setFont(font)
        alignment = Qt.AlignmentFlag.AlignCenter
        painter.save()
        painter.setClipRect(QRect(edge, 0, max(0, self.width()-edge), self.height()))
        painter.setPen(QColor('#172e45'))
        painter.drawText(self.rect(), alignment, text)
        painter.restore()
        painter.save()
        painter.setClipRect(QRect(0, 0, max(0, edge), self.height()))
        painter.setPen(QColor('#ffffff'))
        painter.drawText(self.rect(), alignment, text)
        painter.restore()


ROOT=Path(__file__).resolve().parent
SETTINGS=ROOT/'wizard_gui_settings.json'

def read_json(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError,ValueError):
        return {}

def defaults(kind):
    global_cfg=read_json(ROOT/'Media-Buddy.json')
    recode=global_cfg.get('Recode',{})
    opt=global_cfg.get('Optimizer',{})
    audio=opt.get('Audio',{}).get('Languages',{}).get('default',1)
    subs=opt.get('Subtitles',{}).get('Languages',{}).get('default',{})
    extra_audio={k:v for k,v in opt.get('Audio',{}).get('Languages',{}).items() if k!='default'}
    extra_subs={k:v for k,v in opt.get('Subtitles',{}).get('Languages',{}).items() if k!='default'}
    extra_languages={k:{'audio':int(extra_audio.get(k,0)), 'normal':int(extra_subs.get(k,{}).get('Normal',0)), 'forced':int(extra_subs.get(k,{}).get('Forced',0))} for k in set(extra_audio)|set(extra_subs)}
    scraper=read_json(ROOT/('series_scraper_config.json' if kind=='Serien' else 'movie_scraper_config.json' if kind=='Spielfilme' else 'musicvideo_scraper_config.json' if kind=='Musikvideos' else 'video_scraper_config.json'))
    return dict(source='',target='',download_images=scraper.get('Download_Images',True),
        overwrite_artwork=scraper.get('Overwrite',{}).get('Artwork',False),
        overwrite_nfo=scraper.get('Overwrite',{}).get('NFO',False),
        mkv_metadata=False,grabber=scraper.get('TVShow',{}).get('Default_Grabber','TVDB'),
        fanart=True,banner=True,poster=True,clearlogo=True,thumb=True,season=True,chapters=False,trailer=False,movieset=False,
        audio_optimizer=opt.get('Enabled',True),subtitle_optimizer=opt.get('Enabled',True),
        extra_languages=extra_languages,
        audio_count=max(1,int(audio)) if isinstance(audio,int) else 1,
        subtitle_normal=int(subs.get('Normal',0)),subtitle_forced=int(subs.get('Forced',1)),
        normalize=recode.get('AudioProcessing',{}).get('Normalize',True),
        normalize_level=recode.get('AudioProcessing',{}).get('NormalizeLevel',0.95),
        dynamic_compression=recode.get('AudioProcessing',{}).get('DynamicCompression',2),
        quality=recode.get('Quality',{'SD':5,'720':5,'1080':5,'4K':4}),
        video_codec=recode.get('VideoCodec','hevc'),
        remux_thresholds_kbps={'SD':0,'720':0,'1080':0,'4K':0})

class CollapsibleSection(QWidget):
    """Independent accordion section; hiding contents never alters widget values."""
    def __init__(self, title, content, parent=None):
        super().__init__(parent)
        outer=QVBoxLayout(self)
        outer.setContentsMargins(0,0,0,0)
        outer.setSpacing(0)
        self.toggle=QToolButton()
        self.toggle.setText(title)
        self.toggle.setCheckable(True)
        self.toggle.setChecked(False)
        self.toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Fixed)
        self.toggle.setStyleSheet('QToolButton { text-align:left; padding:11px 12px; border:1px solid #d6dfe8; background:#eef4fa; border-radius:5px; font-weight:600; } QToolButton:hover { background:#e1edf8; }')
        outer.addWidget(self.toggle)
        self.content=content
        self.content.setVisible(False)
        outer.addWidget(self.content)
        self.toggle.toggled.connect(self.set_open)

    def set_open(self, opened):
        self.content.setVisible(opened)
        self.toggle.setArrowType(Qt.ArrowType.DownArrow if opened else Qt.ArrowType.RightArrow)

class SeriesWorker(QThread):
    update = Signal(str, object, int, int)
    finished_result = Signal(object)
    def __init__(self, settings):
        super().__init__()
        self.settings = settings
        self.cancel_after_file = False
        self.immediate_cancel = threading.Event()
    def run(self):
        try:
            from series_wizard_runner import run_series_wizard
            from movie_wizard_runner import run_movie_wizard, run_musicvideo_wizard
            from other_video_wizard_runner import run_other_video_wizard
            from recode_controller import run_recode_batch, run_remux_batch, run_audio_normalize_batch
            from append_controller import run_append_batch
            from ffmpeg_manager import set_cancel_event
            set_cancel_event(self.immediate_cancel)
            try:
                runner = {'Spielfilme': run_movie_wizard, 'Musikvideos': run_musicvideo_wizard, 'Sonstige Videos': run_other_video_wizard, 'Recode': run_recode_batch, 'Remux': run_remux_batch, 'Audio-Normalisieren': run_audio_normalize_batch, 'Videos zusammenfügen': run_append_batch}.get(self.settings.get('_wizard_kind'), run_series_wizard)
                result = runner(self.settings, lambda *a: self.update.emit(*a),
                                           lambda: self.cancel_after_file,
                                           lambda: self.immediate_cancel.is_set())
            finally:
                set_cancel_event(None)
        except Exception as exc:
            result = {'fatal': str(exc)}
        self.finished_result.emit(result)

class WizardPage(QWidget):
    def __init__(self,kind,parent=None):
        super().__init__(parent)
        self.kind=kind
        all_settings=read_json(SETTINGS)
        self.data=defaults(kind)
        self.data.update(all_settings.get(kind,{}))
        root=QVBoxLayout(self)
        root.setContentsMargins(4,4,4,4)
        root.setSpacing(5)
        title=QLabel(f"{_('Wizards')}: {_('Videos') if kind == 'Sonstige Videos' else navigation_label(kind)} – {_('Processing')}")
        title.setObjectName('wizardTitle')
        root.addWidget(title)
        self.title_label=title
        folders=QGroupBox(_('Source and destination folders'))
        f=QGridLayout(folders)
        self.source=QLineEdit(str(self.data['source']))
        self.target=QLineEdit(str(self.data['target']))
        for i,(label,edit) in enumerate(((_('Source folder'),self.source),(_('Destination folder'),self.target))):
            f.addWidget(QLabel(label),i,0)
            f.addWidget(edit,i,1)
            browse=QPushButton(_('Browse …'))
            browse.clicked.connect(lambda checked=False,e=edit: self.browse(e))
            f.addWidget(browse,i,2)
        root.addWidget(folders)
        self.folders_widget=folders
        actions=QHBoxLayout()
        if kind in ('Serien', 'Spielfilme', 'Musikvideos', 'Sonstige Videos', 'Recode', 'Remux', 'Audio-Normalisieren', 'Videos zusammenfügen'):
            # No development notes or redundant descriptions on the four media wizards.
            actions.addStretch(1)
        else:
            note=QLabel(_('Remux videos without re-encoding. The optimizer is optional.') if kind=='Remux' else _('Re-encode videos and process audio according to the settings.') if kind=='Recode' else _('Copy video streams and re-encode all audio streams.') if kind=='Audio-Normalisieren' else _('Join all videos in each subfolder without re-encoding.') if kind=='Videos zusammenfügen' else _('This wizard is not yet connected.'))
            note.setWordWrap(True)
            actions.addWidget(note,1)
        if kind != 'Videos zusammenfügen':
            save=QPushButton(_('Save settings'))
            save.clicked.connect(self.save)
            actions.addWidget(save)
        start=QPushButton(_('Start'))
        start.setMinimumWidth(145)
        start.setMinimumHeight(38)
        start.setStyleSheet('QPushButton { background:#1769ad; color:white; font-weight:bold; border-radius:5px; padding:7px 18px; } QPushButton:hover { background:#0d568f; }')
        start.clicked.connect(self.start_wizard if kind in ('Serien','Spielfilme','Musikvideos','Recode','Remux','Audio-Normalisieren','Videos zusammenfügen','Sonstige Videos') else self.not_ready)
        self.start_button = start
        actions.addWidget(start)
        self.actions_widget=QWidget()
        self.actions_widget.setLayout(actions)
        root.addWidget(self.actions_widget)
        self.processing_panel=QWidget()
        panel=QVBoxLayout(self.processing_panel)
        panel.setContentsMargins(12,8,12,8)
        panel.setSpacing(5)
        heading=QLabel(_('TV series processing in progress'))
        heading.setStyleSheet('font-size:19px; font-weight:700; color:#183a5b;')
        heading.setMinimumHeight(27)
        self.processing_heading=heading
        panel.addWidget(heading)
        self.progress_status=QLabel(_('Preparing'))
        self.progress_status.setWordWrap(True)
        self.progress_status.setStyleSheet('font-size:13px; color:#172e45;')
        self.progress_status.setMinimumHeight(22)
        panel.addWidget(self.progress_status)
        for label,attr in [(_('Overall progress'),'overall_progress'),(_('Current file'),'current_progress')]:
            progress_label=QLabel(label)
            progress_label.setMinimumHeight(17)
            panel.addWidget(progress_label)
            bar=ContrastProgressBar()
            bar.setRange(0,100)
            bar.setFixedHeight(27)
            bar.setAlignment(Qt.AlignmentFlag.AlignCenter)
            bar.setFormat('%p %')
            bar.setStyleSheet('QProgressBar {border:2px solid #8196a8; border-radius:7px; background:#e2eaf1; text-align:center; font-size:13px; font-weight:bold;} QProgressBar::chunk {background:#1774b8; border-radius:5px;}')
            setattr(self,attr,bar)
            panel.addWidget(bar)
        panel.addSpacing(8)  # kompakter Abstand; Layout trennt die Zeilen zuverlässig
        self.cancel_button=QPushButton(_('Cancel after current file'))
        self.cancel_button.clicked.connect(self.cancel_wizard)
        self.cancel_button.setMinimumHeight(32)
        self.immediate_cancel_button = QPushButton(_('Cancel immediately'))
        self.immediate_cancel_button.setMinimumHeight(32)
        self.immediate_cancel_button.clicked.connect(self.cancel_immediately)
        # Beide Abbruchaktionen teilen sich eine Zeile und erhalten gleiche Breite.
        cancel_row = QHBoxLayout()
        cancel_row.setContentsMargins(0, 0, 0, 0)
        cancel_row.setSpacing(8)
        cancel_row.addWidget(self.cancel_button, 1)
        cancel_row.addWidget(self.immediate_cancel_button, 1)
        panel.addLayout(cancel_row)
        # Laufzeitoption: ausdrücklich NICHT in collect()/save() übernehmen.
        action_row = QHBoxLayout()
        action_row.addWidget(QLabel(_('After completion:')))
        self.end_action = QComboBox()
        self.end_action.addItem(_('Do nothing'), 'none')
        self.end_action.addItem(_('Exit program'), 'exit')
        if self.kind in ('Serien', 'Spielfilme', 'Sonstige Videos'):
            self.end_action.addItem(_('Open post-processing'), 'postprocess')
        for key, label in [('standby', _('Standby')), ('hibernate', _('Hibernate')), ('shutdown', _('Shut down PC'))]:
            if key in available_actions():
                self.end_action.addItem(label, key)
        self.end_action.setCurrentIndex(0)
        action_row.addWidget(self.end_action, 1)
        action_row.setContentsMargins(0, 4, 0, 0)
        action_row.setSpacing(8)
        self.end_action.setMinimumHeight(27)
        panel.addLayout(action_row)
        self.result_label=QLabel('')
        self.result_label.setWordWrap(True)
        self.result_label.setStyleSheet('font-size:14px;')
        self.result_label.setMinimumHeight(0)
        panel.addWidget(self.result_label)
        self.result_label.hide()
        self.back_button=QPushButton(_('Back to wizard'))
        self.back_button.setMinimumHeight(42)
        self.back_button.clicked.connect(self.show_setup)
        self.back_button.hide()
        panel.addWidget(self.back_button)
        panel.addStretch(1)
        root.addWidget(self.processing_panel,1)
        self.processing_panel.hide()
        self._current_index=0
        self._total_files=1
        self._phase='video'
        self._running=False
        scroll=QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        root.addWidget(scroll,1)
        self.settings_scroll=scroll
        content=QWidget()
        scroll.setWidget(content)
        layout=QVBoxLayout(content)
        layout.setContentsMargins(0,4,8,8)
        layout.setSpacing(9)
        self.sections=[]
        def section(name, widget):
            accordion=CollapsibleSection(name,widget)
            layout.addWidget(accordion)
            self.sections.append(accordion)
        metadata_and_images=QWidget()
        row=QHBoxLayout(metadata_and_images)
        row.setContentsMargins(5,8,5,8)
        row.setSpacing(12)
        metadata=QGroupBox(_('Metadata and artwork'))
        ml=QVBoxLayout(metadata)
        self.checks={}
        for key,label in [('download_images',_('Download artwork')),('overwrite_artwork',_('Replace artwork')),('overwrite_nfo',_('Replace NFO')),('mkv_metadata',_('Embed NFO and artwork in MKV'))]:
            cb=QCheckBox(label);cb.setChecked(bool(self.data[key]));ml.addWidget(cb);self.checks[key]=cb
            if kind=='Sonstige Videos':cb.hide()
        self.grabber=None
        if kind in ('Spielfilme','Musikvideos','Sonstige Videos'):
            ml.addWidget(QLabel(_('Data sources: MusicBrainz / TheAudioDB / Last.fm / Fanart.tv') if kind=='Musikvideos' else _('Local video scraper (offline)') if kind=='Sonstige Videos' else _('Source: TMDB')))
            cb=QCheckBox(_('Download chapters'));cb.setChecked(bool(self.data.get('chapters',False)));ml.addWidget(cb);self.checks['chapters']=cb
            if kind in ('Musikvideos','Sonstige Videos'): cb.hide()
        else:
            ml.addWidget(QLabel(_('Preferred source')))
            self.grabber=QComboBox();self.grabber.addItems(['TVDB','TMDB'])
            self.grabber.setCurrentText(str(self.data['grabber']))
            ml.addWidget(self.grabber)
        ml.addStretch()
        row.addWidget(metadata,1)
        images=QGroupBox(_('Which artwork should be generated?') if kind=='Sonstige Videos' else _('Which artwork to download?'))
        il=QVBoxLayout(images)
        image_options=[('fanart',_('Fanart / Background')),('banner',_('Banner')),('poster',_('Poster / Cover')),('clearlogo',_('Clearart / Logo'))]
        if kind=='Spielfilme':
            image_options += [('trailer',_('Download trailers')),('movieset',_('Download movie set artwork'))]
        elif kind=='Musikvideos':
            image_options += [('thumb',_('Music video thumbnail'))]
        elif kind=='Sonstige Videos':
            image_options = [('poster',_('Create poster')),('fanart',_('Create fanart'))]
        else:
            image_options += [('thumb',_('Thumbnail')),('season',_('Season poster / banner'))]
        for key,label in image_options:
            cb=QCheckBox(label);cb.setChecked(bool(self.data[key]));il.addWidget(cb);self.checks[key]=cb
        row.addWidget(images,1)
        if kind=='Sonstige Videos':
            row.setDirection(QBoxLayout.Direction.TopToBottom)
        if kind=='Sonstige Videos':
            local=QGroupBox(_('Videos – local metadata'))
            form=QGridLayout(local)
            self.other_fields={}
            for index,(key,label) in enumerate([('genre',_('Default genre')),('plot',_('Default description'))]):
                field=QLineEdit(str(self.data.get('other_'+key,'')))
                form.addWidget(QLabel(label),index,0);form.addWidget(field,index,1)
                self.other_fields[key]=field
            for index,(key,label,default) in enumerate([
                ('genre_from_folder',_('Use folder name as genre'),True),
                ('mediainfo_plot',_('Use MediaInfo as description'),True),
                ('make_nfo',_('Create NFO'),True)]):
                cb=QCheckBox(label)
                cb.setChecked(bool(self.data.get('other_'+key,default)))
                form.addWidget(cb,index+2,0,1,2);self.other_fields[key]=cb
            for index,(key,label,values,default) in enumerate([
                ('poster_count',_('Poster frames'),[1,2],1),
                ('fanart_count',_('Fanart frames'),[1,2,3,4,5,6],6)]):
                combo=QComboBox()
                for value in values:combo.addItem(str(value),value)
                choice=combo.findData(int(self.data.get('other_'+key,default)))
                if choice>=0:combo.setCurrentIndex(choice)
                form.addWidget(QLabel(label),index+5,0);form.addWidget(combo,index+5,1)
                self.other_fields[key]=combo
            for index,(key,label,folder) in enumerate([
                ('cover_template',_('Poster background'),'Cover'),
                ('backdrop_template',_('Fanart background'),'Backdrop'),
                ('border_template',_('Border'),'Border')]):
                combo=QComboBox()
                combo.addItem(_('Video frame / Default'),None)
                for file in sorted((ROOT/'Templates'/folder).glob('*')):
                    if file.suffix.lower() in ('.png','.jpg','.jpeg','.webp'):
                        combo.addItem(file.name,str(file))
                selected=self.data.get('other_'+key)
                if selected:
                    found=combo.findData(selected)
                    if found>=0:combo.setCurrentIndex(found)
                form.addWidget(QLabel(label),index+7,0)
                form.addWidget(combo,index+7,1)
                self.other_fields[key]=combo
            row.addWidget(local,2)
        quality=QGroupBox(_('Video quality – individual profile for')+' '+navigation_label(kind))
        ql=QGridLayout(quality)
        ql.addWidget(QLabel(_('Resolution')),0,0);ql.addWidget(QLabel(_('Quality level (1–7)')),0,1)
        self.qualities={}
        for i,(key,label) in enumerate([('SD','SD'),('720','720p'),('1080','1080p'),('4K','4K / UHD')],1):
            ql.addWidget(QLabel(label),i,0)
            combo=QComboBox()
            for j,description in enumerate(QUALITY_LABELS,1): combo.addItem(f'{j} – {description}',j)
            value=int(self.data['quality'].get(key,5))
            combo.setCurrentIndex(max(0,min(6,value-1)))
            ql.addWidget(combo,i,1);self.qualities[key]=combo
        ql.addWidget(QLabel(_('Target codec')),5,0)
        self.codec=QComboBox()
        for name,value in [('HEVC / H.265','hevc'),('AVC / H.264','h264'),('AV1','av1')]: self.codec.addItem(name,value)
        idx=self.codec.findData(self.data['video_codec'])
        if idx>=0:self.codec.setCurrentIndex(idx)
        ql.addWidget(self.codec,5,1)
        section(_('Metadata and artwork'),metadata_and_images)
        thresholds=QGroupBox(_('Remux thresholds – individual profile for')+' '+navigation_label(kind))
        self.threshold_group = thresholds
        tl=QGridLayout(thresholds)
        explanation=QLabel(_('Video bitrate thresholds per resolution in kbit/s.\n0 = not configured yet (no automatic decision).\nThe wizard compares these values against the source video bitrate.'))
        explanation.setWordWrap(True)
        tl.addWidget(explanation,0,0,1,2)
        self.remux_thresholds={}
        saved=self.data.get('remux_thresholds_kbps',{})
        if not isinstance(saved,dict): saved={}
        for i,(key,label) in enumerate([('SD','SD'),('720','720p'),('1080','1080p'),('4K','4K / UHD')],1):
            tl.addWidget(QLabel(label),i,0)
            field=QSpinBox()
            field.setRange(0,1000000)
            field.setSingleStep(500)
            field.setSuffix(' kbit/s')
            field.setSpecialValueText(_('Not configured'))
            try: value=int(saved.get(key,0))
            except (ValueError,TypeError): value=0
            field.setValue(max(0,min(1000000,value)))
            tl.addWidget(field,i,1)
            self.remux_thresholds[key]=field
        quality_thresholds=QWidget()
        qt_layout=QHBoxLayout(quality_thresholds)
        qt_layout.setContentsMargins(5,8,5,8)
        qt_layout.addWidget(quality,1)
        qt_layout.addWidget(thresholds,1)
        section(_('Video quality and remux thresholds'),quality_thresholds)
        optimizer_and_audio=QWidget()
        bottom=QHBoxLayout(optimizer_and_audio)
        bottom.setContentsMargins(5,8,5,8)
        bottom.setSpacing(12)
        optimizer=QGroupBox(_('Optimizer – language quotas for')+' '+(_('Videos') if kind=='Sonstige Videos' else navigation_label(kind)))
        self.optimizer_group = optimizer
        ol=QGridLayout(optimizer)
        for r,(key,label) in enumerate([('audio_optimizer',_('Optimize audio')),('subtitle_optimizer',_('Optimize subtitles'))]):
            cb=QCheckBox(label);cb.setChecked(bool(self.data[key]));ol.addWidget(cb,r,0,1,2);self.checks[key]=cb
        ol.addWidget(QLabel(_('Audio tracks (min. 1)')),2,0)
        self.audio_count=QSpinBox();self.audio_count.setRange(1,32);self.audio_count.setValue(max(1,int(self.data['audio_count'])));ol.addWidget(self.audio_count,2,1)
        ol.addWidget(QLabel(_('Regular subtitles')),3,0)
        self.sub_normal=QSpinBox();self.sub_normal.setRange(0,32);self.sub_normal.setValue(int(self.data['subtitle_normal']));ol.addWidget(self.sub_normal,3,1)
        ol.addWidget(QLabel(_('Forced subtitles')),4,0)
        self.sub_forced=QSpinBox();self.sub_forced.setRange(0,32);self.sub_forced.setValue(int(self.data['subtitle_forced']));ol.addWidget(self.sub_forced,4,1)
        ol.addWidget(QLabel(_('Additional languages (ISO-639-2, e.g. eng, fra, ita)')),5,0,1,2)
        self.language_table=QTableWidget(0,4)
        self.language_table.setHorizontalHeaderLabels([_('Language'),_('Audio'),_('Regular subtitles'),_('Forced subtitles')])
        self.language_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.language_table.setMinimumHeight(145)
        ol.addWidget(self.language_table,6,0,1,2)
        lang_actions=QHBoxLayout()
        add_language=QPushButton(_('Add language'))
        add_language.clicked.connect(lambda: self.add_language_row())
        remove_language=QPushButton(_('Remove selected'))
        remove_language.clicked.connect(self.remove_language_row)
        lang_actions.addWidget(add_language)
        lang_actions.addWidget(remove_language)
        ol.addLayout(lang_actions,7,0,1,2)
        for code,limits in sorted(self.data.get('extra_languages',{}).items()):
            if isinstance(limits,dict):
                self.add_language_row(code,limits)
        ol.addWidget(QLabel(_('0 = do not keep tracks of this type. Default language is set above.')),8,0,1,2)
        bottom.addWidget(optimizer,1)
        processing=QGroupBox(_('Audio processing'))
        self.audio_processing_group = processing
        pl=QGridLayout(processing)
        self.normalize=QCheckBox(_('Normalize audio'));self.normalize.setChecked(bool(self.data['normalize']));pl.addWidget(self.normalize,0,0,1,2)
        pl.addWidget(QLabel(_('Normalization level')),1,0)
        self.level=QDoubleSpinBox();self.level.setDecimals(2);self.level.setRange(0.01,1.0);self.level.setSingleStep(.01);self.level.setValue(float(self.data['normalize_level']));pl.addWidget(self.level,1,1)
        self.normalize.toggled.connect(self.level.setEnabled);self.level.setEnabled(self.normalize.isChecked())
        pl.addWidget(QLabel(_('Dynamic range compression')),2,0)
        self.compression=QComboBox()
        for name in [_('0 – Off'),_('1 – Low'),_('2 – Medium'),_('3 – High'),_('4 – Extreme')]:self.compression.addItem(name)
        self.compression.setCurrentIndex(max(0,min(4,int(self.data['dynamic_compression']))));pl.addWidget(self.compression,2,1)
        # Keep the three controls at the top when the adjacent optimizer table is taller.
        pl.setRowStretch(3, 1)
        bottom.addWidget(processing,1)
        if kind=='Sonstige Videos':
            # Keep the wide language table readable at the default window width.
            bottom.removeWidget(processing)
            bottom.removeWidget(optimizer)
            bottom.addWidget(optimizer)
            bottom.setDirection(QBoxLayout.Direction.TopToBottom)
            bottom.addWidget(processing)
        section(_('Optimizer and audio processing'),optimizer_and_audio)
        layout.addStretch(1)

    def add_language_row(self,code='',limits=None):
        limits=limits or {}
        row=self.language_table.rowCount()
        self.language_table.insertRow(row)
        self.language_table.setItem(row,0,QTableWidgetItem(str(code)))
        for col,key in [(1,'audio'),(2,'normal'),(3,'forced')]:
            spin=QSpinBox()
            spin.setRange(0,32)
            spin.setValue(max(0,min(32,int(limits.get(key,0)))))
            self.language_table.setCellWidget(row,col,spin)

    def remove_language_row(self):
        rows=sorted({index.row() for index in self.language_table.selectedIndexes()},reverse=True)
        for row in rows:
            self.language_table.removeRow(row)

    def get_extra_languages(self):
        result={}
        for row in range(self.language_table.rowCount()):
            item=self.language_table.item(row,0)
            code=(item.text() if item else '').strip().lower()
            if not code:
                continue
            if len(code)!=3 or not code.isalpha() or code=='default':
                raise ValueError(_('Invalid language code on row {row}: {code!r}. Use three-letter ISO language codes.').format(row=row+1, code=code))
            if code in result:
                raise ValueError(f'Doppelte Zusatzsprache: {code}')
            result[code]={key:self.language_table.cellWidget(row,col).value() for col,key in [(1,'audio'),(2,'normal'),(3,'forced')]}
        return result

    def browse(self,edit):
        chosen=QFileDialog.getExistingDirectory(self,_('Select folder'),edit.text() or str(ROOT))
        if chosen:edit.setText(chosen)

    def collect(self):
        result=dict(self.data)
        result.update(source=self.source.text(),target=self.target.text(),grabber=self.grabber.currentText() if self.grabber is not None else 'TMDB',
            quality={k:v.currentData() for k,v in self.qualities.items()},video_codec=self.codec.currentData(),
            remux_thresholds_kbps={k:v.value() for k,v in self.remux_thresholds.items()},
            audio_count=self.audio_count.value(),subtitle_normal=self.sub_normal.value(),subtitle_forced=self.sub_forced.value(),
            normalize=self.normalize.isChecked(),normalize_level=self.level.value(),dynamic_compression=self.compression.currentIndex())
        result.update({k:cb.isChecked() for k,cb in self.checks.items()})
        result['extra_languages']=self.get_extra_languages()
        if self.kind=='Sonstige Videos':
            for key,widget in self.other_fields.items():
                result['other_'+key]=(widget.text() if isinstance(widget,QLineEdit)
                                     else widget.isChecked() if isinstance(widget,QCheckBox)
                                     else widget.currentData())
            result['other_make_poster']=result.get('poster',True)
            result['other_make_fanart']=result.get('fanart',True)
        result.pop('video_optimizer',None)
        return result

    def save(self, silent=False):
        all_settings=read_json(SETTINGS)
        try:
            all_settings[self.kind]=self.collect()
        except ValueError as exc:
            if not silent: QMessageBox.warning(self,_('Language configuration'),str(exc))
            return
        try:
            tmp=SETTINGS.with_suffix('.json.tmp')
            tmp.write_text(json.dumps(all_settings,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            tmp.replace(SETTINGS)
        except OSError as exc:
            QMessageBox.critical(self,_('Save failed'),str(exc));return
        if not silent:
            QMessageBox.information(self,_('Saved'),f"{_('Wizard settings saved:')} {navigation_label(self.kind)}.\n{_('The global Media-Buddy.json was not modified.')}")

    def start_wizard(self):
        if getattr(self, 'worker', None) is not None and self.worker.isRunning():
            return
        try:
            settings = self.collect()
            source = Path(settings['source']).expanduser().resolve()
            target = Path(settings['target']).expanduser().resolve()
            if not source.is_dir() or not target.is_dir():
                raise ValueError(_('Source and destination folders must exist.'))
            if source == target or source in target.parents or target in source.parents:
                raise ValueError(_('Source and destination folders must not be nested.'))
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, _('Wizard'), str(exc))
            return
        self.end_action.setCurrentIndex(0)  # Jeder neue Durchgang beginnt mit 'Nichts tun'.
        settings['_wizard_kind']=self.kind
        self.worker = SeriesWorker(settings)
        self._running=True
        activity_event(f"{self.kind}: Verarbeitung gestartet; Quelle={source}; Ziel={target}")
        self._set_navigation_locked(True)
        self.processing_heading.setText(_('Video transcoding in progress') if self.kind=='Recode' else _('Video remuxing in progress') if self.kind=='Remux' else _('Audio normalization running') if self.kind=='Audio-Normalisieren' else _('Joining videos') if self.kind=='Videos zusammenfügen' else _('Processing music videos') if self.kind=='Musikvideos' else _('Processing movies') if self.kind=='Spielfilme' else _('Processing other videos') if self.kind=='Sonstige Videos' else _('TV series processing in progress'))
        self._phase='video'
        self._current_index=0
        self._total_files=1
        self.folders_widget.hide()
        self.actions_widget.hide()
        self.settings_scroll.hide()
        self.processing_panel.show()
        self.back_button.hide()
        self.result_label.clear()
        self.result_label.hide()
        self.worker.update.connect(self.on_worker_update)
        self.worker.finished_result.connect(self.on_worker_done)
        self.start_button.setEnabled(False)
        self.cancel_button.setText(_('Cancel after current file'))
        self.cancel_button.show()
        self.immediate_cancel_button.show()
        self.cancel_button.setEnabled(True)
        self.immediate_cancel_button.setEnabled(True)
        self.overall_progress.setValue(0)
        self.current_progress.setValue(0)
        window = self.window()
        if hasattr(window, "set_work_status"):
            window.set_work_status("phase", _('Preparing TV series processing'))
        self.worker.start()

    def cancel_wizard(self):
        if not (self.worker and self.worker.isRunning()):
            return
        if self.worker.cancel_after_file:
            self.worker.cancel_after_file = False
            self.cancel_button.setText(_('Cancel after current file'))
            self.progress_status.setText(_('Cancellation request withdrawn'))
            activity_event(f'{self.kind}: Abbruchanforderung zurückgenommen')
            return
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Question)
        dialog.setWindowTitle(_('Confirm cancellation'))
        dialog.setText(_('Stop processing after the current file?\nOriginal files remain unchanged.'))
        yes_button = dialog.addButton(_('Yes'), QMessageBox.ButtonRole.YesRole)
        no_button = dialog.addButton(_('No'), QMessageBox.ButtonRole.NoRole)
        dialog.setDefaultButton(no_button)
        dialog.exec()
        if dialog.clickedButton() is yes_button:
            self.worker.cancel_after_file = True
            activity_event(f'{self.kind}: Abbruch nach aktueller Datei angefordert', 'WARN')
            self.cancel_button.setText(_('Undo cancellation'))
            self.progress_status.setText(_('Cancellation after current file requested …'))

    def cancel_immediately(self):
        if not (self.worker and self.worker.isRunning()):
            return
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Question)
        dialog.setWindowTitle(_('Confirm immediate cancellation'))
        dialog.setText(_('Stop processing IMMEDIATELY?\nThe current output file will remain incomplete.\nOriginal files remain unchanged.'))
        yes_button = dialog.addButton(_('Yes'), QMessageBox.ButtonRole.YesRole)
        no_button = dialog.addButton(_('No'), QMessageBox.ButtonRole.NoRole)
        dialog.setDefaultButton(no_button)
        dialog.exec()
        if dialog.clickedButton() is yes_button:
            self.worker.immediate_cancel.set()
            activity_event(f"{self.kind}: Sofortabbruch angefordert", "WARN")
            self.worker.cancel_after_file = True
            self.cancel_button.setEnabled(False)
            self.immediate_cancel_button.setEnabled(False)
            self.progress_status.setText(_('Immediate cancellation requested – stopping current process …'))

    def _set_navigation_locked(self, locked):
        window=self.window()
        if hasattr(window, 'nav'):
            window.nav.setEnabled(not locked)
        if hasattr(window, 'subnav'):
            window.subnav.setEnabled(not locked)
        if hasattr(window, 'menuBar'):
            window.menuBar().setEnabled(not locked)

    def show_setup(self):
        if self._running:
            return
        self.processing_panel.hide()
        self.folders_widget.show()
        self.actions_widget.show()
        self.settings_scroll.show()

    def on_worker_update(self, kind, value, index, total):
        window = self.window()
        if hasattr(window, "set_work_status"):
            window.set_work_status(kind, value, index, total)
        total=max(1,int(total))
        if kind=='file':
            self._phase='video'
            self._current_index=index
            self._total_files=total
            self.progress_status.setText(_('Video {index} of {total}: {filename}').format(index=index, total=total, filename=value))
            self.current_progress.setValue(0)
            self.overall_progress.setValue(round((100 if self.kind in ('Recode','Remux','Audio-Normalisieren','Videos zusammenfügen') else 90)*(index-1)/total))
        elif kind=='progress' and isinstance(value,dict):
            try:
                pct=float(value.get('percent',value.get('percentage',0)))
                pct=max(0,min(100,pct))
                self.current_progress.setValue(round(pct))
                if self._phase=='video':
                    self.overall_progress.setValue(round((100 if self.kind in ('Recode','Remux','Audio-Normalisieren','Videos zusammenfügen') else 90)*((self._current_index-1)+pct/100)/self._total_files))
            except (ValueError,TypeError):
                pass
        elif kind=='phase':
            self.current_progress.setRange(0, 100)
            self._phase=str(value)
            self.progress_status.setText(stage_label(str(value)))
            self.current_progress.setValue(0)
            self.overall_progress.setValue(90 if 'Scraper' in str(value) else 95)
        elif kind=='scraper_progress' and isinstance(value, dict):
            stage = str(value.get('stage') or 'Scraper')
            filename = value.get('file')
            i, n = value.get('index'), value.get('total')
            prefix = f'{i}/{n} – ' if isinstance(i, int) and isinstance(n, int) and n else ''
            self.progress_status.setText(f'{prefix}{filename + " – " if filename else ""}{stage_label(stage)}')
            pct = value.get('percent')
            if pct is None:
                # An unknown network operation has no invented percentage.
                self.current_progress.setRange(0, 0)
            else:
                self.current_progress.setRange(0, 100)
                self.current_progress.setValue(round(max(0, min(100, float(pct)))))
            if isinstance(i, int) and isinstance(n, int) and n:
                self.overall_progress.setValue(round(90 + 9 * max(0, i-1)/n))
        elif kind=='phase_progress':
            self.current_progress.setRange(0, 100)
            try:
                pct=max(0,min(100,float(value)))
                self.current_progress.setValue(round(pct))
                base=90 if 'Scraper' in self._phase else 99
                span=9 if 'Scraper' in self._phase else 1
                self.overall_progress.setValue(round(base+span*pct/100))
            except (ValueError,TypeError):
                pass
        elif kind=='log':
            self.progress_status.setText(str(value)[:220])

    def on_worker_done(self, result):
        window = self.window()
        if hasattr(window, "set_work_status"):
            window.set_work_status("done")
        self._running = False
        self._set_navigation_locked(False)
        self.start_button.setEnabled(True)
        self.cancel_button.hide()
        self.immediate_cancel_button.hide()
        self.show_setup()
        fatal = result.get('fatal')
        aborted = result.get('aborted')
        errors = sum(int(result.get(k, 0)) for k in ('failed', 'tag_errors', 'attachment_errors', 'scraper_errors'))
        status = "fehlgeschlagen" if fatal else "abgebrochen" if aborted else "mit Fehlern beendet" if errors or result.get("scraper_rc", 0) else "erfolgreich beendet"
        activity_event(
            f"{self.kind}: {status}; verarbeitet={result.get('processed', 0)}; "
            f"übersprungen={result.get('skipped', 0)}; Fehler={errors}; "
            f"Scraper-RC={result.get('scraper_rc', 0)}; "
            f"Modul-Log={result.get('log_file', '–')}"
            + (f"; Ursache={fatal}" if fatal else ""),
            "ERROR" if fatal else "WARN" if aborted or errors or result.get("scraper_rc", 0) else "INFO"
        )

        if fatal:
            title = _('TV series processing: error')
            summary = f"{_('Processing failed:')} {fatal}"
        else:
            title = _('TV series processing cancelled') if aborted else (_('TV series processing completed with errors') if errors or result.get('scraper_rc', 0) else _('TV series processing completed'))
            summary = (f"{_('Processed:')} {result.get('processed', 0)}\n"
                       f"{_('Skipped:')} {result.get('skipped', 0)}\n"
                       f"{_('Processing errors:')} {result.get('failed', 0)}\n"
                       f"{_('Matroska tags written:')} {result.get('tagged', 0)}\n"
                       f"{_('Tagging errors:')} {result.get('tag_errors', 0)}\n"
                       f"{_('Attachments embedded:')} {result.get('attachments', 0)}\n"
                       f"{_('Attachment errors:')} {result.get('attachment_errors', 0)}\n"
                       f"{_('Scraper return code:')} {result.get('scraper_rc', '–')}\n"
                       f"{_('Log file:')} {result.get('log_file', _('not available'))}")
        if self.kind=='Sonstige Videos' and not fatal:
            title = (_('Other videos: cancelled') if aborted else
                     _('Other videos: completed with errors') if errors else
                     _('Other videos: completed'))
            summary = (f"{_('Processed:')} {result.get('processed',0)}\n"
                       f"{_('Skipped:')} {result.get('skipped',0)}\n"
                       f"{_('Video errors:')} {result.get('failed',0)}\n"
                       f"{_('Artwork/NFO created:')} {result.get('scraped',0)}\n"
                       f"{_('Artwork/NFO skipped:')} {result.get('scraper_skipped',0)}\n"
                       f"{_('Artwork/NFO errors:')} {result.get('scraper_errors',0)}\n"
                       f"{_('Log file:')} {result.get('log_file','–')}")
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setText(summary)
        box.setIcon(QMessageBox.Icon.Warning if fatal or aborted or errors else QMessageBox.Icon.Information)
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        # Normaler Abschluss: nach acht Sekunden schließen, aber jederzeit OK möglich.
        # Bei Fehlern/Abbruch offen lassen, damit nichts übersehen wird.
        if not aborted:
            timer = QTimer(box)
            timer.setSingleShot(True)
            timer.timeout.connect(box.accept)
            timer.start(8000)
        box.open()
        # Erst nach Abschluss die zuletzt gewählte Laufzeitoption auswerten.
        if not aborted:
            action = self.end_action.currentData()
            if action != 'none':
                activity_event(f'{self.kind}: Endaktion gewählt: {action}')
            if action == 'exit':
                box.finished.connect(lambda _result: self.window().close())
            elif action == 'postprocess' and self.kind == 'Spielfilme':
                output_folder = str(self.target.text().strip())
                box.finished.connect(lambda _result, folder=output_folder:
                                     self._open_movie_postprocess(folder))
            elif action == 'postprocess' and self.kind == 'Serien':
                # Zielordner des eben beendeten Laufs, niemals der gespeicherte
                # Einstiegsordner der Nachbearbeitung.
                output_folder = str(self.target.text().strip())
                box.finished.connect(lambda _result, folder=output_folder:
                                     self._open_series_postprocess(folder))
            elif action == 'postprocess' and self.kind == 'Sonstige Videos':
                output_folder = str(self.target.text().strip())
                box.finished.connect(lambda _result, folder=output_folder:
                                     self._open_other_video_postprocess(folder))
            elif action in ('standby', 'hibernate', 'shutdown'):
                # Dialog zuerst schließen, dann separaten Countdown öffnen.
                box.finished.connect(lambda _result, a=action: self._show_power_countdown(a))

    def _open_movie_postprocess(self, folder):
        host = self.window()
        if hasattr(host, 'open_movie_postprocess_for_folder'):
            host.open_movie_postprocess_for_folder(folder)
        else:
            QMessageBox.warning(self, _('Post-processing'), _('Movie post-processing is unavailable.'))

    def _open_series_postprocess(self, folder):
        host = self.window()
        if hasattr(host, 'open_series_postprocess_for_folder'):
            host.open_series_postprocess_for_folder(folder)
        else:
            QMessageBox.warning(self, _('Post-processing'),
                                _('TV show post-processing is unavailable.'))

    def _open_other_video_postprocess(self, folder):
        host = self.window()
        if hasattr(host, 'open_other_video_postprocess_for_folder'):
            host.open_other_video_postprocess_for_folder(folder)
        else:
            QMessageBox.warning(self, _('Post-processing'),
                                _('Post-processing for other videos is unavailable.'))

    def _show_power_countdown(self, action):
        # 60-second cancelable countdown.
        names = {'standby':_('Standby'), 'hibernate':_('Hibernate'), 'shutdown':_('Shut down')}
        dialog = QMessageBox(self)
        dialog.setWindowTitle(_('System action after completion'))
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setStandardButtons(QMessageBox.StandardButton.Cancel)
        dialog.button(QMessageBox.StandardButton.Cancel).setText(_('Cancel action'))
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        remaining = [60]
        dialog.setText(f"{names[action]} {_('in')} {remaining[0]} {_('seconds.')}\n{_('Select “Cancel action” to keep the computer running.')}")
        ticker = QTimer(dialog)
        ticker.setInterval(1000)
        def tick():
            remaining[0] -= 1
            if remaining[0] <= 0:
                ticker.stop()
                # Dialog zuerst schließen, damit eine OS-Aktion nicht die UI blockiert.
                dialog.accept()
                try:
                    activity_event(f"{self.kind}: Endaktion wird ausgeführt: {action}")
                    perform_action(action)
                except Exception as exc:
                    QMessageBox.critical(self, _('System action failed'), str(exc))
            else:
                dialog.setText(f"{names[action]} {_('in')} {remaining[0]} {_('seconds.')}\n{_('Select “Cancel action” to keep the computer running.')}")
        ticker.timeout.connect(tick)
        ticker.start()
        dialog.open()

    def not_ready(self):
        QMessageBox.information(self,_('Not yet connected'),_('The batch start is not yet connected to the Mission Manager.\nNo files were changed.'))


class RecodePage(WizardPage):
    """Standalone recode UI reuses the proven wizard progress/cancel controls."""
    def __init__(self, parent=None):
        super().__init__('Recode', parent)
        self.title_label.setText(_('Processing: Transcode videos'))
        # Hide scraper controls and automatic remux thresholds; only quality remains.
        self.sections[0].hide()  # Metadata and artwork are not part of Recode.
        # Section 1 contains quality/codec AND automatic remux thresholds.
        # Keep quality visible; remove only the threshold box for this tool.
        self.threshold_group.hide()
        self.sections[1].toggle.setText(_('Video quality and target codec'))
        # Place recode-only controls INSIDE the audio group grid, not beside it.
        # Appending to the outer horizontal layout previously pushed these
        # controls into a narrow third column at the far right.
        audio_grid = self.audio_processing_group.layout()
        audio_grid.setRowStretch(3, 0)
        self.audio_mode = QComboBox()
        self.audio_mode.addItem(_('Re-encode audio'), 'recode')
        self.audio_mode.addItem(_('Copy audio'), 'copy')
        mode = str(self.data.get('audio_mode', 'recode'))
        self.audio_mode.setCurrentIndex(max(0, self.audio_mode.findData(mode)))
        self.audio_mode.currentIndexChanged.connect(self._sync_audio_mode)
        audio_grid.addWidget(QLabel(_('Audio mode')), 3, 0)
        audio_grid.addWidget(self.audio_mode, 3, 1)
        self.audio_codec = QComboBox()
        for label, value in [(_('Use global setting'), 'copy'), ('E-AC3', 'eac3'), ('AC3', 'ac3'), ('AAC', 'aac'), ('Opus', 'opus'), ('FLAC', 'flac')]:
            self.audio_codec.addItem(label, value)
        self.audio_codec.setCurrentIndex(max(0, self.audio_codec.findData(self.data.get('audio_codec', 'copy'))))
        self.everyday_track = QCheckBox(_('Create everyday audio track according to engine settings'))
        self.everyday_track.setChecked(bool(self.data.get('everyday_track', True)))
        audio_grid.addWidget(QLabel(_('Target audio codec')), 4, 0)
        audio_grid.addWidget(self.audio_codec, 4, 1)
        audio_grid.addWidget(self.everyday_track, 5, 0, 1, 2)
        audio_grid.setRowStretch(6, 1)
        self._sync_audio_mode()
        self.processing_heading.setText(_('Video transcoding in progress'))

    def _sync_audio_mode(self):
        copying = self.audio_mode.currentData() == 'copy'
        if copying:
            self.normalize.setChecked(False)
        self.normalize.setEnabled(not copying)
        self.compression.setEnabled(not copying)
        if hasattr(self, 'audio_codec'):
            self.audio_codec.setEnabled(not copying)
            self.everyday_track.setEnabled(not copying)

    def collect(self):
        data = super().collect()
        data['audio_mode'] = self.audio_mode.currentData()
        data['audio_codec'] = self.audio_codec.currentData()
        data['everyday_track'] = self.everyday_track.isChecked()
        return data


class RemuxPage(WizardPage):
    """Standalone copy-only remux; optimizer remains configurable."""
    def __init__(self, parent=None):
        super().__init__('Remux', parent)
        self.title_label.setText(_('Processing: Remux videos'))
        self.sections[0].hide()  # No video codec or quality setting for streamcopy.
        self.sections[1].hide()  # No bitrate-based recode thresholds.
        # Hide audio DSP controls, keep optimizer and language quotas.
        self.audio_processing_group.hide()
        self.normalize.setChecked(False)
        self.normalize.setEnabled(False)
        self.level.setEnabled(False)
        self.compression.setEnabled(False)
        self.processing_heading.setText(_('Video remuxing in progress'))

    def collect(self):
        data = super().collect()
        data.update(audio_mode='copy', normalize=False, everyday_track=False)
        return data


class AudioNormalizePage(WizardPage):
    """Video streamcopy, audio always re-encoded with optional source codec."""
    def __init__(self, parent=None):
        super().__init__('Audio-Normalisieren', parent)
        self.title_label.setText(_('Processing: Normalize audio'))
        self.sections[0].hide()
        self.sections[1].hide()
        # Audio-Normalisierung bearbeitet ausnahmslos alle vorhandenen Tonspuren.
        self.optimizer_group.hide()
        self.audio_codec = QComboBox()
        for label, value in [(_('Keep source codec (re-encode)'), 'source'), ('AC-3', 'ac3'), ('AAC', 'aac'), ('MP3', 'mp3'), ('FLAC', 'flac'), ('Opus', 'opus'), ('DTS', 'dts'), ('E-AC3', 'eac3')]:
            self.audio_codec.addItem(label, value)
        self.audio_codec.setCurrentIndex(max(0, self.audio_codec.findData(self.data.get('audio_codec', 'source'))))
        self.sections[2].content.layout().addWidget(QLabel(_('Target audio codec')))
        self.sections[2].content.layout().addWidget(self.audio_codec)
        self.normalize.setChecked(bool(self.data.get('normalize', True)))
        self.processing_heading.setText(_('Audio normalization running'))

    def collect(self):
        data = super().collect()
        data.update(audio_mode='recode', audio_codec=self.audio_codec.currentData(), everyday_track=False, audio_optimizer=False, subtitle_optimizer=False)
        return data


class AppendPage(WizardPage):
    """V1-like simple source/target UI; common progress and abort facilities."""
    def __init__(self, parent=None):
        super().__init__('Videos zusammenfügen', parent)
        self.title_label.setText(_('Processing: Join videos'))
        self.settings_scroll.hide()
        self.back_button.hide()
        # Ohne Einstellungen darf der freie Platz nicht zwischen Titel, Ordnern und Startleiste landen.
        # Ein Streckelement NACH der Bedienleiste hält alle Elemente oben wie bei den anderen Assistenten.
        self.layout().addStretch(1)
        self.processing_heading.setText(_('Joining videos'))

    def collect(self):
        return {'source': self.source.text().strip(), 'target': self.target.text().strip()}

    def show_setup(self):
        super().show_setup()
        self.settings_scroll.hide()
        self.back_button.hide()
