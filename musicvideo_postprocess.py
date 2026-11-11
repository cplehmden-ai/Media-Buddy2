"""Test 157: local music-video and reusable artist NFO editors.

No scraper calls. Unknown XML nodes are preserved. Media originals are never deleted.
"""
from __future__ import annotations
from gui_i18n import _
from postprocess_save_guard import can_autosave, report_save_failure

from postprocess_entry import make_entry

import os
import re
import subprocess
import tempfile
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote

from PySide6.QtCore import Qt, QSettings, QUrl, QSize, QThread, Signal
from PySide6.QtGui import QDesktopServices, QPixmap, QImage
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QFileDialog, QListWidget, QLineEdit, QTextEdit,
    QStackedWidget, QMessageBox, QSplitter, QGroupBox, QScrollArea, QDialog, QSlider, QSpinBox, QFrame, QProgressBar, QSizePolicy)
from series_postprocess import ArtworkPreview, media_patterns, is_media
from linked_media_walk import linked_files

VIDEO_FIELDS = [('title',_('Title')),('artist',_('Artists (separate multiple with ;)')),
                ('album',_('Album')),('year',_('Year')),('runtime',_('Runtime (minutes)')),
                ('track',_('Track')),('genre',_('Genre')),('studio',_('Studio / Label')),
                ('premiered',_('Release')),('rating',_('Rating'))]
ARTIST_FIELDS = [('name',_('Artist')),('musicBrainzArtistID','MusicBrainz Artist ID'),
                 ('sortname',_('Sort name')),('type',_('Type')),('gender',_('Gender')),
                 ('disambiguation',_('Disambiguation')),('born',_('Born')),
                 ('died',_('Died')),('formed',_('Founded')),('disbanded',_('Disbanded')),
                 ('genre',_('Genre'))]
ARTIST_ART = [('poster.jpg', _('Poster')),('fanart.jpg', _('Fanart')),('banner.jpg', _('Banner')),
              ('clearlogo.png', _('Clearlogo')),('clearart.png', _('Clearart')),('landscape.jpg', _('Landscape'))]
VIDEO_ART = [('poster.jpg', _('Cover')),('fanart.jpg', _('Fanart'))]


def _nodes(root, tag):
    return root.findall(tag)


def _value(root, tag):
    return '; '.join((n.text or '').strip() for n in _nodes(root, tag) if (n.text or '').strip())


def _update(root, tag, value, multi=False):
    """Replace managed tag values in place; leave unrelated nodes intact."""
    nodes = _nodes(root, tag)
    values = [s.strip() for s in value.split(';') if s.strip()] if multi else [value.strip()]
    if not values:
        values = ['']
    for i, val in enumerate(values):
        node = nodes[i] if i < len(nodes) else ET.SubElement(root, tag)
        node.text = val
    for node in nodes[len(values):]:
        root.remove(node)


def _drop_ids(root, kinds):
    for node in list(root):
        if node.tag.lower() == 'uniqueid':
            kind = (node.get('type') or '').lower()
            if ('artist' in kinds and kind == 'musicbrainz_artist') or ( 'track' in kinds and kind == 'musicbrainz') or ('album' in kinds and kind == 'musicbrainz_album'):
                root.remove(node)
        elif node.tag.lower() in ('musicbrainzartistid', 'musicbrainztrackid', 'musicbrainzalbumid'):
            if node.tag.lower().replace('musicbrainz','').replace('id','') in kinds:
                root.remove(node)


def _write_tree(tree, path):
    """Write atomically; a parse or write failure never truncates the NFO."""
    tmp = path.with_name(path.name + '.mb157.tmp')
    try:
        ET.indent(tree, space='  ')
        tree.write(tmp, encoding='utf-8', xml_declaration=True)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def scan_videos(folder):
    result = []
    for video in linked_files(folder):
        if video.is_file() and is_media(video, media_patterns()) and video.with_suffix('.nfo').is_file():
            try:
                if ET.parse(video.with_suffix('.nfo')).getroot().tag == 'musicvideo':
                    result.append(video)
            except ET.ParseError:
                pass
    return sorted(result, key=lambda p: p.stem.casefold())


def scan_artists(folder):
    return sorted((p for p in linked_files(folder) if p.name.lower() == 'artist.nfo'),
                  key=lambda p: p.parent.name.casefold())


class VideoArtworkPreview(ArtworkPreview):
    """Video-specific sidecars: never look for a global poster.jpg in the folder."""
    def __init__(self, kind, title):
        super().__init__(kind + '.jpg', title)
        self.kind = kind
        self.video = None
        self.keep_aspect = True
        self.image.setStyleSheet('QPushButton { background: palette(base); color: palette(text); border: 1px solid palette(mid); }')
        # Height is assigned by the enclosing ArtworkColumn based on BOTH
        # aspect ratios and the actual available viewport height.
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(1)

    def display_video(self, video):
        self.video = video
        self.folder = video.parent if video else None
        candidates = []
        if video:
            if self.kind == 'poster':
                candidates = [video.with_name(video.stem + '-poster.jpg'),
                              video.with_name(video.stem + '-thumb.jpg')]
            else:
                candidates = [video.with_name(video.stem + '-fanart.jpg')]
        self.pixmap = QPixmap()
        for candidate in candidates:
            if candidate.is_file():
                self.pixmap = QPixmap(str(candidate))
                if not self.pixmap.isNull(): break
        self.refresh_icon()

    def choose(self):
        if not self.video: return
        src, _ = QFileDialog.getOpenFileName(self, _('Select artwork'), '', 'Bilder (*.jpg *.jpeg *.png *.webp *.bmp)')
        if not src: return
        from PySide6.QtGui import QImageReader
        im = QImageReader(src).read()
        if im.isNull():
            QMessageBox.warning(self,_('Artwork'),_('Could not read the image.')); return
        dest = self.video.with_name(self.video.stem + '-' + self.kind + '.jpg')
        if not im.save(str(dest),'JPG'):
            QMessageBox.warning(self,_('Artwork'),_('Save failed.'));return
        self.display_video(self.video)


class ArtworkColumn(QWidget):
    """Fit independent 1:1 and 16:9 previews without scrollbars or clipping.

    The column never dictates splitter width. On every resize, compute the
    maximum common preview width that fits both the available width AND height.
    """
    def __init__(self, previews, parent=None):
        super().__init__(parent)
        self.previews = previews
        self.setMinimumWidth(175)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(4)
        for preview in previews:
            layout.addWidget(preview, 0, Qt.AlignmentFlag.AlignTop)
        layout.addStretch(1)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        layout = self.layout()
        margins = layout.contentsMargins()
        usable_w = max(1, self.width() - margins.left() - margins.right())
        usable_h = max(1, self.height() - margins.top() - margins.bottom() - layout.spacing() * 2)
        # Cover 1:1 plus fanart 16:9, with borders accounted for.
        common_w = max(1, min(usable_w, int((usable_h - 4) / (1 + 9 / 16))))
        for preview, ratio in zip(self.previews, (1.0, 9 / 16)):
            preview.setFixedWidth(common_w)
            preview.setFixedHeight(max(1, round(common_w * ratio)))


class _FrameWorker(QThread):
    finished_frames = Signal(str)
    def __init__(self, video, folder, parent=None):
        super().__init__(parent)
        self.video = video
        self.folder = folder
        self.proc = None
        self.cancelled = False
        self.timestamps = []

    def run(self):
        from tool_manager import get_tool
        try:
            ffmpeg = get_tool('ffmpeg')
        except Exception:
            ffmpeg = 'ffmpeg'
        # Spread the 16 choices across the actual duration, rather than
        # sampling every 30 seconds (which yields only 6-8 for music videos).
        # Keep timestamps in the worker: the full-resolution extraction must
        # seek to the EXACT same timestamp as its preview.
        duration = None
        try:
            ffprobe = get_tool('ffprobe')
            probe = subprocess.run(
                [str(ffprobe), '-v', 'error', '-show_entries',
                 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1',
                 str(self.video)], capture_output=True, text=True,
                timeout=25, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if probe.returncode == 0:
                duration = float(probe.stdout.strip())
                if not (0 < duration < float('inf')):
                    duration = None
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        if duration is None:
            # Fallback for missing ffprobe: query duration from the media
            # container with the same FFmpeg installation.
            try:
                probe = subprocess.run([str(ffmpeg), '-hide_banner', '-i', str(self.video)],
                    capture_output=True, text=True, timeout=25,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                match = __import__('re').search(r'Duration: (\d+):(\d+):(\d+(?:\.\d+)?)', probe.stderr)
                if match:
                    hours, minutes, seconds = match.groups()
                    duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
        if duration is None:
            message = _('Could not determine video duration.')
            self.finished_frames.emit(message)
            return
        # Avoid the very first/last seconds, where fades and credits are common.
        # For short clips keep at least 0.1 s inside the actual video.
        margin = min(5.0, duration * 0.05)
        usable = max(0.0, duration - 2 * margin)
        self.timestamps = [round(margin + usable * (i + 0.5) / 16, 3)
                           for i in range(16)]
        message = ''
        for index, timestamp in enumerate(self.timestamps):
            if self.cancelled:
                break
            target = str(Path(self.folder) / f'frame_{index:02d}.jpg')
            cmd = [str(ffmpeg), '-hide_banner', '-loglevel', 'error', '-y',
                   '-ss', str(timestamp), '-i', str(self.video),
                   '-map', '0:v:0', '-frames:v', '1', '-an', '-sn',
                   '-vf', 'scale=400:-2', '-update', '1', target]
            try:
                self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                _, err = self.proc.communicate()
                if self.proc.returncode != 0:
                    message = (err or b'').decode('utf-8', errors='replace')[-500:]
                    break
                if not Path(target).exists():
                    break  # Reached end of video.
            except OSError as exc:
                message = str(exc)
                break
        self.finished_frames.emit(message)

    def stop(self):
        self.cancelled = True
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()


class _FullResolutionFrameWorker(QThread):
    """Extract exactly one source-resolution frame, without preview downscaling."""
    frame_ready = Signal(str)

    def __init__(self, video, seconds, destination, parent=None):
        super().__init__(parent)
        self.video, self.seconds, self.destination = video, seconds, destination
        self.proc = None

    def run(self):
        from tool_manager import get_tool
        try:
            ffmpeg = get_tool('ffmpeg')
        except Exception:
            ffmpeg = 'ffmpeg'
        cmd = [str(ffmpeg), '-hide_banner', '-loglevel', 'error', '-y',
               '-ss', str(self.seconds), '-i', str(self.video),
               '-map', '0:v:0', '-frames:v', '1', '-an', '-sn',
               '-update', '1', str(self.destination)]
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            _, err = self.proc.communicate()
            if self.proc.returncode != 0:
                self.frame_ready.emit((err or b'').decode('utf-8', errors='replace')[-500:])
            else:
                self.frame_ready.emit('')
        except OSError as exc:
            self.frame_ready.emit(str(exc))

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()


class CoverPicker(QDialog):
    """Explicit 16-frame picker with an adjustable square crop; no source modification."""
    def __init__(self, video, parent=None):
        super().__init__(parent)
        self.video=video; self.selected=None; self.frames=[]; self.frame_times=[]; self.saving=False
        self.setWindowTitle(_('Select square music video cover'))
        self.resize(950,700)
        self.tmp=tempfile.TemporaryDirectory(prefix='mb157_cover_')
        layout=QVBoxLayout(self)
        layout.addWidget(QLabel(_('Select a frame, then move the square crop.')))
        self.grid=QGridLayout(); layout.addLayout(self.grid)
        self.preview=QLabel(_('Select frame'));self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(160);layout.addWidget(self.preview,1)
        controls=QHBoxLayout();controls.addWidget(QLabel(_('Horizontal')))
        self.x=QSlider(Qt.Orientation.Horizontal);self.x.setRange(0,100);self.x.setValue(50);controls.addWidget(self.x)
        controls.addWidget(QLabel(_('Vertical')))
        self.y=QSlider(Qt.Orientation.Horizontal);self.y.setRange(0,100);self.y.setValue(50);controls.addWidget(self.y)
        layout.addLayout(controls)
        zoom_controls = QHBoxLayout()
        zoom_controls.addWidget(QLabel(_('Zoom')))
        self.zoom = QSlider(Qt.Orientation.Horizontal)
        self.zoom.setRange(100, 300)
        self.zoom.setSingleStep(5)
        self.zoom.setPageStep(10)
        self.zoom.setValue(100)
        zoom_controls.addWidget(self.zoom)
        self.zoom_label = QLabel('100 %')
        self.zoom_label.setMinimumWidth(55)
        zoom_controls.addWidget(self.zoom_label)
        layout.addLayout(zoom_controls)
        actions=QHBoxLayout();ok=QPushButton(_('Save square cover'));ok.clicked.connect(self.save)
        cancel=QPushButton(_('Cancel'));cancel.clicked.connect(self.reject)
        actions.addWidget(ok);actions.addWidget(cancel);layout.addLayout(actions)
        self.x.valueChanged.connect(self.refresh);self.y.valueChanged.connect(self.refresh)
        self.zoom.valueChanged.connect(self.refresh)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        layout.insertWidget(1, self.progress)
        self.status = QLabel(_('Frames are being generated in the background …'))
        layout.insertWidget(2, self.status)
        ok.setEnabled(False)
        self.save_button = ok
        self.generate()

    def generate(self):
        self.worker = _FrameWorker(self.video, self.tmp.name, self)
        self.worker.finished_frames.connect(self._frames_ready)
        self.worker.start()

    def _frames_ready(self, error):
        self.progress.hide()
        for path in sorted(Path(self.tmp.name).glob('frame_*.jpg')):
            im = QImage(str(path))
            if im.isNull():
                continue
            self.frames.append(im)
            self.frame_times.append(self.worker.timestamps[int(path.stem.split("_")[-1])])
            i = len(self.frames) - 1
            button = QPushButton()
            button.setIcon(QPixmap.fromImage(im))
            button.setIconSize(QSize(145, 82))
            button.setFixedSize(158, 92)
            button.clicked.connect(lambda checked=False, n=i: self.select(n))
            self.grid.addWidget(button, i // 4, i % 4)
        if self.frames:
            self.status.setText(_('{count} frames ready.').format(count=len(self.frames)))
            self.save_button.setEnabled(True)
            self.select(0)
        else:
            self.status.setText(_('No frames generated. ') + error)

    def reject(self):
        if hasattr(self, 'full_worker') and self.full_worker.isRunning():
            self.full_worker.stop()
            self.full_worker.wait(3000)
        if hasattr(self, 'worker') and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(3000)
        super().reject()

    def select(self,index):
        self.selected=index;self.refresh()

    def crop(self):
        if self.selected is None:return None
        im=self.frames[self.selected]
        # 100 %: largest possible square; higher zoom: smaller source crop.
        # The same crop is used for both preview and saved JPEG.
        side=max(1, round(min(im.width(),im.height()) * 100 / self.zoom.value()))
        x=round((im.width()-side)*self.x.value()/100)
        y=round((im.height()-side)*self.y.value()/100)
        x=max(0,min(x,im.width()-side))
        y=max(0,min(y,im.height()-side))
        return im.copy(x,y,side,side)

    def refresh(self,*args):
        self.zoom_label.setText(f'{self.zoom.value()} %')
        im=self.crop()
        if im is not None:
            self.preview.setPixmap(QPixmap.fromImage(im).scaled(190,190,Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation))

    def save(self):
        if self.selected is None or self.saving:
            return
        dest = self.video.with_name(self.video.stem + '-poster.jpg')
        # Previews are scaled to 400px, with timestamps distributed over the video.
        # Decode the chosen source frame again at native resolution, off the GUI thread.
        self.saving = True
        self.save_button.setEnabled(False)
        self.status.setText(_('Loading full-resolution source frame …'))
        self.progress.show()
        self.progress.setRange(0, 0)
        self.full_frame_path = Path(self.tmp.name) / 'selected_full.png'
        self.full_worker = _FullResolutionFrameWorker(
            self.video, self.frame_times[self.selected], self.full_frame_path, self)
        self.full_worker.frame_ready.connect(self._save_full_resolution)
        self.full_worker.start()

    def _save_full_resolution(self, error):
        self.progress.hide()
        self.saving = False
        self.save_button.setEnabled(True)
        if error:
            self.status.setText(_('Could not read source frame.'))
            QMessageBox.warning(self, _('Cover'), error)
            return
        original = QImage(str(self.full_frame_path))
        if original.isNull():
            QMessageBox.warning(self, _('Cover'), _('Could not load the original still image.'))
            return
        # Use the same relative crop geometry as the preview, now on native pixels.
        side = max(1, round(min(original.width(), original.height()) * 100 / self.zoom.value()))
        x = round((original.width() - side) * self.x.value() / 100)
        y = round((original.height() - side) * self.y.value() / 100)
        x = max(0, min(x, original.width() - side))
        y = max(0, min(y, original.height() - side))
        result = original.copy(x, y, side, side).scaled(
            1000, 1000, Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        dest = self.video.with_name(self.video.stem + '-poster.jpg')
        # Write a temporary JPEG first, so failed saves never damage the existing cover.
        # Stage the JPEG in the destination directory. os.replace() is atomic
        # only on the same filesystem (Windows raises WinError 17 across drives).
        # Keep the existing poster intact if writing or replacing fails.
        import uuid
        temp = dest.with_name('.' + dest.name + '.' + uuid.uuid4().hex + '.tmp.jpg')
        try:
            if not result.save(str(temp), 'JPG', 95):
                QMessageBox.warning(self, _('Cover'), _('Could not save the cover.'))
                return
            os.replace(str(temp), str(dest))
        except OSError as exc:
            QMessageBox.warning(self, _('Cover'), str(exc))
            return
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
        self.accept()

    def closeEvent(self,event):
        if hasattr(self, 'full_worker') and self.full_worker.isRunning():
            self.full_worker.stop()
            self.full_worker.wait(3000)
        if hasattr(self, 'worker') and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(3000)
        self.tmp.cleanup()
        super().closeEvent(event)


class NfoEditor(QWidget):
    """Reusable editor for a single artist.nfo; independent of caller."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.path = None
        self.tree = None
        layout = QVBoxLayout(self)
        form = QGridLayout()
        self.fields = {}
        for row, (key, label) in enumerate(ARTIST_FIELDS):
            edit = QLineEdit(); edit.setMinimumWidth(0)
            self.fields[key] = edit
            form.addWidget(QLabel(label), row, 0)
            form.addWidget(edit, row, 1)
        scroll_body = QWidget(); scroll_body.setLayout(form)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(scroll_body)
        layout.addWidget(scroll, 2)
        layout.addWidget(QLabel(_('Biography')))
        self.biography = QTextEdit(); layout.addWidget(self.biography, 2)
        actions = QHBoxLayout()
        for title, site in [('Wikipedia','wikipedia'),('Discogs','discogs')]:
            b = QPushButton(title); b.clicked.connect(lambda checked=False, s=site: self.open_site(s))
            actions.addWidget(b)
        save = QPushButton(_('Save artist NFO')); save.clicked.connect(self.save)
        actions.addWidget(save); layout.addLayout(actions)

    def load(self, path):
        self.path = Path(path) if path else None
        self.tree = None
        if self.path:
            try:
                tree = ET.parse(self.path)
                if tree.getroot().tag != 'artist': raise ValueError(_('No artist.nfo'))
                self.tree = tree
            except (OSError, ET.ParseError, ValueError) as exc:
                QMessageBox.warning(self, _('Artist'), str(exc))
        root = self.tree.getroot() if self.tree is not None else None
        for key, field in self.fields.items(): field.setText(_value(root,key) if root is not None else '')
        self.biography.setPlainText(_value(root,'biography') if root is not None else '')

    def save(self):
        if self.tree is None or self.path is None: return False
        root = self.tree.getroot()
        for key, field in self.fields.items():
            _update(root, key, field.text(), multi=key=='genre')
        _update(root, 'biography', self.biography.toPlainText())
        try: _write_tree(self.tree, self.path)
        except OSError as exc:
            QMessageBox.warning(self,_('Artist'),_('Could not save: {exc}').format(exc=exc)); return False
        return True

    def open_site(self, site):
        name = self.fields['name'].text().strip()
        if not name: return
        encoded = quote(name.replace(' ', '_') if site=='wikipedia' else name, safe='')
        url = ('https://en.wikipedia.org/wiki/' if site=='wikipedia' else 'https://www.discogs.com/search/?type=artist&q=') + encoded
        QDesktopServices.openUrl(QUrl(url))


class MusicvideoPostprocess(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.videos=[]; self.current=None; self.tree=None; self.initial={}; self.loading=False
        self.artist_files=[]
        main = QVBoxLayout(self); main.setContentsMargins(5,5,5,5)
        self.pages=QStackedWidget(); main.addWidget(self.pages)
        self.folder_edit=QLineEdit(QSettings('BuddyTools','Media-Buddy-V2').value('postprocess/musicvideos_folder',''))
        start=make_entry(self.folder_edit,self.browse,self.open_folder,_('Post-processing: Music videos'),_('Music video folder'))
        self.pages.addWidget(start)
        edit=QWidget(); el=QVBoxLayout(edit);el.setContentsMargins(0,0,0,0)
        self.views=QStackedWidget();el.addWidget(self.views)
        video_page=QWidget(); vl=QVBoxLayout(video_page)
        split=QSplitter(Qt.Orientation.Horizontal);vl.addWidget(split,1)
        self.video_list=QListWidget();self.video_list.setMinimumWidth(270);self.video_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff);split.addWidget(self.video_list)
        center=QWidget();cl=QVBoxLayout(center);cl.setContentsMargins(2,2,2,2)
        fields=QGridLayout();fields.setHorizontalSpacing(6);fields.setVerticalSpacing(4);fields.setColumnStretch(1,1);self.inputs={}
        for i,(key,label) in enumerate(VIDEO_FIELDS):
            inp=QLineEdit();inp.setMinimumWidth(0)
            # The application's global 5px padding clips descenders (g, p, y)
            # at the default line-edit height on Windows.
            inp.setMinimumHeight(26)
            inp.setStyleSheet('QLineEdit { padding: 1px 5px; }')
            self.inputs[key]=inp
            fields.addWidget(QLabel(label),i,0);fields.addWidget(inp,i,1)
        cl.addLayout(fields);cl.addWidget(QLabel(_('Description')))
        self.plot=QTextEdit();self.plot.setStyleSheet('QTextEdit { padding: 4px; }');cl.addWidget(self.plot,1)
        split.addWidget(center)
        self.video_art=[VideoArtworkPreview('poster','Cover'),
                        VideoArtworkPreview('fanart',_('Fanart'))]
        art_col=ArtworkColumn(self.video_art)
        # Editing actions belong below the artwork; navigation belongs below them.
        art_panel=QWidget()
        art_layout=QVBoxLayout(art_panel)
        art_layout.setContentsMargins(1, 1, 1, 1)
        art_layout.setSpacing(4)
        art_layout.addWidget(art_col, 1)
        buttons=QGridLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setHorizontalSpacing(4)
        buttons.setVerticalSpacing(4)
        for label, fn, row, column in [
            (_('Select cover'), self.pick_cover, 0, 0),
            (_('Edit artist'), None, 0, 1),
            (_('Play'), self.play, 1, 0),
            (_('Back'), self.leave, 1, 1),
        ]:
            button=QPushButton(label)
            button.setMinimumWidth(0)
            if fn is None:
                button.setEnabled(False)
                button.setToolTip(_('Shared artist post-processing will follow with the music module'))
            else:
                button.clicked.connect(fn)
            buttons.addWidget(button, row, column)
        buttons.setColumnStretch(0, 1)
        buttons.setColumnStretch(1, 1)
        art_layout.addLayout(buttons)
        split.addWidget(art_panel)
        # Keep the metadata form usable even at the default window width.
        center.setMinimumWidth(400)
        split.setChildrenCollapsible(False)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 6)
        split.setStretchFactor(2, 3)
        split.setSizes([325, 560, 300])
        self.views.addWidget(video_page)
        artist_page=QWidget(); ap=QVBoxLayout(artist_page)
        aheader=QHBoxLayout();back=QPushButton(_('Back to music videos'));back.clicked.connect(self.back_from_artist);aheader.addWidget(back)
        aheader.addWidget(QLabel(_('Artist post-processing')),1);ap.addLayout(aheader)
        asplit=QSplitter(Qt.Orientation.Horizontal);ap.addWidget(asplit,1)
        self.artist_list=QListWidget();asplit.addWidget(self.artist_list)
        self.artist_editor=NfoEditor();asplit.addWidget(self.artist_editor)
        artbox=QWidget();artlayout=QVBoxLayout(artbox)
        self.artist_art=[]
        for filename,title in ARTIST_ART:
            preview=ArtworkPreview(filename,title);preview.keep_aspect=True
            self.artist_art.append(preview);artlayout.addWidget(preview,1)
        artist_scroll=QScrollArea();artist_scroll.setWidgetResizable(True);artist_scroll.setWidget(artbox)
        asplit.addWidget(artist_scroll);asplit.setSizes([180,530,290])
        self.views.addWidget(artist_page);self.pages.addWidget(edit)
        self.video_list.currentRowChanged.connect(self.switch_video)
        self.artist_list.currentRowChanged.connect(self.switch_artist)

    def browse(self):
        folder=QFileDialog.getExistingDirectory(self,_('Select music video folder'),self.folder_edit.text())
        if folder:self.folder_edit.setText(folder)

    def open_folder(self, folder_override=None):
        if isinstance(folder_override,bool):folder_override=None
        folder=Path(folder_override if folder_override is not None else self.folder_edit.text().strip())
        if not folder.is_dir():
            QMessageBox.warning(self,_('Music videos'),_('The selected folder does not exist.'));return
        self.folder_edit.setText(str(folder))
        QSettings('BuddyTools','Media-Buddy-V2').setValue('postprocess/musicvideos_folder',str(folder))
        self.videos=scan_videos(folder);self.current=None;self.tree=None
        self.loading=True;self.video_list.clear();self.video_list.addItems([p.stem for p in self.videos]);self.loading=False
        self.pages.setCurrentIndex(1);self.views.setCurrentIndex(0)
        self.video_list.setCurrentRow(0 if self.videos else -1)
        if not self.videos:QMessageBox.information(self,_('Music videos'),_('No music videos with a valid NFO found.'))

    def switch_video(self, index):
        if self.loading:return
        if self.current is not None and not self.save_current():
            self.loading=True;self.video_list.setCurrentRow(self.videos.index(self.current));self.loading=False;return
        self.current=self.videos[index] if 0<=index<len(self.videos) else None
        self.tree=None
        if self.current:
            try:self.tree=ET.parse(self.current.with_suffix('.nfo'))
            except (ET.ParseError,OSError) as exc:QMessageBox.warning(self,_('Music videos'),str(exc))
        root=self.tree.getroot() if self.tree is not None else None
        self.initial={key:_value(root,key) if root is not None else '' for key in self.inputs}
        for key,field in self.inputs.items():field.setText(self.initial[key])
        self.plot.setPlainText(_value(root,'plot') if root is not None else '')
        for art in self.video_art:art.display_video(self.current)

    def save_current(self):
        if self.current is None or self.tree is None:return True
        root=self.tree.getroot()
        changed={key for key in ('artist','title','album') if self.inputs[key].text().strip()!=self.initial.get(key,'').strip()}
        # A track ID cannot be trusted after artist/title changes. Album IDs are independent.
        ids=set()
        if 'artist' in changed:ids.update(('artist','track'))
        if 'title' in changed:ids.add('track')
        if 'album' in changed:ids.add('album')
        _drop_ids(root,ids)
        for key,field in self.inputs.items():_update(root,key,field.text(),multi=key in ('artist','genre','studio'))
        _update(root,'plot',self.plot.toPlainText())
        # Never rename source media until the NFO has been saved successfully.
        nfo=self.current.with_suffix('.nfo')
        if not can_autosave(self, nfo):return True
        try:_write_tree(self.tree,nfo)
        except OSError as exc:report_save_failure(self,nfo,exc,_('Music videos'));return True
        self.initial={key:self.inputs[key].text().strip() for key in self.inputs}
        return True

    def pick_cover(self):
        if not self.current:return
        dialog=CoverPicker(self.current,self)
        if dialog.exec()==QDialog.DialogCode.Accepted:
            for art in self.video_art:art.display_video(self.current)

    def play(self):
        if not self.current:
            return
        try:
            from tool_manager import get_tool
            subprocess.Popen([get_tool('mpv'), str(self.current)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, cwd=str(self.current.parent))
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, _('Play'), str(exc))

    def show_artists(self):
        if not self.save_current():return
        # Use a separately configured artist root if present; never rely on obsolete <path> NFO tags.
        folder=QFileDialog.getExistingDirectory(self,_('Select artist folder'),self.folder_edit.text())
        if not folder:return
        self.artist_files=scan_artists(Path(folder))
        self.artist_list.clear();self.artist_list.addItems([p.parent.name for p in self.artist_files])
        self.views.setCurrentIndex(1)
        self.artist_list.setCurrentRow(0 if self.artist_files else -1)

    def switch_artist(self,index):
        if not self.artist_editor.save(): return
        path=self.artist_files[index] if 0<=index<len(self.artist_files) else None
        self.artist_editor.load(path)
        for art in self.artist_art:art.display(path.parent if path else None)

    def back_from_artist(self):
        if self.artist_editor.save():self.views.setCurrentIndex(0)

    def leave(self):
        if self.save_current():self.pages.setCurrentIndex(0)
