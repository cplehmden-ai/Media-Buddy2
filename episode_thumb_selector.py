"""Select one of sixteen evenly spaced episode frames without modifying media."""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from gui_i18n import _
from PySide6.QtCore import Qt, QThread, Signal, QSize
from PySide6.QtGui import QPixmap, QImageReader
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QGridLayout, QHBoxLayout,
                               QPushButton, QLabel, QScrollArea, QWidget, QMessageBox)


class FrameWorker(QThread):
    frame_ready = Signal(int, str)
    failed = Signal(str)
    completed = Signal()

    def __init__(self, video: Path, output: Path, parent=None):
        super().__init__(parent)
        self.video, self.output = video, output

    def run(self):
        try:
            from tool_manager import get_tool
            ffprobe, ffmpeg = get_tool('ffprobe'), get_tool('ffmpeg')
            probe = subprocess.run([ffprobe, '-v', 'error', '-show_entries',
                                    'format=duration', '-of', 'json', str(self.video)],
                                   capture_output=True, text=True, errors='replace',
                                   timeout=40, check=True)
            duration = float(json.loads(probe.stdout)['format']['duration'])
            if duration <= 0:
                raise ValueError('Keine gültige Videolaufzeit ermittelt.')
            success = 0
            for index in range(16):
                if self.isInterruptionRequested():
                    return
                # Avoid black intro/outro frames by sampling within the central 90%.
                second = duration * (0.05 + (index + 0.5) * 0.90 / 16)
                path = self.output / f'frame_{index:02d}.jpg'
                command = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin',
                           '-ss', f'{second:.3f}', '-i', str(self.video),
                           '-map', '0:v:0', '-frames:v', '1', '-vf',
                           'scale=480:-2', '-q:v', '3', '-y', str(path)]
                try:
                    proc = subprocess.run(command, capture_output=True, timeout=45,
                                          check=False)
                    if proc.returncode == 0 and path.is_file() and path.stat().st_size:
                        success += 1
                        self.frame_ready.emit(index, str(path))
                except (OSError, subprocess.TimeoutExpired):
                    continue
            if not success:
                raise ValueError('FFmpeg konnte keine Standbilder aus dem Video erzeugen.')
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            self.failed.emit(str(exc))
        finally:
            self.completed.emit()


class EpisodeThumbSelector(QDialog):
    def __init__(self, video: Path, parent=None):
        super().__init__(parent)
        self.video = Path(video)
        self.setWindowTitle(_('Select episode thumbnail'))
        self.resize(1050, 680)
        self.setMinimumSize(700, 470)
        self._temp = tempfile.TemporaryDirectory(prefix='mb_episode_frames_')
        self._files = {}
        self._selected = None
        self._worker = None
        outer = QVBoxLayout(self)
        self.info = QLabel(_('Generating 16 frames with FFmpeg …'))
        outer.addWidget(self.info)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        self.grid = QGridLayout(content)
        self.grid.setSpacing(7)
        self.buttons = []
        for index in range(16):
            button = QPushButton(f'{index + 1}\n{_('Loading …')}')
            button.setMinimumSize(150, 95)
            button.setEnabled(False)
            button.setStyleSheet('QPushButton { background: #e8ebef; border: 2px solid #aab3bd; color: #202020; }')
            button.clicked.connect(lambda _checked=False, i=index: self.choose(i))
            self.grid.addWidget(button, index // 4, index % 4)
            self.buttons.append(button)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)
        actions = QHBoxLayout()
        actions.addStretch(1)
        self.accept_button = QPushButton(_('Use selected image'))
        self.accept_button.setEnabled(False)
        self.accept_button.clicked.connect(self.accept)
        actions.addWidget(self.accept_button)
        cancel = QPushButton(_('Cancel'))
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        outer.addLayout(actions)
        self._worker = FrameWorker(self.video, Path(self._temp.name), self)
        self._worker.frame_ready.connect(self.frame_ready)
        self._worker.failed.connect(self.worker_failed)
        self._worker.completed.connect(self.worker_completed)
        self._worker.start()

    def frame_ready(self, index, path):
        self._files[index] = Path(path)
        pixmap = QPixmap(path)
        button = self.buttons[index]
        if not pixmap.isNull():
            from PySide6.QtGui import QIcon
            button.setIcon(QIcon(pixmap))
            button.setIconSize(button.size() - QSize(12, 12))
            button.setText('')
            button.setEnabled(True)
        self.info.setText(_('{count}/16 frames generated – select one.').format(count=len(self._files)))

    def choose(self, index):
        if index not in self._files:
            return
        self._selected = index
        for i, button in enumerate(self.buttons):
            button.setStyleSheet('QPushButton { background: #e8ebef; border: 3px solid '
                                 + ('#1675c5' if i == index else '#aab3bd') + '; }')
        self.accept_button.setEnabled(True)

    def worker_failed(self, message):
        self.info.setText(_('Could not generate all frames: ') + message)
        if not self._files:
            QMessageBox.warning(self, _('Refresh thumbnail'), message)

    def worker_completed(self):
        if self._files:
            self.info.setText(_('{count} frames available – select one to use.').format(count=len(self._files)))

    def selected_image(self):
        return self._files.get(self._selected)

    def closeEvent(self, event):
        if self._worker and self._worker.isRunning():
            self._worker.requestInterruption()
            self._worker.wait(5000)
            if self._worker.isRunning():
                event.ignore()
                return
        super().closeEvent(event)

    def done(self, result):
        if self._worker and self._worker.isRunning():
            self._worker.requestInterruption()
            self._worker.wait(5000)
            if self._worker.isRunning():
                return
        super().done(result)

    def cleanup(self):
        if self._worker and self._worker.isRunning():
            self._worker.requestInterruption()
            self._worker.wait()
        self._temp.cleanup()
