"""Test 145: manual series-level NFO and artwork review.

Only the series level is implemented here. Episode editing and online artwork
selection are deliberately reserved for later tests.
"""
from __future__ import annotations
from gui_i18n import _
from postprocess_save_guard import can_autosave, report_save_failure

from postprocess_entry import make_entry
from linked_media_walk import linked_files

import os
import subprocess
import fnmatch
import re
import shutil
from pathlib import Path
import xml.etree.ElementTree as ET

from PySide6.QtCore import Qt, QSettings, QSize, QThread, Signal, QTimer, QSignalBlocker
from date_normalizer import normalize_date, InvalidDate
from postprocess_renamer import rename_episode_files
from PySide6.QtGui import QPixmap, QIcon
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QFileDialog, QListWidget, QLineEdit, QTextEdit, QScrollArea, QMessageBox,
    QSplitter, QFrame, QStackedWidget, QSizePolicy, QGroupBox, QAbstractItemView,
)

def media_patterns():
    """Use the same two lists as Einstellungen > Video, including stubs."""
    try:
        from configuration_store import effective
        data = effective(Path(__file__).resolve().parent / 'Media-Buddy.json')
    except (OSError, ValueError, ImportError):
        data = {}
    defaults = data.get('FileExtensions') or ['*.mkv', '*.mk3d', '*.mp4', '*.avi', '*.stub', '*.disc']
    settings = data.get('VideoSettings', {})
    videos = settings.get('VideoExtensions')
    stubs = settings.get('StubExtensions')
    return tuple(str(x).strip().lower() for x in ((videos or []) + (stubs or []) if videos is not None or stubs is not None else defaults) if str(x).strip())


def is_media(path, patterns):
    return any(fnmatch.fnmatch(path.name.lower(), pat) for pat in patterns)

FIELDS = (
    ('title', _('Title')), ('sorttitle', _('Sort title')),
    ('originaltitle', _('Original title')), ('premiered', _('Release date')),
    ('rating', _('Rating')), ('mpaa', _('Content rating')), ('genre', _('Genre')),
    ('studio', _('Studio')), ('status', _('Status')),
)
ARTWORK = (
    ('banner.jpg', _('Banner')), ('fanart.jpg', _('Fanart')),
    ('poster.jpg', _('Poster')), ('clearart.png', _('Clearart')),
    ('clearlogo.png', _('Clearlogo')),
)


def series_with_videos(folder: Path):
    """Find series directories, not empty series roots or loose media files."""
    if not folder.is_dir():
        return []
    result = []
    patterns = media_patterns()
    for child in sorted(folder.iterdir(), key=lambda p: p.name.casefold()):
        if child.is_dir() and any(is_media(p, patterns) for p in linked_files(child)):
            result.append(child)
    return result


def read_series(path: Path):
    nfo = path / 'tvshow.nfo'
    if not nfo.is_file():
        raise FileNotFoundError(_('No tvshow.nfo found: {nfo}').format(nfo=nfo))
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    tree = ET.parse(nfo, parser=parser)
    if tree.getroot().tag != 'tvshow':
        raise ValueError(_('Unexpected NFO root element: {tag}').format(tag=tree.getroot().tag))
    return tree


def get_values(tree):
    root = tree.getroot()
    values = {key: (root.findtext(key) or '') for key, _ in FIELDS}
    values['plot'] = root.findtext('plot') or ''
    values['tvdb'] = values['tmdb'] = values['imdb'] = ''
    for elem in root.findall('uniqueid'):
        kind = elem.get('type', '').lower()
        if kind in ('tvdb', 'tmdb', 'imdb'):
            values[kind] = elem.text or ''
    custom = next((e for e in root.findall('uniqueid') if e.get('type', '').lower() not in ('tvdb', 'tmdb', 'imdb')), None)
    values['custom_type'] = custom.get('type', '') if custom is not None else ''
    values['custom_id'] = (custom.text or '') if custom is not None else ''
    return values


def write_series(path: Path, tree, values):
    """Update editable nodes, retaining unrelated NFO nodes and attributes."""
    root = tree.getroot()
    values = dict(values)
    # Normalize only the explicitly declared date field, never titles or paths.
    # Keep the original date unchanged when the user did not edit it.
    old_date = root.findtext('premiered') or ''
    date_warning = None
    if values.get('premiered', '') != old_date:
        try:
            values['premiered'] = normalize_date(values.get('premiered'))
        except InvalidDate as exc:
            # An invalid date must never block navigation or closing. Keep
            # the original NFO date, but save all other valid field changes.
            values['premiered'] = old_date
            date_warning = str(exc)
    for key, _ in FIELDS:
        node = root.find(key)
        if node is None and values[key]:
            node = ET.SubElement(root, key)
        if node is not None:
            node.text = values[key]
    plot = root.find('plot')
    if plot is None and values['plot']:
        plot = ET.SubElement(root, 'plot')
    if plot is not None:
        plot.text = values['plot']
    for kind in ('tvdb', 'tmdb', 'imdb'):
        node = next((e for e in root.findall('uniqueid') if e.get('type', '').lower() == kind), None)
        if node is None and values[kind]:
            node = ET.SubElement(root, 'uniqueid', {'type': kind})
        if node is not None:
            node.text = values[kind]
    kind, value = values['custom_type'].strip(), values['custom_id'].strip()
    if kind and value:
        node = next((e for e in root.findall('uniqueid') if e.get('type') == kind), None)
        if node is None:
            node = ET.SubElement(root, 'uniqueid', {'type': kind})
        node.text = value
    dest = path / 'tvshow.nfo'
    temporary = dest.with_suffix('.nfo.tmp')
    try:
        tree.write(temporary, encoding='utf-8', xml_declaration=True)
        os.replace(temporary, dest)
    finally:
        temporary.unlink(missing_ok=True)
    return date_warning


def ensure_active_season_artwork(folder: Path, episode_files, patterns=None):
    """Create missing season artwork only for seasons with real media episodes.

    Prefer episode NFO season numbers, falling back to SxxExx filenames.
    Existing artwork is never replaced; series artwork is never modified.
    """
    import logging
    logger = logging.getLogger(__name__)
    active_seasons = set()
    for media in episode_files:
        season = None
        nfo = media.with_suffix('.nfo')
        if nfo.is_file():
            try:
                root = ET.parse(nfo).getroot()
                if root.tag == 'episodedetails':
                    value = (root.findtext('season') or '').strip()
                    if value.isdigit():
                        season = int(value)
            except (OSError, ET.ParseError, ValueError) as exc:
                logger.debug('Staffelnummer aus %s nicht lesbar: %s', nfo, exc)
        if season is None:
            match = re.search(r'(?i)(?<![a-z0-9])s(\d{1,4})e\d{1,4}', media.stem)
            if match:
                season = int(match.group(1))
        if season is not None and season >= 0:
            active_seasons.add(season)

    for season in sorted(active_seasons):
        for kind in ('poster', 'banner'):
            source = folder / f'{kind}.jpg'
            target = folder / (f'season-specials-{kind}.jpg' if season == 0 else f'season{season:02d}-{kind}.jpg')
            if not source.is_file() or target.exists():
                continue
            try:
                # Exclusive create: never overwrite artwork created by the scraper
                # or another simultaneously running instance.
                with source.open('rb') as inp, target.open('xb') as out:
                    shutil.copyfileobj(inp, out)
                logger.info('[ARTWORK FALLBACK] %s <- %s', target.name, source.name)
            except FileExistsError:
                pass
            except OSError as exc:
                logger.warning('Staffelgrafik-Fallback %s fehlgeschlagen: %s', target, exc)
                # Avoid leaving a partially written image after a failed copy.
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    pass


class ArtworkPreview(QFrame):
    def __init__(self, filename, title, parent=None):
        super().__init__(parent)
        self.filename = filename
        self.folder = None
        self.pixmap = QPixmap()
        self.setFrameShape(QFrame.Shape.NoFrame)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(2)
        if title in ('Clearart', 'Clearlogo'):
            label = QLabel(title)
            label.setStyleSheet('color:#252525;')
            layout.addWidget(label)
        self.image = QPushButton(_('Artwork missing'))
        self.image.setStyleSheet('QPushButton { background: #161616; color: #ddd; border: 1px solid #aaa; }')
        self.image.clicked.connect(self.choose)
        self.image.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(self.image, 1)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.refresh_icon()

    def refresh_icon(self):
        if self.pixmap.isNull():
            self.image.setIcon(QIcon())
            self.image.setText(_('Artwork missing – click to select'))
            return
        self.image.setText('')
        # V1-like preview: fill the available area rather than tiny centered thumbnails.
        size = self.image.size() - QSize(4, 4)
        if size.width() > 4 and size.height() > 4:
            scaled = self.pixmap.scaled(size, (Qt.AspectRatioMode.KeepAspectRatio
                                           if getattr(self, "keep_aspect", False)
                                           else Qt.AspectRatioMode.IgnoreAspectRatio),
                                        Qt.TransformationMode.SmoothTransformation)
            self.image.setIcon(QIcon(scaled))
            self.image.setIconSize(scaled.size())

    def display(self, folder):
        self.folder = folder
        file = folder / self.filename if folder else None
        self.pixmap = QPixmap(str(file)) if file and file.is_file() else QPixmap()
        self.refresh_icon()

    def choose(self):
        if not self.folder:
            return
        src, _ = QFileDialog.getOpenFileName(self, _('Select artwork'), '', 'Bilder (*.jpg *.jpeg *.png *.webp *.avif *.bmp)')
        if not src:
            return
        dest = self.folder / self.filename
        from PySide6.QtGui import QImageReader
        image = QImageReader(src).read()
        if image.isNull():
            QMessageBox.warning(self, _('Artwork'), _('Could not read this image format.'))
            return
        if not image.save(str(dest), 'PNG' if dest.suffix.lower() == '.png' else 'JPG'):
            QMessageBox.warning(self, _('Artwork'), _('Could not save artwork.'))
        self.display(self.folder)

class SeriesTagWorker(QThread):
    progress = Signal(int, int, str)
    finished_summary = Signal(int, int, int, str)

    def __init__(self, folder, parent=None):
        super().__init__(parent)
        self.folder = Path(folder)

    def run(self):
        from matroska_tagger import tag_series, EXTENSIONS
        from tool_manager import get_tool
        import logging
        logger = logging.getLogger(__name__)
        videos = sorted((p for p in linked_files(self.folder)
                         if p.is_file() and p.suffix.lower() in EXTENSIONS),
                        key=lambda p: str(p).casefold())
        ok = errors = 0
        try:
            executable = get_tool('mkvpropedit') if videos else None
        except (OSError, ValueError, FileNotFoundError) as exc:
            self.finished_summary.emit(0, len(videos), len(videos), str(exc))
            return
        for index, video in enumerate(videos, 1):
            self.progress.emit(index, len(videos), video.name)
            try:
                result = tag_series(video, mkvpropedit=executable)
                if result.status == 'tagged':
                    ok += 1
                else:
                    errors += 1
                    logger.warning('Serien-Nachtaggen: %s: %s', video, result.message)
            except Exception as exc:
                errors += 1
                logger.exception('Serien-Nachtaggen fehlgeschlagen: %s', video)
        self.finished_summary.emit(ok, errors, len(videos), '')


class SeriesEditorCanvas(QWidget):
    """Explicit V1-style geometry; no competing splitter size hints."""
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        # The main window must determine the editor's visible height. A fixed
        # minimum height previously pushed the action buttons below the viewport.
        self.setMinimumSize(0, 0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setStyleSheet('QLineEdit, QTextEdit { color: #202020; background: white; border: 1px solid #a9b3bf; border-radius: 2px; padding: 0px 3px; selection-background-color: #2878b8; font-size: 10pt; } QLineEdit:focus, QTextEdit:focus { border: 1px solid #2878b8; } QLabel { color: #303030; } QListWidget { color: #202020; }')
        self.list = QListWidget(self)
        self.list.currentRowChanged.connect(owner.switch_series)
        self.inputs = {}
        self.labels = {}
        for key, caption in FIELDS:
            if key == 'originaltitle':
                continue  # Preserve the NFO value without an unnecessary GUI field.
            self.labels[key] = QLabel(caption, self)
            self.inputs[key] = QLineEdit(self)
        for key, caption in (('tvdb','TVDB-ID'), ('tmdb','TMDB-ID'),
                             ('imdb','IMDb-ID'), ('custom_type',_('Custom ID – Type')),
                             ('custom_id',_('Custom ID – Value'))):
            self.labels[key] = QLabel(caption, self)
            self.inputs[key] = QLineEdit(self)
        self.labels['title'].hide()  # Title is self-explanatory; no label needed.
        self.plot_label = QLabel(_('Description'), self)
        self.plot = QTextEdit(self)
        self.artwork = [ArtworkPreview(filename, title, self) for filename, title in ARTWORK]
        self.episodes = QPushButton(_('Edit episodes'), self)
        self.episodes.setEnabled(True)
        self.tag = QPushButton(_('Retag'), self)
        self.tag.setEnabled(True)
        self.back = QPushButton(_('Back'), self)
        self.back.clicked.connect(owner.leave)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        w, h = self.width(), self.height()
        margin, gap = 6, 8
        list_w = max(190, min(270, int(w * .235)))
        meta_w = max(280, min(380, int(w * .29)))
        meta_x = margin + list_w + gap
        art_x = meta_x + meta_w + gap
        art_w = max(220, w - art_x - margin)
        self.list.setGeometry(margin, margin, list_w, h - 2 * margin)

        # Banner starts over the right-hand part of the metadata area.
        banner_h = max(72, min(118, int(h * .17)))
        # Fill precisely the artwork column, matching the graphics below.
        self.artwork[0].setGeometry(art_x, margin, art_w, banner_h)
        body_y = margin + banner_h + 7
        bottom_y = h - margin
        body_h = max(0, bottom_y - body_y)

        # Metadata uses all remaining height; no full-width footer is reserved.
        col_gap, label_h, input_h = 6, 14, 23
        half = (meta_w - col_gap) // 2
        third = (meta_w - 2 * col_gap) // 3
        rows = [
            [('title', 0, 2, 2)],
            [('tvdb', 0, 1, 3), ('tmdb', 1, 1, 3), ('imdb', 2, 1, 3)],
            [('custom_type', 0, 1, 2), ('custom_id', 1, 1, 2)],
            [('premiered', 0, 1, 3), ('rating', 1, 1, 3), ('mpaa', 2, 1, 3)],
            [('genre', 0, 1, 2), ('studio', 1, 1, 2)],
            [('sorttitle', 0, 1, 2), ('status', 1, 1, 2)],
        ]
        # Metadata starts level with the series list and banner. Preserve all
        # existing input heights and row spacing; only omit the title label.
        row_step = 38
        y = margin
        for row in rows:
            for key, column, span, columns in row:
                unit = third if columns == 3 else half
                x = meta_x + column * (unit + col_gap)
                cell_w = (meta_x + meta_w - x) if column + span == columns else unit * span + col_gap * (span - 1)
                if key == 'title':
                    self.inputs[key].setGeometry(x, y, cell_w, input_h)
                else:
                    self.labels[key].setGeometry(x, y, cell_w, label_h)
                    self.inputs[key].setGeometry(x, y + label_h, cell_w, input_h)
            y += (input_h + 1) if row[0][0] == 'title' else row_step
        self.plot_label.setGeometry(meta_x, y + 2, meta_w, label_h)
        self.plot.setGeometry(meta_x, y + label_h + 3, meta_w,
                              max(0, bottom_y - (y + label_h + 3)))

        # Only the right artwork column reserves a bottom action row.
        button_h = 30
        button_y = bottom_y - button_h
        art_bottom = button_y - 8
        art_body_h = max(0, art_bottom - body_y)
        fan_w = int(art_w * .48)
        poster_x = art_x + fan_w + gap
        poster_w = art_w - fan_w - gap
        fan_h = max(0, int(art_body_h * .43))
        self.artwork[1].setGeometry(art_x, body_y, fan_w, fan_h)
        self.artwork[2].setGeometry(poster_x, body_y, poster_w, art_body_h)
        logo_y = body_y + fan_h + gap
        logo_h = max(0, (art_bottom - logo_y - gap) // 2)
        self.artwork[3].setGeometry(art_x, logo_y, fan_w, logo_h)
        self.artwork[4].setGeometry(art_x, logo_y + logo_h + gap, fan_w,
                                    max(0, art_bottom - logo_y - logo_h - gap))

        # Three buttons are contained exclusively within the artwork column.
        button_gap = 5
        widths = [max(100, int((art_w - 2 * button_gap) * .43)),
                  max(80, int((art_w - 2 * button_gap) * .31))]
        widths.append(art_w - sum(widths) - 2 * button_gap)
        x = art_x
        for button, width in zip((self.episodes, self.tag, self.back), widths):
            button.setGeometry(x, button_y, width, button_h)
            x += width + button_gap



class MissingEpisodeThumbWorker(QThread):
    """Generate one missing sidecar thumbnail without blocking the Qt GUI."""
    finished_thumb = Signal(str, str)  # video path, error (empty on success)

    def __init__(self, video: Path, parent=None):
        super().__init__(parent)
        self.video = Path(video)

    def run(self):
        import json
        import tempfile
        video = self.video
        target = video.with_name(video.stem + '-thumb.jpg')
        error = ''
        try:
            # Never replace manually selected or downloaded artwork.
            if target.is_file():
                return
            from tool_manager import get_tool
            probe = subprocess.run([get_tool('ffprobe'), '-v', 'error',
                                    '-show_entries', 'format=duration', '-of', 'json',
                                    str(video)], capture_output=True, text=True,
                                   errors='replace', timeout=45, check=True)
            duration = float(json.loads(probe.stdout)['format']['duration'])
            if not 0 < duration < float('inf'):
                raise ValueError(_('Invalid video duration'))
            with tempfile.NamedTemporaryFile(prefix='.mb_episode_thumb_', suffix='.jpg',
                                             dir=target.parent, delete=False) as tmp:
                temporary = Path(tmp.name)
            try:
                result = subprocess.run([get_tool('ffmpeg'), '-hide_banner', '-loglevel',
                                         'error', '-nostdin', '-ss', f'{duration * .30:.3f}',
                                         '-i', str(video), '-map', '0:v:0', '-frames:v', '1',
                                         '-q:v', '3', '-y', str(temporary)],
                                        capture_output=True, timeout=90, check=False)
                if result.returncode or not temporary.is_file() or not temporary.stat().st_size:
                    raise ValueError(_('FFmpeg could not generate a still image: ') +
                                     result.stderr.decode('utf-8', errors='replace')[-350:])
                # A thumbnail might have been created by the user while extraction ran.
                # Exclusive creation avoids overwriting it.
                try:
                    with target.open('xb') as output, temporary.open('rb') as source:
                        import shutil
                        shutil.copyfileobj(source, output)
                except FileExistsError:
                    pass
                except Exception:
                    target.unlink(missing_ok=True)
                    raise
            finally:
                temporary.unlink(missing_ok=True)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            error = str(exc)
        finally:
            self.finished_thumb.emit(str(video), error)


class EpisodeEditor(QWidget):
    """First working episode NFO editor; parent controls the series selection."""
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.episode_files = []
        self.current_file = None
        self.current_tree = None
        self.busy = False
        self._thumb_workers = {}
        self.setStyleSheet('QLineEdit, QTextEdit {color:#202020;background:white;border:1px solid #a9b3bf;padding:1px 3px;font-size:10pt;} QListWidget {color:#202020;} QLabel {color:#303030;}')
        # Test 146e: four independent columns. The banner spans description
        # and poster, while the metadata and episode list use the full height.
        main = QGridLayout(self)
        main.setContentsMargins(6, 6, 6, 6)
        main.setHorizontalSpacing(9)
        main.setVerticalSpacing(6)
        for col, stretch in enumerate((28, 24, 25, 23)):
            main.setColumnStretch(col, stretch)
        # Protect the episode list and metadata from artwork-driven compression.
        # Minimum widths, not fixed widths; other views remain unchanged.
        for col, minimum in enumerate((205, 205, 210, 274)):
            main.setColumnMinimumWidth(col, minimum)
        main.setRowStretch(1, 1)

        self.list = QListWidget()
        self.list.setMinimumWidth(175)
        self.list.setMinimumHeight(0)
        main.addWidget(self.list, 0, 0, 2, 1)

        middle = QWidget()
        form = QVBoxLayout(middle)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(4)
        self.fields = {}
        for key, caption in (('title', _('Title')), ('showtitle', _('Series title'))):
            form.addWidget(QLabel(caption))
            edit = QLineEdit()
            self.fields[key] = edit
            form.addWidget(edit)
        numbers = QHBoxLayout()
        for key, caption in (('season', _('Season')), ('episode', _('Episode'))):
            column = QVBoxLayout()
            column.setContentsMargins(0, 0, 0, 0)
            column.addWidget(QLabel(caption))
            edit = QLineEdit()
            self.fields[key] = edit
            column.addWidget(edit)
            numbers.addLayout(column)
        form.addLayout(numbers)
        form.addWidget(QLabel(_('Released')))
        self.fields['aired'] = QLineEdit()
        form.addWidget(self.fields['aired'])
        form.addWidget(QLabel(_('Episode thumbnail')))
        self.thumb = ArtworkPreview('', 'Thumbnail', self)
        self.thumb.keep_aspect = True
        # No black letterbox bars around aspect-preserved previews.
        self.thumb.image.setStyleSheet('QPushButton { background: transparent; color: #303030; border: none; }')
        self.thumb.setMinimumHeight(0)
        form.addWidget(self.thumb, 1)
        main.addWidget(middle, 0, 1, 2, 1)

        self.banner = ArtworkPreview('banner.jpg', 'Banner', self)
        self.banner.setFixedHeight(100)
        main.addWidget(self.banner, 0, 2, 1, 2)

        description = QWidget()
        description_layout = QVBoxLayout(description)
        description_layout.setContentsMargins(0, 0, 0, 0)
        description_layout.setSpacing(4)
        description_layout.addWidget(QLabel(_('Description')))
        self.plot = QTextEdit()
        self.plot.setMinimumHeight(0)
        description_layout.addWidget(self.plot, 1)
        main.addWidget(description, 1, 2)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)
        self.poster = ArtworkPreview('poster.jpg', 'Staffelposter', self)
        self.poster.keep_aspect = True
        self.poster.image.setStyleSheet('QPushButton { background: transparent; color: #303030; border: none; }')
        self.poster.setMinimumHeight(0)
        right_layout.addWidget(self.poster, 1)
        buttons = QHBoxLayout()
        buttons.setSpacing(4)
        self.refresh_thumb = QPushButton(_('Refresh thumbnail'))
        self.refresh_thumb.setEnabled(True)
        self.play = QPushButton(_('Play'))
        self.play.setEnabled(True)
        self.back = QPushButton(_('Back'))
        for button in (self.refresh_thumb, self.play, self.back):
            button.setMinimumWidth(0)
            buttons.addWidget(button)
        right_layout.addLayout(buttons)
        main.addWidget(right, 1, 3)
        self.list.currentRowChanged.connect(self.switch_episode)
        self.back.clicked.connect(self.go_back)
        self.play.clicked.connect(self.play_episode)
        self.refresh_thumb.clicked.connect(self.renew_thumb)

    def open_series(self, folder):
        self.series_folder = folder
        self.busy = True
        patterns = media_patterns()
        self.episode_files = sorted((p for p in linked_files(folder) if is_media(p,patterns)),
                                    key=lambda p: str(p.relative_to(folder)).casefold())
        self.list.clear()
        for file in self.episode_files:
            self.list.addItem(str(file.relative_to(folder)))
        self.busy = False
        self.current_file = None
        self.current_tree = None
        ensure_active_season_artwork(folder, self.episode_files)
        self.banner.display(folder)
        self.poster.display(folder)
        self.list.setCurrentRow(0 if self.episode_files else -1)
        if not self.episode_files:
            self.owner.report_status(_('No episodes found in the series folder.'))

    def nfo_path(self, file):
        return file.with_suffix('.nfo')

    def save_episode(self):
        if self.current_file is None or self.current_tree is None:
            return True
        root = self.current_tree.getroot()
        values = {key: edit.text() for key, edit in self.fields.items()}
        old = root.findtext('aired') or root.findtext('premiered') or ''
        date_warning = None
        if values['aired'] != old:
            try:
                values['aired'] = normalize_date(values['aired'])
            except InvalidDate as exc:
                date_warning = str(exc)
                values['aired'] = old
        try:
            for key in ('title','showtitle','season','episode'):
                node = root.find(key)
                if node is None:
                    node = ET.SubElement(root,key)
                node.text = values[key]
            # Respect the existing date tag, if present.
            date_tag = 'aired' if root.find('aired') is not None else 'premiered'
            node = root.find(date_tag)
            if node is None:
                node = ET.SubElement(root,date_tag)
            node.text = values['aired']
            node = root.find('plot')
            if node is None:
                node = ET.SubElement(root,'plot')
            node.text = self.plot.toPlainText()
            dest = self.nfo_path(self.current_file)
            if not can_autosave(self, dest, self.owner.report_status):
                return True
            tmp = dest.with_suffix('.nfo.tmp')
            try:
                self.current_tree.write(tmp,encoding='utf-8',xml_declaration=True)
                os.replace(tmp,dest)
            finally:
                tmp.unlink(missing_ok=True)
            # Save NFO first; rename media and its support files only afterwards.
            # The already saved metadata is retained if a rename conflict occurs.
            rename_warning = None
            try:
                renamed, count = rename_episode_files(self.current_file, self.current_tree,
                                                        Path(__file__).resolve().parent)
                if renamed != self.current_file:
                    index = self.episode_files.index(self.current_file)
                    self.episode_files[index] = renamed
                    self.current_file = renamed
                    self.thumb.filename = renamed.stem + '-thumb.jpg'
                    blocker = QSignalBlocker(self.list)
                    self.list.item(index).setText(str(renamed.relative_to(self.series_folder)))
                    del blocker
                    self.thumb.display(renamed.parent)
                    self.owner.report_status(_('Renamed: {name} ({count} files)').format(name=renamed.name, count=count))
            except (OSError, ValueError) as exc:
                rename_warning = str(exc)
            if rename_warning:
                self.owner.report_status(_('NFO saved, unable to rename: {rename_warning}').format(rename_warning=rename_warning))
                QMessageBox.warning(self, _('Unable to rename'),
                                    _('The NFO was saved.\n{rename_warning}').format(rename_warning=rename_warning))
            if date_warning:
                self.owner.report_status(_('{date_warning} – date unchanged, other episode fields saved').format(date_warning=date_warning))
                QMessageBox.warning(self,_('Invalid date'),_('{date_warning}\nThe existing date is preserved.').format(date_warning=date_warning))
            elif not rename_warning:
                self.owner.report_status(f'Gespeichert: {self.nfo_path(self.current_file).name}')
            return True
        except (OSError,ValueError,ET.ParseError) as exc:
            self.owner.report_status(_('Episode NFO could not be saved: {exc}').format(exc=exc))
            report_save_failure(self, self.nfo_path(self.current_file), exc, _('Save error'))
            return True

    def switch_episode(self,row):
        if self.busy:
            return
        old = self.current_file
        if not self.save_episode():
            old_row = self.episode_files.index(old) if old in self.episode_files else -1
            blocker = QSignalBlocker(self.list)
            self.list.setCurrentRow(old_row)
            del blocker
            return
        self.current_file = self.episode_files[row] if 0 <= row < len(self.episode_files) else None
        self.current_tree = None
        for field in self.fields.values():
            field.clear()
        self.plot.clear()
        self.thumb.display(None)
        if self.current_file is None:
            return
        file = self.current_file
        nfo = self.nfo_path(file)
        if not nfo.exists():
            self.owner.report_status(_('No episode NFO: {nfo_name}').format(nfo_name=nfo.name))
            return
        try:
            parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
            tree = ET.parse(nfo,parser=parser)
            root = tree.getroot()
            if root.tag != 'episodedetails':
                raise ValueError(_('Unexpected NFO element: {tag}').format(tag=root.tag))
            self.current_tree = tree
            for key,edit in self.fields.items():
                edit.setText((root.findtext(key) or '') if key != 'aired' else (root.findtext('aired') or root.findtext('premiered') or ''))
            self.plot.setPlainText(root.findtext('plot') or '')
            # The thumbnail is stored alongside its media file, not the series root.
            self.thumb.filename = file.stem + '-thumb.jpg'
            self.thumb.display(file.parent)
            self.ensure_episode_thumb(file)
            season = root.findtext('season') or ''
            try:
                season_number = int(season)
                if season_number < 0:
                    raise ValueError(_('Negative season number'))
                self.poster.filename = ('season-specials-poster.jpg' if season_number == 0
                                        else f'season{season_number:02d}-poster.jpg')
                self.banner.filename = ('season-specials-banner.jpg' if season_number == 0
                                        else f'season{season_number:02d}-banner.jpg')
            except ValueError:
                self.poster.filename = 'season-specials-poster.jpg'
                self.banner.filename = 'season-specials-banner.jpg'
            self.banner.display(self.series_folder)
            self.poster.display(self.series_folder)
            self.owner.report_status(_('Episode loaded: {file_name}').format(file_name=file.name))
        except (OSError,ValueError,ET.ParseError) as exc:
            self.owner.report_status(_('Could not read the episode NFO: {exc}').format(exc=exc))
            QMessageBox.warning(self,_('Episode NFO'),str(exc))

    def ensure_episode_thumb(self, media):
        """Create missing artwork once, at 30 percent of video duration."""
        media = Path(media)
        if media.suffix.lower() in ('.stub', '.disc') or not media.is_file():
            return
        if media.with_name(media.stem + '-thumb.jpg').is_file():
            return
        key = str(media)
        if key in self._thumb_workers:
            return
        worker = MissingEpisodeThumbWorker(media, self)
        self._thumb_workers[key] = worker
        worker.finished_thumb.connect(self._episode_thumb_generated)
        worker.finished.connect(lambda k=key: self._thumb_workers.pop(k, None))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _episode_thumb_generated(self, filename, error):
        media = Path(filename)
        if error:
            self.owner.report_status(_('Automatic episode thumbnail failed: {error}').format(error=error))
            return
        if self.current_file == media:
            self.thumb.display(media.parent)
        self.owner.report_status(_('Added episode thumbnail: {media_name}').format(media_name=media.name))

    def renew_thumb(self):
        """Choose a video frame; never alter the video or unrelated artwork."""
        if self.current_file is None:
            self.owner.report_status(_('No episode selected.'))
            return
        if not self.save_episode():
            return
        media = self.current_file
        if media.suffix.lower() in ('.stub', '.disc'):
            QMessageBox.information(self, _('Refresh thumbnail'),
                                    _('Frames cannot be generated for stub/disc files.'))
            return
        if not media.is_file():
            QMessageBox.warning(self, _('Refresh thumbnail'), _('File not found: {media}').format(media=media))
            return
        from episode_thumb_selector import EpisodeThumbSelector
        dialog = EpisodeThumbSelector(media, self)
        try:
            if dialog.exec() != dialog.DialogCode.Accepted:
                return
            selected = dialog.selected_image()
            if selected is None:
                return
            target = media.with_name(media.stem + '-thumb.jpg')
            from PySide6.QtGui import QImageReader
            import tempfile
            image = QImageReader(str(selected)).read()
            if image.isNull():
                raise ValueError(_('The selected frame could not be read.'))
            with tempfile.NamedTemporaryFile(prefix='.mb_thumb_', suffix='.jpg',
                                             dir=target.parent, delete=False) as tmp:
                temp_path = Path(tmp.name)
            try:
                if not image.save(str(temp_path), 'JPG', 93):
                    raise OSError(_('Could not save the still image as JPEG.'))
                os.replace(temp_path, target)
            finally:
                temp_path.unlink(missing_ok=True)
            self.thumb.display(media.parent)
            self.owner.report_status(_('Thumbnail replaced: {target_name}').format(target_name=target.name))
        except (OSError, ValueError) as exc:
            self.owner.report_status(_('Thumbnail could not be replaced: {exc}').format(exc=exc))
            QMessageBox.warning(self, _('Refresh thumbnail'), str(exc))
        finally:
            dialog.cleanup()

    def play_episode(self):
        """Launch the configured mpv player for the selected real media file."""
        if self.current_file is None:
            self.owner.report_status(_('No episode selected for playback.'))
            return
        # Save before playback; a rename may also update self.current_file.
        if not self.save_episode():
            return
        media = self.current_file
        if not media.is_file():
            self.owner.report_status(_('Media file not found: {media_name}').format(media_name=media.name))
            QMessageBox.warning(self, _('Play'), _('The file was not found:\n{media}').format(media=media))
            return
        if media.suffix.lower() in ('.stub', '.disc'):
            self.owner.report_status(_('Unplayable placeholder file: {media_name}').format(media_name=media.name))
            QMessageBox.information(self, _('Play'),
                                    _('This episode is a stub or disc placeholder and contains no playable video.'))
            return
        try:
            from tool_manager import get_tool
            player = get_tool('mpv')
            subprocess.Popen([player, str(media)], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             cwd=str(media.parent))
            self.owner.report_status(_('Playing: {media_name}').format(media_name=media.name))
        except (OSError, ValueError) as exc:
            self.owner.report_status(_('Unable to play: {exc}').format(exc=exc))
            QMessageBox.warning(self, _('Unable to play'), str(exc))

    def go_back(self):
        if self.save_episode():
            self.owner.pages.setCurrentIndex(1)

class SeriesPostprocess(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.folder = None
        self.current = None
        self.tree = None
        self.loading = False
        self.series = []
        self.tag_worker = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(5, 5, 5, 5)
        outer.setSpacing(3)
        self.pages = QStackedWidget()
        outer.addWidget(self.pages, 1)
        self.folder_edit = QLineEdit()
        self.folder_edit.setText(QSettings('BuddyTools', 'Media-Buddy-V2').value('postprocess/series_folder', ''))
        entry = make_entry(self.folder_edit, self.browse, self.open_folder, _('Post-processing: TV series'), _('TV series folder'))
        self.pages.addWidget(entry)
        editor_page = QWidget()
        editor_outer = QVBoxLayout(editor_page)
        editor_outer.setContentsMargins(0, 0, 0, 0)
        self.canvas = SeriesEditorCanvas(self)
        editor_outer.addWidget(self.canvas, 1)
        editor_page.setMinimumSize(0, 0)
        editor_page.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.list = self.canvas.list
        self.inputs = self.canvas.inputs
        self.plot = self.canvas.plot
        self.artwork = self.canvas.artwork
        self.canvas.tag.clicked.connect(self.retag_current_series)
        self.pages.addWidget(editor_page)
        self.episode_editor = EpisodeEditor(self)
        self.pages.addWidget(self.episode_editor)
        self.canvas.episodes.clicked.connect(self.open_episodes)

    def open_episodes(self):
        if self.current is None or not self.save_current():
            return
        self.episode_editor.open_series(self.current)
        self.pages.setCurrentIndex(2)

    def report_status(self, message):
        """Use the existing main-window status bar, next to its clock."""
        host = self.window()
        if hasattr(host, 'set_work_status'):
            host.set_work_status('phase', message)

    def retag_current_series(self):
        if self.current is None or (self.tag_worker is not None and self.tag_worker.isRunning()):
            return
        if not self.save_current():
            return
        folder = self.current
        self.tag_worker = SeriesTagWorker(folder, self)
        self.tag_worker.progress.connect(
            lambda index, total, name: self.report_status(
                _('Retagging: {folder_name} – {index}/{total}: {name}').format(folder_name=folder.name, index=index, total=total, name=name)))
        self.tag_worker.finished_summary.connect(self.retag_finished)
        self.canvas.tag.setEnabled(False)
        self.canvas.back.setEnabled(False)
        self.canvas.list.setEnabled(False)
        self.report_status(_('Retagging: {folder_name} …').format(folder_name=folder.name))
        self.tag_worker.start()

    def retag_finished(self, ok, errors, total, detail):
        self.canvas.tag.setEnabled(True)
        self.canvas.back.setEnabled(True)
        self.canvas.list.setEnabled(True)
        if detail:
            message = _('Retagging failed: {detail}').format(detail=detail)
        else:
            message = _('Retagging completed: {ok}/{total} episodes, {errors} errors').format(ok=ok, total=total, errors=errors)
        self.report_status(message)
        if detail or errors:
            QMessageBox.warning(self, _('Retag TV series'), message)

    def browse(self):
        folder = QFileDialog.getExistingDirectory(self, _('Select TV series folder'), self.folder_edit.text())
        if folder:
            self.folder_edit.setText(folder)

    def open_folder(self, folder_override=None):
        # Qt clicked(bool) darf nicht als Ordnerpfad interpretiert werden.
        if isinstance(folder_override, bool):
            folder_override = None
        if not self.save_current():
            return
        folder = Path(folder_override if folder_override is not None else self.folder_edit.text().strip())
        if not folder.is_dir():
            QMessageBox.warning(self, _('TV series'), _('The selected folder does not exist.'))
            return
        self.folder = folder
        if folder_override is None:
            QSettings('BuddyTools', 'Media-Buddy-V2').setValue('postprocess/series_folder', str(folder))
        else:
            self.folder_edit.setText(str(folder))
        self.series = series_with_videos(folder)
        self.loading = True
        self.list.clear()
        self.list.addItems([p.name for p in self.series])
        self.loading = False
        self.current = None
        self.tree = None
        self.pages.setCurrentIndex(1)
        self.list.setCurrentRow(0 if self.series else -1)
        if not self.series:
            self.report_status(_('No series with video or stub files found.'))

    def values(self):
        values = {key: field.text() for key, field in self.inputs.items()}
        values['originaltitle'] = self.tree.getroot().findtext('originaltitle') or '' if self.tree is not None else ''
        values['plot'] = self.plot.toPlainText()
        return values

    def save_current(self):
        if self.current is None or self.tree is None:
            return True
        dest = self.current / 'tvshow.nfo'
        if not can_autosave(self, dest, self.report_status):
            return True
        try:
            date_warning = write_series(self.current, self.tree, self.values())
            if date_warning:
                _('{date_warning} – date unchanged, remaining metadata saved.').format(date_warning=date_warning)
                self.report_status(message)
                QMessageBox.warning(self, _('Invalid date'),
                    _('{date_warning}\n\nThe existing date in the NFO is preserved. Other changes were saved and editing can continue.').format(date_warning=date_warning))
            else:
                self.report_status(f'Gespeichert: {self.current.name}/tvshow.nfo')
            return True
        except (OSError, ValueError, ET.ParseError) as exc:
            self.report_status(_('NFO could not be saved: {exc}').format(exc=exc))
            report_save_failure(self, dest, exc, _('Could not save NFO'))
            return True

    def restore_series_selection(self, row):
        """Keep the list selection consistent with the displayed series."""
        blocker = QSignalBlocker(self.list)
        self.list.setCurrentRow(row)
        # setCurrentRow normally selects the item as well; explicitly clear
        # any leftover selection if there is no active series.
        if row < 0:
            self.list.clearSelection()
        del blocker

    def switch_series(self, row):
        if self.loading:
            return
        old = self.current
        if not self.save_current():
            # A mouse click may finish changing QListWidget's selection after
            # currentRowChanged has returned (especially after a modal dialog).
            # Restore the old selection once the pending GUI event has ended.
            old_row = self.series.index(old) if old in self.series else -1
            self.restore_series_selection(old_row)
            QTimer.singleShot(0, lambda row=old_row: self.restore_series_selection(row))
            return
        self.current = self.series[row] if 0 <= row < len(self.series) else None
        self.tree = None
        for field in self.inputs.values():
            field.clear()
        self.plot.clear()
        for art in self.artwork:
            art.display(self.current)
        if self.current is None:
            return
        if not self.current.is_dir():
            self.report_status(_('TV show folder no longer exists: {self_current}').format(self_current=self.current))
            return
        try:
            self.tree = read_series(self.current)
            values = get_values(self.tree)
            for key, field in self.inputs.items():
                field.setText(values[key])
            self.plot.setPlainText(values['plot'])
            self.report_status(f'Serie geladen: {self.current.name}')
        except (OSError, ValueError, ET.ParseError) as exc:
            self.report_status(f'{self.current.name}: NFO nicht lesbar')
            QMessageBox.warning(self, _('TV show NFO'), str(exc))

    def leave(self):
        if self.save_current():
            if self.pages.currentIndex() == 1:
                self.pages.setCurrentIndex(0)
            else:
                self.window().select_section('Assistenten')

    def closeEvent(self, event):
        if self.pages.currentIndex() == 2 and not self.episode_editor.save_episode():
            event.ignore()
            return
        if self.save_current():
            event.accept()
        else:
            event.ignore()
