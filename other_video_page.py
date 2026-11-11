from activity_log import event as activity_event
"""Test 138a GUI: unattended other-video artwork and NFO batch."""
from pathlib import Path
from PySide6.QtCore import QThread, Signal, QSettings, Qt
from PySide6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QGridLayout,QFormLayout,QLabel,QLineEdit,
    QPushButton,QFileDialog,QCheckBox,QComboBox,QPlainTextEdit,QGroupBox,QMessageBox,
    QToolButton,QScrollArea,QSizePolicy,QFrame)
from other_video_scraper import ROOT
from metadata_page import ScraperWorker
import metadata_runtime_ui as runtime_ui
from gui_i18n import _
from metadata_page import StatusRelayLabel

class Worker(QThread):
    message=Signal(str)
    completed=Signal(dict)
    failed=Signal(str)
    def __init__(self,source,options):
        super().__init__();self.source=source;self.options=options;self.stop_requested=False
    def run(self):
        try:self.completed.emit(batch(self.source,self.source,self.options,self.message.emit,lambda:self.stop_requested))
        except Exception as e:self.failed.emit(str(e))

from collapsible_settings import CollapsibleSettings

class OtherVideoPage(QWidget):
    def __init__(self,parent=None):
        super().__init__(parent)
        self.worker=None
        self.kind='Sonstige Videos'
        main=QVBoxLayout(self)
        main.setContentsMargins(4,4,4,4)
        main.setSpacing(5)
        title=QLabel(_('Metadata: Videos'))
        title.setObjectName('wizardTitle')
        main.addWidget(title)
        paths=QGroupBox(_('Video folder'))
        pathgrid=QGridLayout(paths)
        self.source=QLineEdit()
        self.source.setPlaceholderText(_('Folder containing finished videos'))
        pathgrid.addWidget(QLabel(_('Video folder')),0,0)
        pathgrid.addWidget(self.source,0,1)
        browse=QPushButton(_('Browse …'))
        browse.clicked.connect(lambda:self.choose_dir(self.source))
        pathgrid.addWidget(browse,0,2)
        # Reserve exactly the second folder row of the two-folder assistant.
        pathgrid.setRowMinimumHeight(1,36)
        main.addWidget(paths)

        # Independent accordion sections, like the existing series assistant.
        self.poster=QCheckBox(_('Create poster'));self.poster.setChecked(True)
        self.fanart=QCheckBox(_('Create fanart'));self.fanart.setChecked(True)
        self.nfo=QCheckBox(_('Create NFO'));self.nfo.setChecked(True)
        self.poster_count=QComboBox();self.poster_count.addItems([_('1 frame'),_('2 frames')])
        self.fanart_count=QComboBox();self.fanart_count.addItems([_('1 frame (new)'),_('2 frames'),_('3 frames'),_('4 frames'),_('5 frames'),_('6 frames')])
        self.fanart_count.setCurrentIndex(5)
        self.genre_folder=QCheckBox(_('Use folder name as genre'));self.genre_folder.setChecked(True)
        self.mediainfo=QCheckBox(_('Use MediaInfo as plot'));self.mediainfo.setChecked(True)
        self.genre=QLineEdit();self.genre.setPlaceholderText(_('Default genre (optional)'))
        self.plot=QLineEdit();self.plot.setPlaceholderText(_('Default plot (optional)'))
        self.cover_template=QComboBox();self.back_template=QComboBox();self.border_template=QComboBox()
        self.cover_template.addItem(_('Video frame (blurred)'),None)
        self.back_template.addItem(_('Video frame (blurred)'),None)
        self.border_template.addItem(_('Simple light border'),None)
        for widget,folder in ((self.cover_template,'Cover'),(self.back_template,'Backdrop'),(self.border_template,'Border')):
            for file in sorted((ROOT/'Templates'/folder).glob('*')):
                if file.suffix.lower() in ('.png','.jpg','.jpeg','.webp'):
                    widget.addItem(file.name,str(file))

        poster_content=QWidget();poster_layout=QFormLayout(poster_content)
        poster_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        poster_layout.addRow(self.poster)
        poster_layout.addRow(_('Frames:'),self.poster_count)
        fanart_content=QWidget();fanart_layout=QFormLayout(fanart_content)
        fanart_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        fanart_layout.addRow(self.fanart)
        fanart_layout.addRow(_('Collage:'),self.fanart_count)
        nfo_content=QWidget();nfo_layout=QFormLayout(nfo_content)
        nfo_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        nfo_layout.addRow(self.nfo)
        nfo_layout.addRow(self.genre_folder)
        nfo_layout.addRow(_('Default genre:'),self.genre)
        nfo_layout.addRow(self.mediainfo)
        nfo_layout.addRow(_('Default plot:'),self.plot)
        design_content=QWidget();design_layout=QFormLayout(design_content)
        design_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        design_layout.addRow(_('Poster background:'),self.cover_template)
        design_layout.addRow(_('Fanart background:'),self.back_template)
        design_layout.addRow(_('Border:'),self.border_template)

        self.sections=[]
        for title,content,opened in (
            (_('Poster'),poster_content,True),(_('Fanart'),fanart_content,False),
            (_('NFO and metadata'),nfo_content,False),(_('Backgrounds and borders'),design_content,False)):
            section=CollapsibleSettings(title,content,opened)
            self.sections.append(section)
        scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setFrameShape(QFrame.Shape.NoFrame)
        body=QWidget();body_layout=QVBoxLayout(body)
        body_layout.setContentsMargins(0,0,4,0);body_layout.setSpacing(7)
        for section in self.sections:body_layout.addWidget(section)
        body_layout.addStretch(1)
        scroll.setWidget(body)
        for combo in (self.poster_count,self.fanart_count,self.cover_template,self.back_template,self.border_template):
            combo.setMinimumHeight(30)
            combo.setMinimumWidth(270)
            combo.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Fixed)
        for edit in (self.source,self.genre,self.plot):edit.setMinimumHeight(30)

        bar=QHBoxLayout()
        self.stop=QPushButton();self.stop.hide();self.stop.setEnabled(False)
        self.stop_now=QPushButton();self.stop_now.hide();self.stop_now.setEnabled(False)
        bar.addStretch(1)
        self.save_button=QPushButton(_('Save settings'))
        self.save_button.clicked.connect(self.save)
        bar.addWidget(self.save_button)
        self.start=QPushButton(_('Start'))
        self.start.setMinimumHeight(38)
        self.start.setMinimumWidth(145)
        self.start.setStyleSheet('QPushButton { background:#1769ad; color:white; font-weight:bold; border-radius:5px; padding:7px 18px; } QPushButton:hover { background:#0d568f; }')
        self.start.clicked.connect(self.run_batch)
        bar.addWidget(self.start)
        self.actions_widget=QWidget()
        self.actions_widget.setLayout(bar)
        main.addWidget(self.actions_widget)
        main.addWidget(scroll,1)
        # Keep the log receiver for worker messages without the unused large panel.
        self.log=QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.hide()
        self.status=StatusRelayLabel(_('Idle'))
        self.status.hide()
        runtime_ui.install(self, main)
        self._settings=QSettings('BuddyTools','Media-Buddy-V2')
        self._restore_preferences()
        self._connect_preferences()
    def _fields(self):
        return {'videoordner':self.source,'genre':self.genre,'plot':self.plot,
                'poster':self.poster,'fanart':self.fanart,'nfo':self.nfo,
                'genre_folder':self.genre_folder,'mediainfo':self.mediainfo,
                'poster_count':self.poster_count,'fanart_count':self.fanart_count,
                'cover_template':self.cover_template,'back_template':self.back_template,
                'border_template':self.border_template}

    def _restore_preferences(self):
        for name,w in self._fields().items():
            key='metadata/other_videos/'+name
            value=self._settings.value(key,None)
            if value is None:continue
            if isinstance(w,QLineEdit):w.setText(str(value))
            elif isinstance(w,QCheckBox):w.setChecked(str(value).lower() in ('true','1'))
            elif isinstance(w,QComboBox):
                if name.endswith('_template'):
                    idx=w.findData(value if value!='' else None)
                else:
                    try:idx=int(value)
                    except (ValueError,TypeError):idx=-1
                if 0<=idx<w.count():w.setCurrentIndex(idx)

    def _save_preferences(self):
        for name,w in self._fields().items():
            key='metadata/other_videos/'+name
            if isinstance(w,QLineEdit):value=w.text()
            elif isinstance(w,QCheckBox):value=w.isChecked()
            elif isinstance(w,QComboBox):
                value=w.currentData() if name.endswith('_template') else w.currentIndex()
                if value is None:value=''
            self._settings.setValue(key,value)

    def save(self, silent=False):
        """Compatibility with MainWindow.closeEvent; preferences also autosave."""
        self._save_preferences()
        self._settings.sync()

    def _connect_preferences(self):
        for w in self._fields().values():
            if isinstance(w,QLineEdit):w.textChanged.connect(self._save_preferences)
            elif isinstance(w,QCheckBox):w.toggled.connect(self._save_preferences)
            elif isinstance(w,QComboBox):w.currentIndexChanged.connect(self._save_preferences)

    def choose_dir(self,edit):
        p=QFileDialog.getExistingDirectory(self,_('Select folder'),edit.text())
        if p:edit.setText(p)
    def run_batch(self):
        source=self.source.text().strip()
        if not source:
            QMessageBox.warning(self,_('Missing folder'),_('Please select a video folder.'));return
        opts=dict(make_poster=self.poster.isChecked(),make_fanart=self.fanart.isChecked(),make_nfo=self.nfo.isChecked(),
            poster_count=self.poster_count.currentIndex()+1,fanart_count=self.fanart_count.currentIndex()+1,
            genre_from_folder=self.genre_folder.isChecked(),mediainfo_plot=self.mediainfo.isChecked(),
            genre=self.genre.text(),plot=self.plot.text(),cover_template=self.cover_template.currentData(),
            backdrop_template=self.back_template.currentData(),border_template=self.border_template.currentData())
        self.log.clear();self.status.setText(_('Processing …'))
        self.start.setEnabled(False);self.stop.setEnabled(True);self.stop_now.setEnabled(True)
        self._running=True
        runtime_ui.started(self,source)
        activity_event(f'Sonstige Videos: Metadatenbearbeitung gestartet; Quelle={source}')
        self.worker=ScraperWorker(self.kind,source,opts)
        self.worker.message.connect(self.log.appendPlainText)
        self.worker.message.connect(self.status.setText)
        self.worker.progress.connect(lambda event:runtime_ui.update(self,event))
        self.worker.finished_run.connect(self.done)
        self.worker.start()
    def request_stop(self):
        if not self.worker: return
        answer=QMessageBox.question(self,_('Stop processing'),
            _('Stop after the current file?'),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes: return
        self.worker.request_stop()
        self.stop.setEnabled(False)
        self.runtime_stop.setEnabled(False)
        self.status.setText(_('Stop after the current file requested …'))

    def request_immediate_stop(self):
        if not self.worker: return
        answer=QMessageBox.question(self,_('Stop immediately'),
            _('Stop processing immediately?\nThe current file may be incomplete.'),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes: return
        self.worker.request_immediate_stop()
        self.stop.setEnabled(False);self.stop_now.setEnabled(False)
        self.runtime_stop.setEnabled(False);self.runtime_stop_now.setEnabled(False)
        self.status.setText(_('Immediate stop requested…'))

    def done(self,code,stopped):
        status = 'abgebrochen' if stopped else 'erfolgreich beendet' if code == 0 else f'Fehler (RC={code})'
        self.status.setText(_('Other videos: {status}').format(status=_('Stopped') if stopped else _('Completed successfully') if code == 0 else _('Error (RC={code})').format(code=code)))
        activity_event(f'Sonstige Videos: Metadatenbearbeitung {status}',
                       'WARN' if stopped or code else 'INFO')
        self.start.setEnabled(True);self.stop.setEnabled(False);self.stop_now.setEnabled(False)
        self._running=False
        runtime_ui.finished(self,code==0,stopped)
