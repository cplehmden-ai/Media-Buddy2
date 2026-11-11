"""Standalone metadata pages: shared one-folder layout, independent scraper execution."""
from __future__ import annotations
from pathlib import Path
import subprocess
import sys
import json
import re
import os
import tempfile
import shutil
import signal
import threading
from PySide6.QtCore import QThread, Signal, QSettings
from PySide6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QGridLayout,QLabel,QLineEdit,
    QPushButton,QFileDialog,QCheckBox,QGroupBox,QScrollArea,QFrame,QMessageBox,QProgressBar)
from collapsible_settings import CollapsibleSettings
from activity_log import event as activity_event
import metadata_runtime_ui as runtime_ui
from gui_i18n import _, navigation_label
from config_manager import ConfigManager

ROOT=Path(__file__).resolve().parent
MODULES={"Serien":"series_scraper.py","Spielfilme":"movie_scraper.py","Musikvideos":"musicvideo_scraper.py"}
CONFIGS={"Serien":"series_scraper_config.json","Spielfilme":"movie_scraper_config.json","Musikvideos":"musicvideo_scraper_config.json"}

class ScraperWorker(QThread):
    message=Signal(str)
    progress=Signal(dict)
    finished_run=Signal(int,bool)
    def __init__(self,kind,folder,overrides):
        super().__init__()
        self.kind=kind;self.folder=folder;self.overrides=overrides;self.stop_requested=False;self.immediate_requested=False
        self.process=None
    def run(self):
        # Each scraper runs in its own process: native logging remains intact.
        # A stop marker is checked by the scraper only between media files.
        self.stop_dir=tempfile.mkdtemp(prefix="mb_metadata_stop_")
        marker=str(Path(self.stop_dir)/"stop")
        if self.stop_requested:
            Path(marker).touch()
        try:
            cmd=[sys.executable, '-u', str(ROOT/'metadata_scraper_runner.py'),
                 self.kind, self.folder, json.dumps(self.overrides, ensure_ascii=False)]
            env=os.environ.copy()
            env["MB_METADATA_STOP_FILE"]=marker
            # The GUI reads the child process as UTF-8. Force the same encoding
            # for Python stdout on Windows, where redirected pipes may use cp1252.
            env["PYTHONIOENCODING"]="utf-8:replace"
            self.process=subprocess.Popen(cmd,cwd=str(ROOT),stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,text=True,encoding="utf-8",errors="replace",bufsize=1,
                env=env, start_new_session=(os.name != "nt"),
                creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0))
            for line in self.process.stdout:
                line = line.rstrip()
                if line.startswith('[MB_METADATA_PROGRESS] '):
                    try: self.progress.emit(json.loads(line[len('[MB_METADATA_PROGRESS] '):]))
                    except (ValueError,TypeError): pass
                else:
                    self.message.emit(line)
            rc=self.process.wait()
            self.finished_run.emit(int(rc or 0),self.stop_requested or self.immediate_requested)
        except Exception as exc:
            self.message.emit(f"[ERROR] {exc}")
            self.finished_run.emit(10,self.stop_requested or self.immediate_requested)
        finally:
            shutil.rmtree(self.stop_dir,ignore_errors=True)
    def request_immediate_stop(self):
        self.immediate_requested=True
        self.stop_requested=True
        process=self.process
        if process is not None and process.poll() is None:
            try:
                if os.name == 'nt':
                    subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],
                                   capture_output=True, timeout=10)
                else:
                    os.killpg(process.pid, signal.SIGTERM)
            except (OSError,subprocess.SubprocessError) as exc:
                self.message.emit(f'[STOP WARN] {exc}')
                try: process.terminate()
                except OSError: pass

    def request_stop(self):
        self.stop_requested=True
        if getattr(self,"stop_dir",None):
            Path(self.stop_dir,"stop").touch()


class StatusRelayLabel(QLabel):
    """Retain the worker's status receiver but display it in the main status bar."""
    def setText(self, value):
        super().setText(value)
        host = self.window()
        if hasattr(host, '_status_text') and not getattr(self.parent(), '_running', False):
            host._status_text = str(value)
            if hasattr(host, '_refresh_status'):
                host._refresh_status()

class MetadataPage(QWidget):
    def __init__(self,kind,parent=None):
        super().__init__(parent)
        self.kind=kind;self.worker=None
        self.prefs=QSettings("BuddyTools","Media-Buddy-V2")
        root=QVBoxLayout(self);root.setContentsMargins(4,4,4,4);root.setSpacing(5)
        heading=QLabel(_('Metadata: {kind}').format(kind=navigation_label(kind)))
        heading.setObjectName("wizardTitle");root.addWidget(heading)
        paths=QGroupBox(_('Media folder'))
        grid=QGridLayout(paths)
        self.source=QLineEdit()
        self.source.setMinimumHeight(30)
        self.source.setText(str(self.prefs.value(f"metadata/{kind}/folder","")))
        grid.addWidget(QLabel({'Serien': _('TV series folder'), 'Spielfilme': _('Movie folder'), 'Musikvideos': _('Music video folder')}[kind]),0,0)
        grid.addWidget(self.source,0,1)
        browse=QPushButton(_('Browse …'))
        browse.clicked.connect(self.browse)
        grid.addWidget(browse,0,2)
        grid.setRowMinimumHeight(1,36)
        root.addWidget(paths)
        action=QHBoxLayout()
        # The stop buttons belong exclusively to the running view.
        self.stop=QPushButton();self.stop.hide();self.stop.setEnabled(False)
        self.stop_now=QPushButton();self.stop_now.hide();self.stop_now.setEnabled(False)
        action.addStretch(1)
        self.save_button=QPushButton(_('Save settings'))
        self.save_button.clicked.connect(self.save_preferences)
        action.addWidget(self.save_button)
        self.start=QPushButton(_('Start'))
        self.start.setMinimumHeight(38);self.start.setMinimumWidth(145)
        self.start.setStyleSheet('QPushButton { background:#1769ad; color:white; font-weight:bold; border-radius:5px; padding:7px 18px; } QPushButton:hover { background:#0d568f; }')
        self.start.clicked.connect(self.run_scraper)
        action.addWidget(self.start)
        controls=QWidget();controls.setLayout(action);root.addWidget(controls)
        scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner=QWidget();lay=QVBoxLayout(inner)
        # Use the same effective defaults as the scrapers, not a potentially absent JSON file.
        try:
            config=ConfigManager(ROOT, CONFIGS[kind]).effective
        except Exception:
            config={}
        # The metadata pages mirror the wizard layout, but retain independent
        # metadata/<kind>/options QSettings values and scraper overrides.
        image_fields={
            'Serien': [(("TVShow/Image_Fanart",),_('Fanart / Background')),
                       (("TVShow/Image_Banner",),_('Banner')),
                       (("TVShow/Image_Poster",),_('Poster / Cover')),
                       (("TVShow/Image_ClearLogo","TVShow/Image_ClearArt"),_('Clearart / Logo')),
                       (("TVShow/Image_Episode",),_('Thumbnail')),
                       (("TVShow/Image_SeasonPoster", "TVShow/Image_SeasonBanner"),_('Season poster / banner'))],
            'Spielfilme': [(("Movies/Image_Backdrop",),_('Fanart / Background')),
                           (("Movies/Image_Banner",),_('Banner')),
                           (("Movies/Image_Poster",),_('Poster / Cover')),
                           (("Movies/Image_ClearLogo","Movies/Image_ClearArt"),_('Clearart / Logo')),
                           (("Movies/Trailer_Download",),_('Download trailers')),
                           (("Download_Movieset_Images",),_('Download movie set artwork'))],
            'Musikvideos': [(("MusicVideos/Image_Fanart",),_('Fanart / Background')),
                            (("MusicVideos/Image_Banner",),_('Banner')),
                            (("MusicVideos/Image_Poster",),_('Poster / Cover')),
                            (("MusicVideos/Image_ClearLogo","MusicVideos/Image_ClearArt"),_('Clearart / Logo')),
                            (("MusicVideos/Image_Thumb",),_('Music video thumbnail'))],
        }
        self.checks={}
        self.combined_image_keys={}
        def add_check(layout, keys, label):
            primary=keys[0]
            value=config
            for part in primary.split("/"):
                value=value.get(part,{}) if isinstance(value,dict) else {}
            default=value if isinstance(value,bool) else True
            cb=QCheckBox(label)
            cb.setObjectName(primary)
            saved=self.prefs.value(f"metadata/{kind}/options/{primary}",None)
            cb.setChecked(bool(default) if saved is None else str(saved).lower() in ("true","1"))
            layout.addWidget(cb)
            self.checks[primary]=cb
            if len(keys)>1:
                self.combined_image_keys[primary]=keys[1:]
            return cb
        section_body=QWidget()
        row=QHBoxLayout(section_body)
        row.setContentsMargins(5,8,5,8)
        row.setSpacing(12)
        metadata=QGroupBox(_('Metadata and artwork'))
        left=QVBoxLayout(metadata)
        for keys,label in [(("Download_Images",),_('Download artwork')),
                           (("Overwrite/Artwork",),_('Replace artwork')),
                           (("Overwrite/NFO",),_('Replace NFO'))]:
            add_check(left,keys,label)
        if kind=="Spielfilme":
            left.addWidget(QLabel(_('Source: TMDB')))
        elif kind=="Musikvideos":
            left.addWidget(QLabel(_('Data sources: MusicBrainz / TheAudioDB / Last.fm / Fanart.tv')))
        left.addStretch()
        row.addWidget(metadata,1)
        artwork=QGroupBox(_('Which artwork to download?'))
        right=QVBoxLayout(artwork)
        for keys,label in image_fields[kind]:
            add_check(right,keys,label)
        right.addStretch()
        row.addWidget(artwork,1)
        lay.addWidget(CollapsibleSettings(_('Metadata and artwork'),section_body,False))
        lay.addStretch()
        scroll.setWidget(inner);root.addWidget(scroll,1)
        self.status=StatusRelayLabel(_('Idle'), self);self.status.hide()
        runtime_ui.install(self, root)
        self.source.textChanged.connect(lambda value:self.prefs.setValue(f"metadata/{kind}/folder",value))
    def save_preferences(self):
        # Persist the existing folder key and the editable options in QSettings.
        self.prefs.setValue(f"metadata/{self.kind}/folder",self.source.text())
        for key, cb in self.checks.items():
            self.prefs.setValue(f"metadata/{self.kind}/options/{key}",cb.isChecked())
            for related in self.combined_image_keys.get(key,()):
                self.prefs.setValue(f"metadata/{self.kind}/options/{related}",cb.isChecked())
        self.prefs.sync()

    def browse(self):
        folder=QFileDialog.getExistingDirectory(self,_('Select media folder'),self.source.text())
        if folder:self.source.setText(folder)
    def overrides(self):
        out={}
        for key,cb in self.checks.items():
            for option in (key,*self.combined_image_keys.get(key,())):
                if "/" in option:
                    section,field=option.split("/",1)
                    out.setdefault(section,{})[field]=cb.isChecked()
                else:out[option]=cb.isChecked()
        return out
    def run_scraper(self):
        folder=self.source.text().strip()
        if not Path(folder).is_dir():
            QMessageBox.warning(self,_('Folder missing'),_('Please select an existing media folder.'))
            return
        self.start.setEnabled(False);self.stop.setEnabled(True);self.stop_now.setEnabled(True)
        self._running=True
        runtime_ui.started(self, folder)
        self.status.setText(_('Processing …'))
        activity_event(f"{self.kind}: Metadatenbearbeitung gestartet; Ordner={folder}")
        self.worker=ScraperWorker(self.kind,folder,self.overrides())
        self.worker.message.connect(self.on_message)
        self.worker.progress.connect(lambda event:runtime_ui.update(self,event))
        self.worker.finished_run.connect(self.done)
        self.worker.start()
    def on_message(self,message):
        # Raw scraper logs remain in the log; the status bar shows the current stage and elapsed time.
        if message.strip() and not self._running: self.status.setText(message[:220])
    def request_stop(self):
        if self.worker:
            answer=QMessageBox.question(self,_('Stop processing'),
                _('Stop after the current file?\nThe current file will finish processing.'),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
            self.worker.request_stop()
            self._metadata_stage_text = _('Stop requested – finishing current file…')
            runtime_ui._update_status(self)
            self.stop.setEnabled(False)
            self.runtime_stop.setEnabled(False)
            activity_event(f"{self.kind}: Metadaten-Abbruch angefordert","WARN")
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
        self._metadata_stage_text = _('Immediate stop requested…')
        runtime_ui._update_status(self)

    def done(self,code,stopped):
        status="abgebrochen" if stopped else "erfolgreich beendet" if code==0 else f"mit Fehler beendet (RC={code})"
        self.status.setText((_('Stopped') if stopped else _('Completed successfully')) if code == 0 or stopped else _('Finished with error (RC={code})').format(code=code))
        activity_event(f"{self.kind}: Metadatenbearbeitung {status}",
                       "WARN" if stopped or code else "INFO")
        self.start.setEnabled(True);self.stop.setEnabled(False);self.stop_now.setEnabled(False)
        self._running=False
        runtime_ui.finished(self, code==0, stopped)
