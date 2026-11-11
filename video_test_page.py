"""Unattended, read-only spot-check of completed videos using mpv."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from PySide6.QtCore import QProcess, QTimer, QSettings, Qt, QElapsedTimer
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QLineEdit,
                               QPushButton, QFileDialog, QCheckBox, QProgressBar,
                               QGroupBox, QMessageBox, QDialog)
from gui_i18n import _
from postprocess_entry import make_entry
from tool_manager import get_tool

EXTENSIONS = {'.mkv', '.mp4', '.m4v', '.avi', '.mov', '.wmv', '.webm', '.mpg', '.mpeg', '.ts', '.m2ts'}


class _NextVideoDialog(QDialog):
    """Own the countdown and timer for the whole lifetime of the dialog."""

    def __init__(self, parent, completed, total, next_name):
        super().__init__(parent)
        self.setWindowTitle(_('Video test'))
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setMinimumWidth(370)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"{completed}/{total} — {_('Next video:')}"))
        name = QLabel(next_name)
        name.setWordWrap(True)
        layout.addWidget(name)
        self.countdown_label = QLabel()
        self.countdown_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.countdown_label)
        self.cancel_button = QPushButton(_('Cancel test'))
        self.cancel_button.setDefault(True)
        self.cancel_button.clicked.connect(self.reject)
        layout.addWidget(self.cancel_button)

        self._elapsed = QElapsedTimer()
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(100)
        self._tick_timer.timeout.connect(self._update_countdown)
        self.finished.connect(self._tick_timer.stop)

    def showEvent(self, event):
        super().showEvent(event)
        if not self._elapsed.isValid():
            self._elapsed.start()
            self._update_countdown()
            self._tick_timer.start()
            self.cancel_button.setFocus()

    def _update_countdown(self):
        remaining = max(0, 2000 - self._elapsed.elapsed())
        self.countdown_label.setText(_('Next video in {seconds:.1f} s').format(seconds=remaining / 1000))
        if remaining == 0:
            self._tick_timer.stop()
            self.accept()


class VideoTestPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.files = []
        self.position = 0
        self.segment = 0
        self.failed = []
        self.running = False
        self.waiting = False
        self._pause_dialog = None
        self._token = 0
        self.process = QProcess(self)
        self.process.finished.connect(self._process_finished)
        self.process.errorOccurred.connect(self._process_error)
        self.pause_timer = QTimer(self)
        self.pause_timer.setSingleShot(True)
        self.pause_timer.timeout.connect(self._next_file)
        self.watchdog = QTimer(self)
        self.watchdog.setSingleShot(True)
        self.watchdog.timeout.connect(self._timeout)

        # Match the other post-processing entry pages exactly: the same outer
        # margins, stacked-page container and shared entry layout.
        root = QVBoxLayout(self)
        root.setContentsMargins(5, 5, 5, 5)
        root.setSpacing(0)
        self.folder = QLineEdit()
        entry = make_entry(self.folder, self._browse, self._start,
                           _('Post-processing: Video test'), _('Video folder'))
        self.start = next(button for button in entry.findChildren(QPushButton)
                          if button.text() == _('Start'))
        root.addWidget(entry)
        # Extra controls live below the identical title/folder/start section.
        # Insert before the shared entry's bottom stretch, not above its start.
        layout = entry.layout()
        self.recursive = QCheckBox(_('Include subfolders'))
        self.recursive.setChecked(True)
        layout.insertWidget(layout.count() - 1, self.recursive)
        self.info = QLabel(_('Ready for automatic video testing.'))
        self.info.setWordWrap(True)
        layout.insertWidget(layout.count() - 1, self.info)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        layout.insertWidget(layout.count() - 1, self.progress)
        self.stop = QPushButton(_('Cancel test'))
        self.stop.clicked.connect(self._stop)
        self.stop.setEnabled(False)
        layout.insertWidget(layout.count() - 1, self.stop)
        prefs = QSettings('BuddyTools', 'Media-Buddy-V2')
        self.folder.setText(str(prefs.value('video_test/folder', '')))
        self.recursive.setChecked(str(prefs.value('video_test/recursive', 'true')).lower() in ('true', '1'))

    def _browse(self):
        folder = QFileDialog.getExistingDirectory(self, _('Select video folder'), self.folder.text())
        if folder:
            self.folder.setText(folder)

    def _start(self):
        folder = Path(self.folder.text().strip()).expanduser()
        if not folder.is_dir():
            QMessageBox.warning(self, _('Video test'), _('Please select an existing video folder.'))
            return
        try:
            self.mpv = get_tool('mpv')
            self.ffprobe = get_tool('ffprobe')
        except (FileNotFoundError, OSError) as exc:
            QMessageBox.warning(self, _('Video test'), str(exc))
            return
        try:
            iterator = folder.rglob('*') if self.recursive.isChecked() else folder.iterdir()
            self.files = sorted((p for p in iterator if p.is_file() and p.suffix.lower() in EXTENSIONS),
                                key=lambda p: str(p).casefold())
        except OSError as exc:
            QMessageBox.warning(self, _('Video test'), str(exc))
            return
        if not self.files:
            QMessageBox.information(self, _('Video test'), _('No video files found.'))
            return
        prefs = QSettings('BuddyTools', 'Media-Buddy-V2')
        prefs.setValue('video_test/folder', str(folder))
        prefs.setValue('video_test/recursive', self.recursive.isChecked())
        self.failed = []
        self.position = 0
        self.segment = 0
        self.running = True
        self.waiting = False
        self.start.setEnabled(False)
        self.stop.setEnabled(True)
        self.folder.setEnabled(False)
        self.recursive.setEnabled(False)
        self.progress.setRange(0, len(self.files))
        self.progress.setValue(0)
        self._play_file()

    def _duration(self, file):
        try:
            result = subprocess.run([self.ffprobe, '-v', 'error', '-show_entries',
                                     'format=duration', '-of', 'json', str(file)],
                                    capture_output=True, timeout=12, check=False)
            if result.returncode != 0:
                return None
            duration = float(json.loads(result.stdout.decode('utf-8'))['format']['duration'])
            return duration if duration > 0 else None
        except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
            return None

    def _play_file(self):
        if not self.running:
            return
        if self.position >= len(self.files):
            self._finish()
            return
        file = self.files[self.position]
        if not file.is_file():
            self._skip(_('File no longer exists'))
            return
        duration = self._duration(file)
        if duration is None:
            self._skip(_('Could not determine duration'))
            return
        self._spots = [(max(0, duration / 2 - 2.5), min(5, duration)),
                       (max(0, duration - 5), min(5, duration))]
        self.segment = 0
        self._launch_segment()

    def _launch_segment(self):
        if not self.running:
            return
        file = self.files[self.position]
        start, length = self._spots[self.segment]
        part = _('Middle') if self.segment == 0 else _('End')
        self.info.setText(f'{self.position + 1}/{len(self.files)}: {file.name} — {part} (5 s)')
        self._token += 1
        self.process.setProgram(self.mpv)
        self.process.setArguments(['--no-config', '--no-terminal', '--no-osc', '--really-quiet',
                                   '--force-window=immediate', '--keep-open=no',
                                   f'--start={start:.3f}', f'--length={length:.3f}', str(file)])
        self.process.start()
        # Avoid stalling forever on a broken file or player. Seek can take time.
        self.watchdog.start(45000)

    def _process_error(self, error):
        if self.running and not self.waiting:
            self.watchdog.stop()
            self._skip(_('Could not start player'))

    def _process_finished(self, exit_code, exit_status):
        self.watchdog.stop()
        if not self.running or self.waiting:
            return
        if exit_code != 0 or exit_status != QProcess.ExitStatus.NormalExit:
            self._skip(_('Playback failed'))
            return
        if self.segment == 0:
            self.segment = 1
            self._launch_segment()
        else:
            self._pause()

    def _timeout(self):
        if self.running:
            self._skip(_('Playback timed out'))

    def _skip(self, reason):
        if not self.running or self.waiting:
            return
        self.failed.append(f'{self.files[self.position].name}: {reason}')
        self.waiting = True
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.process.kill()
            self.process.waitForFinished(1500)
        self._pause()

    def _pause(self):
        if not self.running:
            return
        self.waiting = True
        self.progress.setValue(self.position + 1)
        if self.position + 1 >= len(self.files):
            self._finish()
            return

        next_name = self.files[self.position + 1].name
        self.info.setText(f"{self.position + 1}/{len(self.files)} — {_('Next video:')} {next_name}")

        # The dialog owns its QTimer and the connected timeout slot. Unlike a
        # local closure, neither can disappear while the dialog is visible.
        dialog = _NextVideoDialog(self, self.position + 1, len(self.files), next_name)
        self._pause_dialog = dialog

        def finished(result):
            if self._pause_dialog is dialog:
                self._pause_dialog = None
            dialog.deleteLater()
            if not self.running:
                return
            if result == QDialog.DialogCode.Accepted:
                self._next_file()
            else:
                self._stop()

        dialog.finished.connect(finished)
        dialog.open()
        dialog.raise_()
        dialog.activateWindow()

    def _next_file(self):
        if not self.running:
            return
        self.waiting = False
        self.position += 1
        self._play_file()

    def _stop(self):
        self.running = False
        self.pause_timer.stop()
        self.watchdog.stop()
        if self._pause_dialog is not None:
            dialog = self._pause_dialog
            self._pause_dialog = None
            dialog.reject()
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.process.kill()
            self.process.waitForFinished(1500)
        self._finish(cancelled=True)

    def _finish(self, cancelled=False):
        self.running = False
        self.waiting = False
        self.start.setEnabled(True)
        self.stop.setEnabled(False)
        self.folder.setEnabled(True)
        self.recursive.setEnabled(True)
        summary = (_('Video test cancelled.') if cancelled else _('Video test completed.'))
        summary += f'  {self.progress.value()}/{len(self.files)}'
        if self.failed:
            summary += f" — {_('Errors:')} {len(self.failed)}\n" + '\n'.join(self.failed[:12])
        self.info.setText(summary)

    def hideEvent(self, event):
        if self.running:
            self._stop()
        super().hideEvent(event)

    def closeEvent(self, event):
        if self.running:
            self._stop()
        super().closeEvent(event)
