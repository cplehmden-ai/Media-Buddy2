"""Test 189 GUI for cautious music album scraper."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import shutil
import time
from PySide6.QtCore import QSettings, QThread, Signal, QTimer, Qt
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QFileDialog, QCheckBox, QProgressBar, QPlainTextEdit, QMessageBox, QScrollArea, QGroupBox, QComboBox, QStackedWidget)
from gui_i18n import _
from standard_entry_page import SingleFolderEntryPage
from system_actions import available_actions
from metadata_runtime_ui import ACTION_LABELS, _power_countdown
from standard_progress_page import StandardProgressPage
from activity_log import event as activity_event

class MusicWorker(QThread):
    line = Signal(str)
    progress = Signal(int, int)
    complete = Signal(int)
    def __init__(self, opts):
        super().__init__()
        self.opts = opts
        self.stop = False
        self.proc = None
        self.tmp = None
    def run(self):
        self.tmp = tempfile.mkdtemp(prefix='mb_music_stop_')
        marker = Path(self.tmp) / 'stop'
        env = os.environ.copy()
        env['MB_METADATA_STOP_FILE'] = str(marker)
        env['PYTHONIOENCODING'] = 'utf-8:replace'
        try:
            self.proc = subprocess.Popen([sys.executable, '-u', str(Path(__file__).with_name('music_album_scraper.py')),
                '--options', json.dumps(self.opts, ensure_ascii=False)], stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', env=env)
            for line in self.proc.stdout:
                line = line.rstrip()
                if line.startswith('[MB_METADATA_PROGRESS] '):
                    try:
                        data = json.loads(line.split('] ', 1)[1])
                        self.progress.emit(int(data['current']), int(data['total']))
                    except (ValueError, KeyError):
                        pass
                else:
                    self.line.emit(line)
            self.complete.emit(self.proc.wait())
        except Exception as exc:
            self.line.emit('[ERROR] ' + str(exc))
            self.complete.emit(1)
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)
    def request_immediate_stop(self):
        self.request_stop()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def request_stop(self):
        self.stop = True
        if self.tmp:
            (Path(self.tmp) / 'stop').touch()

class MusicScraperPage(QWidget):
    """Metadata controls; unavailable operations are visibly disabled."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.settings=QSettings('BuddyTools','Media-Buddy-V2')
        self.worker=None
        outer=QVBoxLayout(self)
        outer.setContentsMargins(12,12,12,12)
        self.views=QStackedWidget(self)
        outer.addWidget(self.views,1)
        self.source=QLineEdit(str(self.settings.value('music_scraper/source','')))
        setup=SingleFolderEntryPage(
            _('Metadata: Music'), _('Music folder'), self.source,
            lambda: self.browse(self.source), self.start, self.save)
        self.views.addWidget(setup)
        self.save_btn=setup.save_button
        self.start_btn=setup.start_button
        group=setup.add_group
        def check(layout,key,label,default,enabled=True):
            widget=QCheckBox(_(label))
            widget.setChecked(str(self.settings.value('music_scraper/'+key,str(default).lower())).lower()=='true')
            widget.setEnabled(enabled)
            layout.addWidget(widget)
            return widget
        meta=group(_('MusicBrainz metadata and tagging'))
        self.lookup=check(meta,'lookup','Search MusicBrainz when no release ID is present',True)
        self.tagging=check(meta,'tagging','Write MusicBrainz metadata to audio files',False)
        lyrics=group(_('Song lyrics'))
        self.lyrics=check(lyrics,'lyrics','Download lyrics from LRCLIB',True)
        self.plain=check(lyrics,'plain','Use plain lyrics when synchronized lyrics are unavailable',True)
        self.plain.setEnabled(self.lyrics.isChecked())
        self.lyrics.toggled.connect(self.plain.setEnabled)
        self.embed_lyrics=check(lyrics,'embed_lyrics','Embed lyrics in audio tags',False)
        self.clean_lyrics=check(lyrics,'clean_lyrics','Clean unsafe HTML and JavaScript from existing lyrics',False)
        artwork=group(_('Album and artist artwork'))
        self.album_artwork=check(artwork,'album_artwork','Download missing album covers and embed in audio files',False)
        self.artist_artwork=check(artwork,'artist_artwork','Download missing artist artwork to configured locations',False)
        self.music_nfo=check(artwork,'music_nfo','Create Kodi artist.nfo and album.nfo',False)
        self.combined_artist_nfo=check(artwork,'combined_artist_nfo','Create combined artist NFOs for collaborations',False)
        names=group(_('Files and folders'))
        self.rename=check(names,'rename','Rename audio files',False)
        self.organize=check(names,'organize','Organize artist and album folders',False)
        self.disc_folders=check(names,'disc_folders','Create disc and bonus disc folders',False)
        names.addWidget(QLabel(_('Naming patterns and artwork locations are configured under Settings → Audio.')))

        # Standard progress panel; all diagnostic output is written to the
        # application log, not displayed as a GUI transcript.
        running=StandardProgressPage(_('Processing metadata: Music'),
            [(action, ACTION_LABELS[action]) for action in available_actions()])
        self.views.addWidget(running)
        self.stage=running.stage
        self.progress=running.progress
        self.current_progress=running.current_progress
        self.elapsed=running.elapsed
        self.eta=running.eta
        self.stop_btn=running.stop_btn
        self.stop_now_btn=running.stop_now_btn
        self.end_action=running.end_action
        self.stop_btn.clicked.connect(self.stop)
        self.stop_now_btn.clicked.connect(self.stop_now)
        self.clock=QTimer(self)
        self.clock.timeout.connect(self.update_elapsed)
        self.started_at=0.0
        self.stopped=False

    def browse(self, field):
        path=QFileDialog.getExistingDirectory(self,_('Select folder'),field.text())
        if path:field.setText(path)

    def music_settings(self):
        from configuration_store import effective
        from pathlib import Path
        config=effective(Path(__file__).with_name('Media-Buddy.json'))
        return config.get('Audio',{}).get('Music',{}),config.get('Audio',{}).get('ArtistArtworkPath','')

    def options(self):
        music,artwork=self.music_settings()
        return dict(source=self.source.text().strip(),artist_artwork_root=artwork,
                    lyrics=self.lyrics.isChecked(),plain_lyrics=self.plain.isChecked(),
                    embed_lyrics=self.embed_lyrics.isChecked(),clean_lyrics=self.clean_lyrics.isChecked(),
                    lookup=self.lookup.isChecked(),rename=self.rename.isChecked(),
                    album_artwork=self.album_artwork.isChecked(),
                    embed_album_cover=self.album_artwork.isChecked(),
                    artist_artwork=self.artist_artwork.isChecked(),music_nfo=self.music_nfo.isChecked(),
                    combined_artist_nfo=self.combined_artist_nfo.isChecked(),
                    write_tags=self.tagging.isChecked(),
                    organize=self.organize.isChecked(),disc_folders=self.disc_folders.isChecked(),
                    artist_folder_pattern=music.get('ArtistFolderPattern','{albumartist}'),
                    album_folder_pattern=music.get('AlbumFolderPattern','{year} - {album}'),
                    disc_folder_pattern=music.get('DiscFolderPattern','CD {disc}'),
                    bonus_disc_folder=music.get('BonusDiscFolder','Bonus CD'),
                    disc_mode=music.get('DiscMode','automatic'),
                    year_mode=music.get('YearMode','original'),
                    filename_pattern=music.get('AlbumTrackPattern','{track} - {title}'),
                    single_filename_pattern=music.get('SingleTrackPattern','{artist} - {title}'),
                    compilation_filename_pattern=music.get('CompilationTrackPattern','{track} - {artist} - {title}'),
                    compilation_artists=music.get('CompilationArtists',['Various Artists','Various Artist','Various']),
                    audio_extensions=music.get('Extensions',['.mp3','.flac','.wav','.m4a','.ogg','.mka']))

    def save(self, silent=False):
        for key,widget in [('lyrics',self.lyrics),('plain',self.plain),('embed_lyrics',self.embed_lyrics),
                           ('clean_lyrics',self.clean_lyrics),('lookup',self.lookup),
                           ('rename',self.rename),('tagging',self.tagging),
                           ('organize',self.organize),('disc_folders',self.disc_folders),
                           ('album_artwork',self.album_artwork),('artist_artwork',self.artist_artwork),
                           ('music_nfo',self.music_nfo),('combined_artist_nfo',self.combined_artist_nfo)]:
            self.settings.setValue('music_scraper/'+key,str(widget.isChecked()).lower())
        self.settings.setValue('music_scraper/source',self.source.text())
        self.settings.sync()

    def update_elapsed(self):
        seconds=int(time.monotonic()-self.started_at)
        self.elapsed.setText(_('Elapsed: {time}').format(time=f'{seconds//60:02d}:{seconds%60:02d}'))
        self._update_status_times()

    def append_line(self,line):
        activity_event('Musik: ' + line, 'ERROR' if line.startswith('[ERROR]') else 'WARN' if line.startswith('[WARN]') else 'INFO')
        if line.startswith(('[TRACK]', '[ALBUM]', '[SINGLES]', '[TAGS]', '[MUSIC ART] Combined artist NFO checked:', '[WARN]', '[ERROR]')):
            self.stage.setText(line)
            self._scraper_status_message = line[:120]
            self._update_status_times()

    def start(self):
        opts=self.options()
        source=Path(opts['source']).expanduser().resolve()
        if not source.is_dir():
            QMessageBox.warning(self,_('Invalid folder'),_('Choose an existing music folder.'))
            return
        self.save()
        activity_event('Musik-Metadaten gestartet: ' + str(source))
        self.progress.setRange(0,0)
        self.current_progress.setRange(0,0)
        self.stage.setText(_('Starting scraper…'))
        self._scraper_status_message = _('Starting scraper…')
        self.eta.setText(_('Estimated remaining: {time}').format(time='--:--'))
        self.stopped=False
        self.started_at=time.monotonic()
        self.update_elapsed()
        self.stop_btn.setEnabled(True)
        self.stop_now_btn.setEnabled(True)
        self.end_action.setCurrentIndex(0)
        self.views.setCurrentIndex(1)
        self.worker=MusicWorker(opts)
        self.worker.line.connect(self.append_line)
        self.worker.progress.connect(self.update_progress)
        self.worker.complete.connect(self.finished)
        self.clock.start(1000)
        self.worker.start()

    def update_progress(self,current,total):
        self.progress.setRange(0,max(total,1))
        self.progress.setValue(max(0,min(current,total)))
        self.progress.setFormat(_('{current} of {total} (%p%)').format(current=current,total=total))
        elapsed = max(0, time.monotonic()-self.started_at)
        remaining = max(0, int(elapsed * (total-current) / current)) if current and total else None
        eta = f'{remaining//60:02d}:{remaining%60:02d}' if remaining is not None else '--:--'
        self.eta.setText(_('Estimated remaining: {time}').format(time=eta))
        self._update_status_times()

    def _update_status_times(self):
        host = self.window()
        if not hasattr(host, '_status_text') or not hasattr(host, '_refresh_status'):
            return
        status = getattr(self, '_scraper_status_message', '')
        host._status_text = (status + '  |  ' if status else '') + self.elapsed.text() + '  |  ' + self.eta.text()
        host._refresh_status()

    def stop(self):
        if self.worker and self.worker.isRunning():
            self.stopped=True
            self.worker.request_stop()
            self.stop_btn.setEnabled(False)
            self.stage.setText(_('Stopping after current album…'))

    def stop_now(self):
        if self.worker and self.worker.isRunning():
            answer=QMessageBox.question(self,_('Cancel immediately'),
                _('Stop the current music processing immediately?'))
            if answer != QMessageBox.StandardButton.Yes:
                return
            self.stopped=True
            self.worker.request_immediate_stop()
            self.stop_btn.setEnabled(False)
            self.stop_now_btn.setEnabled(False)
            self.stage.setText(_('Stopping immediately…'))

    def finished(self,code):
        self.clock.stop()
        self.stop_btn.setEnabled(False)
        self.stop_now_btn.setEnabled(False)
        self.current_progress.setRange(0,1)
        self.current_progress.setValue(1 if code == 0 else 0)
        self.stage.setText(_('Processing finished (exit code: {code})').format(code=code))
        activity_event('Musik: ' + self.stage.text(), 'ERROR' if code else 'INFO')
        action=self.end_action.currentData() if code==0 and not self.stopped else 'none'
        self.end_action.setCurrentIndex(0)
        # Match other metadata pages: return automatically when processing ends.
        self.views.setCurrentIndex(0)
        if action == 'exit':
            self.window().close()
        elif action in ('standby','hibernate','shutdown'):
            _power_countdown(self,action)
