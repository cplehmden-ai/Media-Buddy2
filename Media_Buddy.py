"""Media-Buddy V2 – Test 122a: eigenständiges GUI-Grundgerüst (PySide6)."""
from __future__ import annotations

import sys
import os
import json
import time
from datetime import datetime
from pathlib import Path
from wizard_page import WizardPage, RecodePage, RemuxPage, AudioNormalizePage, AppendPage
from other_video_page import OtherVideoPage
from metadata_page import MetadataPage
from series_postprocess import SeriesPostprocess
from movie_postprocess import MoviePostprocess
from musicvideo_postprocess import MusicvideoPostprocess
from other_video_postprocess import OtherVideoPostprocess
from video_test_page import VideoTestPage
from global_settings_page import GlobalSettingsPage
from gui_i18n import _, save_languages, load_languages, available_gui_languages, navigation_label, stage_label
from configuration_bootstrap import ensure_configuration
from batch_settings import SettingsError
from configuration_store import load_raw
from activity_log import event as activity_event

try:
    from PySide6.QtCore import Qt, QRect, QSettings, QTimer, QProcess
    from PySide6.QtGui import QAction, QPixmap, QPainter, QLinearGradient, QColor
    from PySide6.QtWidgets import (
        QApplication, QFrame, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
        QPushButton, QScrollArea, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget,
        QComboBox, QGridLayout, QGroupBox, QDialog, QTextBrowser, QDialogButtonBox,
    )
except ImportError as exc:
    raise SystemExit("PySide6 fehlt. Bitte installieren: python -m pip install PySide6") from exc

ROOT = Path(__file__).resolve().parent
LOGO = ROOT / "assets" / "media_buddy_v2_header.png"

# Vorläufige Menüstruktur. Die tatsächlichen Funktionen folgen in späteren Tests.
SECTIONS = {
    "Assistenten": ["Serien-Aufarbeitung", "Spielfilme-Aufarbeitung", "Musikvideo-Aufarbeitung", "Video-Aufarbeitung", "Globale Aufarbeitung", "Audio-Aufarbeitung"],
    "Bearbeitung": ["Recode", "Remux", "Audio-Normalisieren", "Videos zusammenfügen"],
    "Metadaten": ["Serien", "Spielfilme", "Musikvideos", "Sonstige Videos"],
    "Nacharbeiten": ["Serien", "Spielfilme", "Musikvideos", "Sonstige Videos", "Videos-Test"],
    "Hilfsmittel": ["Werkzeuge"],
    "Einstellungen": ["Allgemein", "Assistenten", "Video", "Metadaten", "Dateinamen", "Audio", "Grafiken", "Tools"],
}


class Header(QFrame):
    """Skalierbarer Header mit unabhängig von der Fenstergröße gezeichnetem Verlauf."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("header")
        self.setFixedHeight(132)
        self.logo = QLabel(self)
        self.logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.logo.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.original = QPixmap(str(LOGO))
        # Test 110: Die ursprüngliche Qt-Monochrommaske erfasste auch sehr
        # schwache, nahezu unsichtbare Leuchteffekte und ließ das Logo klein.
        # Die tatsächliche sichtbare Bildfläche wird anhand des Alphakanals
        # bestimmt. Ein kleiner Rand schützt die Neon-Effekte vor Beschnitt.
        if not self.original.isNull() and self.original.hasAlphaChannel():
            bounds = self._visible_bounds(self.original.toImage(), threshold=40)
            if bounds is not None:
                pad = 12
                bounds = bounds.adjusted(-pad, -pad, pad, pad).intersected(self.original.rect())
                self.original = self.original.copy(bounds)
        if self.original.isNull():
            self.logo.setText("Media-Buddy V2")

    @staticmethod
    def _visible_bounds(image, threshold=40):
        """Bounding-Box deutlich sichtbarer Pixel; keine 1-Bit-Alpha-Maske."""
        left, top = image.width(), image.height()
        right, bottom = -1, -1
        for y in range(image.height()):
            for x in range(image.width()):
                if image.pixelColor(x, y).alpha() > threshold:
                    left = min(left, x)
                    top = min(top, y)
                    right = max(right, x)
                    bottom = max(bottom, y)
        if right < left:
            return None
        return QRect(left, top, right - left + 1, bottom - top + 1)

    def paintEvent(self, event):
        painter = QPainter(self)
        gradient = QLinearGradient(0, 0, self.width(), 0)
        gradient.setColorAt(0.0, QColor("#101a30"))
        gradient.setColorAt(0.25, QColor("#153e70"))
        gradient.setColorAt(0.52, QColor("#087ca9"))
        gradient.setColorAt(0.75, QColor("#154875"))
        gradient.setColorAt(1.0, QColor("#101827"))
        painter.fillRect(self.rect(), gradient)
        painter.end()
        super().paintEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Ziel: 160 Pixel Logo-Höhe bei 168 Pixel Headerhöhe.
        margin_x, margin_y = 10, 4
        self.logo.setGeometry(margin_x, margin_y,
                              max(1, self.width() - 2 * margin_x),
                              max(1, self.height() - 2 * margin_y))
        if not self.original.isNull():
            self.logo.setPixmap(self.original.scaled(
                self.logo.size(), Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))



class LanguageSettingsPage(QWidget):
    """Independent GUI/metadata/audio/subtitle preferences. GUI requires restart."""
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        group = QGroupBox(_('Language settings'))
        grid = QGridLayout(group)
        self.fields = {}
        saved = load_languages()
        self._gui_language_at_open = saved.get('gui','de')
        labels = [("gui", _('GUI language')), ("metadata", _('Metadata language')),
                  ("audio", _('Preferred audio language')),
                  ("subtitles", _('Preferred subtitle language'))]
        # ISO language codes are stored, never translated labels.
        languages = [("de", _('German')), ("en", _('English')),
                     ("fr", "Français"), ("es", "Español"), ("it", "Italiano"),
                     ("pt", "Português"), ("nl", "Nederlands"), ("pl", "Polski")]
        known_names = dict(languages)
        gui_languages = [(code, known_names.get(code, code)) for code in available_gui_languages()]
        for row, (key, label) in enumerate(labels):
            grid.addWidget(QLabel(label), row, 0)
            combo = QComboBox()
            for code, name in languages if key != "gui" else gui_languages:
                combo.addItem(name, code)
            index = combo.findData(saved.get(key, "de"))
            if index < 0:
                code = saved.get(key, "de")
                combo.addItem(code, code)
                index = combo.findData(code)
            combo.setCurrentIndex(max(0, index))
            grid.addWidget(combo, row, 1)
            self.fields[key] = combo
        grid.addWidget(QLabel(_('Set all languages to')), len(labels), 0)
        self.all_languages = QComboBox()
        for code, name in languages[:2]:
            self.all_languages.addItem(name, code)
        grid.addWidget(self.all_languages, len(labels), 1)
        apply_all = QPushButton(_('Apply to all'))
        apply_all.clicked.connect(self.set_all)
        grid.addWidget(apply_all, len(labels), 2)
        save = QPushButton(_('Save settings'))
        save.clicked.connect(self.save)
        grid.addWidget(save, len(labels)+1, 2)
        grid.addWidget(QLabel(_('The language change takes effect after restarting the program.')), len(labels)+2, 0, 1, 3)
        layout.addWidget(group)
        layout.addStretch()

    def set_all(self):
        code = self.all_languages.currentData()
        for combo in self.fields.values():
            index = combo.findData(code)
            if index >= 0:
                combo.setCurrentIndex(index)
        # Explicit action only changes fields; save remains a separate action.

    def save(self):
        try:
            save_languages({key: combo.currentData() for key, combo in self.fields.items()})
        except OSError as exc:
            QMessageBox.critical(self, _('Save failed'), str(exc))
            return
        if self.fields['gui'].currentData() != self._gui_language_at_open:
            dialog=QMessageBox(self)
            dialog.setWindowTitle(_('Restart required'))
            dialog.setText(_('The new GUI language takes effect after restarting. Restart now?'))
            later=dialog.addButton(_('Later'), QMessageBox.ButtonRole.RejectRole)
            now=dialog.addButton(_('Restart now'), QMessageBox.ButtonRole.AcceptRole)
            dialog.setDefaultButton(later)
            dialog.exec()
            if dialog.clickedButton() is now:
                window=self.window()
                if hasattr(window,'request_restart'):
                    window.request_restart()
        else:
            QMessageBox.information(self, _('Saved'),
                                    _('Settings saved.'))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Media-Buddy V2")
        # Bewusst unter 1000 px: Qt resize() misst den Inhaltsrahmen,
        # die Windows-Titelleiste kommt zur äußeren Fensterhöhe hinzu.
        self.resize(1250, 850)
        self.setMinimumSize(790, 550)
        self._active_section = "Assistenten"
        self._active_page = "Serien-Aufarbeitung"
        self._nav_buttons = {}
        self._sub_buttons = {}

        file_menu = self.menuBar().addMenu(_('File'))
        delete_action = QAction(_('Delete originals …'), self)
        delete_action.triggered.connect(self._delete_originals_info)
        file_menu.addAction(delete_action)
        file_menu.addSeparator()
        quit_action = QAction(_('Exit'), self)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)
        help_menu = self.menuBar().addMenu(_('Help'))
        about_action = QAction(_('About Media-Buddy V2'), self)
        about_action.triggered.connect(self._about)
        help_menu.addAction(about_action)

        container = QWidget()
        self.setCentralWidget(container)
        outer = QVBoxLayout(container)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.header = Header()
        outer.addWidget(self.header)

        self.nav = QWidget()
        self.nav.setObjectName("navigation")
        nav_layout = QHBoxLayout(self.nav)
        nav_layout.setContentsMargins(8, 3, 8, 3)
        nav_layout.setSpacing(3)
        for name in SECTIONS:
            button = QPushButton(navigation_label(name))
            button.setObjectName("navButton")
            button.setCheckable(True)
            button.clicked.connect(lambda checked=False, n=name: self.select_section(n))
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            nav_layout.addWidget(button)
            self._nav_buttons[name] = button
        outer.addWidget(self.nav)

        self.subnav = QWidget()
        self.subnav.setObjectName("subnavigation")
        self.sub_layout = QHBoxLayout(self.subnav)
        self.sub_layout.setContentsMargins(8, 2, 8, 2)
        self.sub_layout.setSpacing(3)
        outer.addWidget(self.subnav)

        self.content = QFrame()
        self.content.setObjectName("content")
        content_layout = QVBoxLayout(self.content)
        content_layout.setContentsMargins(16, 10, 16, 10)
        content_layout.setSpacing(6)
        self.heading = QLabel()
        self.heading.setObjectName("pageTitle")
        self.heading.setWordWrap(True)
        content_layout.addWidget(self.heading)
        self.description = QLabel(_('The main window and navigation are ready.\nWizards and other functions will be added step by step.'))
        self.description.setObjectName("description")
        self.description.setWordWrap(True)
        content_layout.addWidget(self.description)
        self.wizard_pages = {}
        self.language_page = None
        self.global_settings_page = None
        self.wizard_slot = QVBoxLayout()
        self.wizard_slot.setContentsMargins(0, 0, 0, 0)
        content_layout.addLayout(self.wizard_slot, 1)
        outer.addWidget(self.content, 1)
        # V1-inspirierte, einzeilige Statusleiste mit fester Uhr rechts.
        self._status_text = _('Idle')
        self._file_started = None
        self._last_progress = {}
        self._status_left = QLabel(_('Idle'))
        self._status_left.setMinimumWidth(0)
        self._status_left.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._status_clock = QLabel()
        self._status_clock.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._status_clock.setMinimumWidth(64)
        self._status_clock.setStyleSheet("font-weight:600; color:#243c54; padding-right:6px;")
        # An den Inhaltsboxen ausrichten: links/rechts je ca. 26 px Abstand.
        self._status_left_margin = QLabel()
        self._status_left_margin.setFixedWidth(20)
        self._status_right_margin = QLabel()
        self._status_right_margin.setFixedWidth(20)
        self.statusBar().addWidget(self._status_left_margin)
        self.statusBar().addWidget(self._status_left, 1)
        self.statusBar().addPermanentWidget(self._status_clock)
        self.statusBar().addPermanentWidget(self._status_right_margin)
        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._refresh_status)
        self._status_timer.start(1000)
        self._refresh_status()
        self.setStyleSheet(STYLE)
        # Stable navigation identifiers, independent of translated captions.
        self._nav_ids = {name: i for i, name in enumerate(SECTIONS)}
        # QSettings restoration must work even if the legacy JSON is absent.
        saved = {}
        try:
            saved = json.loads((ROOT / "wizard_gui_settings.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            pass
        prefs = QSettings("BuddyTools", "Media-Buddy-V2")
        main = prefs.value("gui/last_main_tab", saved.get("LastMainTab", "Assistenten"))
        sub = prefs.value("gui/last_sub_tab", saved.get("LastSubTab", "Serien-Aufarbeitung"))
        if main not in SECTIONS or sub not in SECTIONS[main]:
            main, sub = "Assistenten", "Serien-Aufarbeitung"
        self.select_section(main)
        self.select_page(sub)

    def open_movie_postprocess_for_folder(self, folder):
        target = Path(folder).expanduser()
        if not target.is_dir():
            QMessageBox.warning(self, _('Post-processing'), _('Destination folder does not exist: {target}').format(target=target))
            return
        self.select_section('Nacharbeiten', initial_page='Spielfilme')
        editor = self.wizard_pages.get('Nacharbeiten/Spielfilme')
        if editor is not None:
            editor.open_folder(folder_override=str(target))

    def open_series_postprocess_for_folder(self, folder):
        """Endaktion: direkt in den Editor mit dem Assistenten-Zielordner."""
        target = Path(folder).expanduser()
        if not target.is_dir():
            QMessageBox.warning(self, _('Post-processing'),
                                f'{_('Destination folder does not exist:')} {target}')
            return
        self.select_section('Nacharbeiten')
        self.select_page('Serien')
        editor = self.wizard_pages.get('Nacharbeiten/Serien')
        if editor is not None:
            editor.open_folder(folder_override=str(target))

    def open_other_video_postprocess_for_folder(self, folder):
        target = Path(folder).expanduser()
        if not target.is_dir():
            QMessageBox.warning(self, _('Post-processing'),
                                _('Destination folder does not exist: {target}').format(target=target))
            return
        self.select_section('Nacharbeiten', initial_page='Sonstige Videos')
        editor = self.wizard_pages.get('Nacharbeiten/Sonstige Videos')
        if editor is not None:
            editor.open_folder(folder_override=str(target))

    def select_section(self, section, initial_page=None):
        if getattr(self, "_active_section", None) == "Nacharbeiten" and getattr(self, "_active_page", None) == "Serien":
            editor = self.wizard_pages.get("Nacharbeiten/Serien")
            if editor is not None and not editor.save_current():
                return
        if getattr(self, "_active_section", None) == "Nacharbeiten" and getattr(self, "_active_page", None) == "Sonstige Videos":
            editor = self.wizard_pages.get("Nacharbeiten/Sonstige Videos")
            if editor is not None and not editor.save_current():
                return
        self._active_section = section
        for name, button in self._nav_buttons.items():
            button.setChecked(name == section)
        while self.sub_layout.count():
            item = self.sub_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._sub_buttons = {}
        for page in SECTIONS[section]:
            label = _('Videos') if section in ("Metadaten", "Nacharbeiten") and page == "Sonstige Videos" else navigation_label(page)
            button = QPushButton(label)
            button.setObjectName("subButton")
            button.setCheckable(True)
            button.clicked.connect(lambda checked=False, p=page: self.select_page(p))
            self.sub_layout.addWidget(button)
            self._sub_buttons[page] = button
        self.sub_layout.addStretch()
        self.select_page(initial_page if initial_page in SECTIONS[section] else SECTIONS[section][0])

    def select_page(self, page):
        if getattr(self, "_active_section", None) == "Nacharbeiten" and getattr(self, "_active_page", None) == "Serien":
            editor = self.wizard_pages.get("Nacharbeiten/Serien")
            if editor is not None and not editor.save_current():
                return
        if getattr(self, "_active_section", None) == "Nacharbeiten" and getattr(self, "_active_page", None) == "Sonstige Videos":
            editor = self.wizard_pages.get("Nacharbeiten/Sonstige Videos")
            if editor is not None and not editor.save_current():
                return
        activity_event(f"Bereich geöffnet: {self._active_section} / {page}")
        if getattr(self, '_active_page', None) != page or getattr(self, '_last_status_section', None) != self._active_section:
            self._status_text = _('Idle')
            self._file_started = None
            self._last_progress = {}
            self._refresh_status()
        self._last_status_section = self._active_section
        self._active_page = page
        # Persist the last visited module immediately, not only at shutdown.
        settings = QSettings("BuddyTools", "Media-Buddy-V2")
        settings.setValue("gui/last_main_tab", self._active_section)
        settings.setValue("gui/last_sub_tab", page)
        for name, button in self._sub_buttons.items():
            button.setChecked(name == page)
        while self.wizard_slot.count():
            item = self.wizard_slot.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().setParent(None)
        is_languages = self._active_section == "Einstellungen" and page == "Allgemein"
        is_global_settings = self._active_section == "Einstellungen" and page in (
            "Assistenten", "Video", "Metadaten", "Dateinamen", "Audio", "Grafiken", "Tools")
        is_wizard = (self._active_section == "Assistenten" and page in ("Serien-Aufarbeitung", "Spielfilme-Aufarbeitung", "Musikvideo-Aufarbeitung", "Video-Aufarbeitung")) or (self._active_section == "Bearbeitung" and page in ("Recode", "Remux", "Audio-Normalisieren", "Videos zusammenfügen"))
        self.heading.setVisible(not is_wizard and not is_languages and not is_global_settings and not (self._active_section == "Metadaten" and page in ("Serien", "Spielfilme", "Musikvideos", "Sonstige Videos")))
        self.description.setVisible(not is_wizard and not is_languages and not is_global_settings and not (self._active_section == "Metadaten" and page in ("Serien", "Spielfilme", "Musikvideos", "Sonstige Videos")))
        if self._active_section == "Metadaten" and page == "Sonstige Videos":
            # Separate cache key: metadata editor and video wizard are different pages.
            metadata_key = "Metadaten/Sonstige Videos"
            if metadata_key not in self.wizard_pages:
                self.wizard_pages[metadata_key] = OtherVideoPage()
            self.wizard_slot.addWidget(self.wizard_pages[metadata_key])
        if self._active_section == "Metadaten" and page in ("Serien", "Spielfilme", "Musikvideos"):
            metadata_key = f"Metadaten/{page}"
            if metadata_key not in self.wizard_pages:
                self.wizard_pages[metadata_key] = MetadataPage(page)
            self.wizard_slot.addWidget(self.wizard_pages[metadata_key])
        if self._active_section == "Nacharbeiten" and page == "Spielfilme":
            key = "Nacharbeiten/Spielfilme"
            if key not in self.wizard_pages:
                self.wizard_pages[key] = MoviePostprocess()
            self.wizard_slot.addWidget(self.wizard_pages[key])
        if self._active_section == "Nacharbeiten" and page == "Musikvideos":
            key = "Nacharbeiten/Musikvideos"
            if key not in self.wizard_pages:
                self.wizard_pages[key] = MusicvideoPostprocess()
            self.wizard_slot.addWidget(self.wizard_pages[key])
        if self._active_section == "Nacharbeiten" and page == "Sonstige Videos":
            key = "Nacharbeiten/Sonstige Videos"
            if key not in self.wizard_pages:
                self.wizard_pages[key] = OtherVideoPostprocess()
            self.wizard_slot.addWidget(self.wizard_pages[key])
        if self._active_section == "Nacharbeiten" and page == "Serien":
            key = "Nacharbeiten/Serien"
            if key not in self.wizard_pages:
                self.wizard_pages[key] = SeriesPostprocess()
            self.wizard_slot.addWidget(self.wizard_pages[key])
        if self._active_section == "Nacharbeiten" and page == "Videos-Test":
            key = "Nacharbeiten/Videos-Test"
            if key not in self.wizard_pages:
                self.wizard_pages[key] = VideoTestPage()
            self.wizard_slot.addWidget(self.wizard_pages[key])
        if is_wizard:
            kind = page if page in ("Recode", "Remux", "Audio-Normalisieren", "Videos zusammenfügen") else "Serien" if page == "Serien-Aufarbeitung" else "Musikvideos" if page == "Musikvideo-Aufarbeitung" else "Sonstige Videos" if page == "Video-Aufarbeitung" else "Spielfilme"
            if kind not in self.wizard_pages:
                self.wizard_pages[kind] = RecodePage() if kind == "Recode" else RemuxPage() if kind == "Remux" else AudioNormalizePage() if kind == "Audio-Normalisieren" else AppendPage() if kind == "Videos zusammenfügen" else WizardPage(kind)
            self.wizard_slot.addWidget(self.wizard_pages[kind])
        if is_languages:
            if self.language_page is None:
                self.language_page = LanguageSettingsPage(self)
            self.wizard_slot.addWidget(self.language_page)
        if is_global_settings:
            if self.global_settings_page is None:
                self.global_settings_page = GlobalSettingsPage(self)
            index = {"Assistenten": 0, "Video": 1, "Metadaten": 2,
                     "Dateinamen": 3, "Audio": 4, "Grafiken": 6, "Tools": 5}[page]
            self.global_settings_page.pages.setCurrentIndex(index)
            self.wizard_slot.addWidget(self.global_settings_page)
        # Cached pages are hidden and detached during navigation.  After
        # reparenting, explicitly show EVERY selected page (not just editors).
        # Otherwise wizard, processing and metadata pages stay invisible on
        # their second visit.  Never show a detached widget as a top-level window.
        for index in range(self.wizard_slot.count()):
            widget = self.wizard_slot.itemAt(index).widget()
            if widget is not None:
                widget.show()
        if self._active_section == "Nacharbeiten" and page in ("Serien", "Spielfilme", "Musikvideos", "Sonstige Videos", "Videos-Test"):
            self.heading.setVisible(False)
            self.description.setVisible(False)
        self.heading.setText(f"{navigation_label(self._active_section)}: {navigation_label(page)}")
        if self._active_section == "Nacharbeiten" and page == "Originale löschen":
            self.description.setText(_('The originals from the last run can be deleted after confirmation via File → Delete originals.'))
        else:
            self.description.setText(_('The main window and navigation are ready.\nWizards and other functions will be added step by step.'))


    @staticmethod
    def _time_text(seconds):
        try:
            if seconds is None or float(seconds) < 0:
                return "--:--:--"
            n = int(float(seconds))
            return f"{n // 3600:02}:{(n // 60) % 60:02}:{n % 60:02}"
        except (TypeError, ValueError, OverflowError):
            return "--:--:--"

    def set_work_status(self, kind, value=None, index=0, total=0):
        """Receives only GUI-thread worker signals; never estimates a batch ETA."""
        if kind == "file":
            self._file_started = time.monotonic()
            self._last_progress = {}
            self._status_text = _('Video {index}/{total}: {filename}').format(index=index, total=total, filename=value)
        elif kind == "progress" and isinstance(value, dict):
            self._last_progress = value
        elif kind == "phase":
            self._file_started = None
            self._last_progress = {}
            self._status_text = stage_label(str(value))
        elif kind == "log":
            if self._file_started is None:
                self._status_text = str(value)[:160]
        elif kind == "done":
            self._file_started = None
            self._last_progress = {}
            self._status_text = _('Idle')
        self._refresh_status()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_status_left"):
            self._refresh_status()

    def _refresh_status(self):
        self._status_clock.setText(datetime.now().strftime("%H:%M:%S"))
        message = self._status_text
        if self._file_started is not None:
            elapsed = time.monotonic() - self._file_started
            data = self._last_progress
            remaining = data.get("remaining_seconds")
            # Gesamt ist die geschätzte Verarbeitungszeit dieser Datei,
            # nicht die Mediendauer und niemals die Batchdauer.
            estimated_total = elapsed + float(remaining) if remaining is not None else None
            pct = data.get("percent")
            percent_text = f"{float(pct):.1f} %" if pct is not None else "-- %"
            fps = data.get("fps")
            fps_text = f"{fps} FPS" if fps not in (None, "", "N/A") else "-- FPS"
            message += (f"  |  {percent_text}  |  {_('Elapsed:')} {self._time_text(elapsed)}"
                        f"  |  {_('Total:')} {self._time_text(estimated_total)}  |  {fps_text}")
        # Qt elides long text; clock remains visible even at V1 window size.
        metrics = self._status_left.fontMetrics()
        self._status_left.setText(metrics.elidedText(
            message, Qt.TextElideMode.ElideLeft, max(40, self._status_left.width() - 8)))

    def _delete_originals_info(self):
        from original_cleanup import load, cleanup
        try:
            entries = load()['files']
        except Exception as exc:
            QMessageBox.critical(self, _('Delete originals'), _('Could not read the deletion list:\n{exc}').format(exc=exc))
            return
        if not entries:
            QMessageBox.information(self, _('Delete originals'), _('No original files from the last run are marked for deletion.'))
            return
        answer = QMessageBox.warning(
            self, _('Permanently delete original files'),
            _('Permanently delete {value} original file(s) from the last run?\n\nThe files will not be moved to the Recycle Bin.\nCheck all processing results first.\n\nThis action cannot be undone.').format(value=len(entries)),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            activity_event("Originale löschen: abgebrochen")
            return
        activity_event(f"Originale löschen: bestätigt ({len(entries)} Dateien)")
        try:
            result = cleanup()
        except Exception as exc:
            QMessageBox.critical(self, _('Delete originals'), _('Deletion failed:\n{exc}').format(exc=exc))
            return
        activity_event("Originale löschen: abgeschlossen; gelöscht={}; fehlend={}; Fehler={}".format(result["deleted"], result["missing"], len(result["errors"])))
        summary = _('Deleted: {deleted}\nAlready removed: {missing}\nNot deleted: {errors}').format(deleted=result['deleted'], missing=result['missing'], errors=len(result['errors']))
        if result['errors']:
            summary += '\n\n' + _('Remove manually:') + '\n' + '\n'.join(
                _('{path}\n  Reason: {reason}').format(path=path, reason=reason) for path, reason in result['errors'])
            QMessageBox.warning(self, _('Delete originals – result'), summary)
        else:
            QMessageBox.information(self, _('Delete originals – result'), summary)

    def _about(self):
        """Localized credits with official external links (no development test labels)."""
        from html import escape
        dialog = QDialog(self)
        dialog.setWindowTitle(_('About Media-Buddy V2'))
        dialog.setMinimumSize(590, 475)
        dialog.resize(660, 555)
        layout = QVBoxLayout(dialog)
        browser = QTextBrowser(dialog)
        browser.setOpenExternalLinks(True)
        browser.setReadOnly(True)
        browser.setStyleSheet("QTextBrowser { background: white; border: 1px solid #dbe3eb; padding: 12px; }")
        def link(name, url):
            return f'<a href="{escape(url, quote=True)}">{escape(name)}</a>'
        description = (
            _('Media-Buddy V2 organizes, converts and prepares movies, TV series, music videos and other videos for media libraries such as Kodi.')
        )
        source_title = _('Online metadata and artwork sources')
        tools_title = _('External tools')
        developer = _('Developed by')
        sources = [
            ("TheTVDB", "https://thetvdb.com/"),
            ("The Movie Database (TMDB)", "https://www.themoviedb.org/"),
            ("Fanart.tv", "https://fanart.tv/"),
            ("MusicBrainz", "https://musicbrainz.org/"),
            ("TheAudioDB", "https://www.theaudiodb.com/"),
            ("Last.fm", "https://www.last.fm/"),
            ("ChaptersDB", "https://chaptersdb.org/"),
        ]
        tools = [
            ("FFmpeg", "https://ffmpeg.org/"),
            ("MKVToolNix", "https://mkvtoolnix.download/"),
            ("ImageMagick", "https://imagemagick.org/"),
            ("yt-dlp", "https://github.com/yt-dlp/yt-dlp"),
            ("mpv", "https://mpv.io/"),
        ]
        tmdb_note = (
            _('This product uses the TMDB API but is not endorsed or certified by TMDB.')
        )
        html = (
            '<div style="font-family:Segoe UI,Arial,sans-serif;color:#273345;font-size:13px;">'
            '<h2 style="margin-top:0;color:#193b5a;">Media-Buddy V2</h2>'
            f'<p>{escape(description)}</p>'
            f'<p><b>{escape(developer)}:</b> {link("Lehmden", "https://buddytools.de/media-buddy-v2/")}</p>'
            f'<h3 style="color:#193b5a;">{escape(source_title)}</h3>'
            '<p>' + ' &nbsp;·&nbsp; '.join(link(n, u) for n, u in sources) + '</p>'
            f'<p style="font-size:11px;color:#536475;">{escape(tmdb_note)}</p>'
            f'<h3 style="color:#193b5a;">{escape(tools_title)}</h3>'
            '<p>' + ' &nbsp;·&nbsp; '.join(link(n, u) for n, u in tools) + '</p>'
            '</div>'
        )
        browser.setHtml(html)
        layout.addWidget(browser)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText(_('Close'))
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()


    def request_restart(self):
        # closeEvent refuses closure during processing and persists window/wizard state.
        self._restart_requested = False
        if self.close():
            self._restart_requested = True
            QApplication.instance().quit()

    def closeEvent(self, event):
        if any(getattr(page, '_running', False) for page in self.wizard_pages.values()):
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, _('Processing in progress'),
                _('Media-Buddy cannot be closed during processing. Please request cancellation after the current file first.'))
            event.ignore()
            return
        editor = self.wizard_pages.get("Nacharbeiten/Serien")
        if editor is not None and not editor.save_current():
            event.ignore()
            return
        other_editor = self.wizard_pages.get("Nacharbeiten/Sonstige Videos")
        if other_editor is not None and not other_editor.save_current():
            event.ignore()
            return
        movie_editor = self.wizard_pages.get("Nacharbeiten/Spielfilme")
        if movie_editor is not None and not movie_editor.save_current():
            event.ignore()
            return
        # Letzte vom Benutzer gewählte Position und Größe dauerhaft speichern.
        settings = QSettings("BuddyTools", "Media-Buddy-V2")
        settings.setValue("gui/window_state", self.saveGeometry())
        settings.setValue("gui/window_maximized", self.isMaximized())
        if not self.isMaximized() and not self.isMinimized():
            settings.setValue("gui/window_geometry", self.geometry())  # compatibility
        for page in self.wizard_pages.values():
            if hasattr(page, "save"):
                page.save(silent=True)
        try:
            from batch_settings import write_settings
            path = ROOT / "wizard_gui_settings.json"
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            data["LastMainTab"] = self._active_section
            data["LastSubTab"] = self._active_page
            write_settings(path, data)
        except (OSError, ValueError, TypeError) as exc:
            QMessageBox.warning(self, _('Save failed'), str(exc))
        activity_event("Hauptfenster geschlossen")
        super().closeEvent(event)


STYLE = """
QMainWindow, QWidget { background: #f6f8fb; color: #273345; font-family: 'Segoe UI', Arial; font-size: 12px; }
QMenuBar { background: #ffffff; border-bottom: 1px solid #dbe3eb; padding: 0px; }
QMenuBar::item { padding: 2px 7px; }
QMenuBar::item:selected, QMenu::item:selected { background: #e5f2ff; }
QMenu { background: white; }
QMenu::item { padding: 5px 14px 5px 7px; margin: 1px 2px; }
QMenu::separator { height: 1px; background: #dbe3eb; margin: 3px 5px; }
QStatusBar { background: #ffffff; border-top: 1px solid #dbe3eb; color: #617083; }
QFrame#header { border-bottom: 1px solid #164b77; }
QWidget#navigation { background: #ffffff; border-bottom: 1px solid #dbe3eb; }
QWidget#subnavigation { background: #f0f5fb; border-bottom: 1px solid #dbe3eb; }
QPushButton#navButton, QPushButton#subButton { background: transparent; border: 1px solid transparent; border-radius: 3px; padding: 5px 4px; color: #182c40; font-weight: 600; }
QPushButton#navButton:hover, QPushButton#subButton:hover { background: #e6f2ff; }
QPushButton#navButton:checked { background: #dceeff; border: 1px solid #a8d1f8; color: #075b9c; font-weight: 600; }
QPushButton#subButton:checked { background: #ffffff; border: 1px solid #b8d9f5; color: #075b9c; font-weight: 600; }
QFrame#content { background: #ffffff; }
QLabel#pageTitle { font-size: 19px; font-weight: 700; color: #213a56; }
QLabel#wizardTitle { font-size: 19px; font-weight: 700; color: #213a56; }
QGroupBox { border: 1px solid #dbe3eb; border-radius: 6px; margin-top: 12px; padding: 10px; font-weight: 600; background: #fafcff; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 4px; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox { background: white; border: 1px solid #cbd8e5; border-radius: 4px; padding: 5px; }
QLabel#description { font-size: 13px; color: #64748b; }
"""


def _place_window(window, app):
    """Fenster mit Sicherheitsabstand vollständig auf einem verfügbaren Monitor halten."""
    settings = QSettings("BuddyTools", "Media-Buddy-V2")
    stored_state = settings.value("gui/window_state", None)
    restored = bool(stored_state and window.restoreGeometry(stored_state))
    saved = window.geometry() if restored else settings.value("gui/window_geometry", None)
    screens = app.screens()
    if not screens:
        return
    screen = window.screen() or app.primaryScreen() or screens[0]
    if isinstance(saved, QRect):
        # Monitor bestimmen, auf dem die letzte Fensterposition lag.
        for candidate in screens:
            if candidate.geometry().contains(saved.center()):
                screen = candidate
                break
        else:
            # Monitor entfernt: auf den Hauptbildschirm zurückfallen.
            screen = app.primaryScreen() or screens[0]
    area = screen.availableGeometry()
    # 30 px oben und 40 px seitlich; auch unten eine kleine Reserve.
    left_margin, top_margin, right_margin, bottom_margin = 40, 30, 40, 30
    allowed_width = max(1, area.width() - left_margin - right_margin)
    allowed_height = max(1, area.height() - top_margin - bottom_margin)
    if isinstance(saved, QRect):
        desired_width, desired_height = saved.width(), saved.height()
    else:
        desired_width, desired_height = 1250, 850
    # Die tatsächlichen Dekorationen erst nach show() berücksichtigen.
    frame = window.frameGeometry()
    border_width = max(0, frame.width() - window.width())
    border_height = max(0, frame.height() - window.height())
    window.resize(
        max(window.minimumWidth(), min(desired_width, allowed_width - border_width)),
        max(window.minimumHeight(), min(desired_height, allowed_height - border_height)),
    )
    frame = window.frameGeometry()
    min_x = area.left() + left_margin
    min_y = area.top() + top_margin
    max_x = max(min_x, area.right() + 1 - right_margin - frame.width())
    max_y = max(min_y, area.bottom() + 1 - bottom_margin - frame.height())
    if isinstance(saved, QRect):
        target_x = min(max(saved.x() - (window.x() - frame.x()), min_x), max_x)
        target_y = min(max(saved.y() - (window.y() - frame.y()), min_y), max_y)
    else:
        target_x, target_y = min_x, min_y
    # QWidget.move() setzt die Clientposition, nicht die äußere Rahmenposition.
    window.move(window.x() + target_x - frame.x(), window.y() + target_y - frame.y())


def _open_json_in_editor(path):
    """Open in a text editor, never execute the JSON file."""
    import shutil
    import subprocess
    import sys
    path = str(Path(path).resolve())
    if sys.platform == 'win32':
        candidates = [['notepad.exe', path]]
    elif sys.platform == 'darwin':
        candidates = [['open', '-a', 'TextEdit', path]]
    else:
        candidates = [[exe, path] for exe in
                      ('xdg-open', 'mousepad', 'gedit', 'kate', 'xed', 'pluma', 'leafpad')]
    for command in candidates:
        if not shutil.which(command[0]):
            continue
        try:
            subprocess.Popen(command, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            return True
        except OSError:
            continue
    return False


def _configuration_repair_dialog(error):
    """Never replace malformed files. Allow editing and revalidation."""
    from configuration_defaults import DEFAULTS
    from configuration_store import ConfigurationError
    # Select the actual damaged file, not just the first file in the list.
    def check_all():
        for name in DEFAULTS:
            path = ROOT / name
            if path.exists():
                try:
                    load_raw(path)
                except (ConfigurationError, OSError) as exc:
                    return path, exc
        return None, None
    path, problem = check_all()
    if path is None:
        QMessageBox.critical(None, _('Configuration error'), str(error))
        return False
    while True:
        dialog = QMessageBox()
        dialog.setIcon(QMessageBox.Icon.Critical)
        dialog.setWindowTitle(_('Configuration error'))
        dialog.setText(_('The JSON configuration file is damaged.'))
        dialog.setInformativeText(str(path) + '\n\n' + str(problem) +
            '\n\n' + _('The file will not be overwritten automatically.'))
        edit = dialog.addButton(_('Open in text editor'), QMessageBox.ButtonRole.ActionRole)
        retry = dialog.addButton(_('Check again'), QMessageBox.ButtonRole.AcceptRole)
        close = dialog.addButton(_('Exit'), QMessageBox.ButtonRole.RejectRole)
        dialog.setDefaultButton(edit)
        dialog.exec()
        if dialog.clickedButton() is edit:
            if not _open_json_in_editor(path):
                QMessageBox.warning(None, _('Editor not found'),
                    _('Could not open a text editor. File:') + '\n' + str(path))
        elif dialog.clickedButton() is retry:
            path, problem = check_all()
            if path is None:
                return True
        else:
            return False


def main():
    from log_archive import rotate
    from activity_log import start as start_activity, stop as stop_activity
    errors, can_start = rotate(ROOT, retention_months=6)
    if can_start:
        try:
            start_activity(ROOT)
            for error in errors:
                activity_event(f'Logarchivierung: {error}', 'WARN')
        except OSError as exc:
            print(f'[WARN] Allgemeines Log nicht verfügbar: {exc}', file=sys.stderr)
    else:
        print('[WARN] Vorheriges allgemeines Log konnte nicht archiviert werden.', file=sys.stderr)
    app = QApplication(sys.argv)
    app.aboutToQuit.connect(stop_activity)
    app.setApplicationName("Media-Buddy V2")
    try:
        created = ensure_configuration(ROOT)
    except SettingsError as exc:
        if not _configuration_repair_dialog(exc):
            return 1
        try:
            created = ensure_configuration(ROOT)
        except SettingsError as second:
            QMessageBox.critical(None, _('Configuration error'), str(second))
            return 1
    if created:
        message = (_('Setting up Media-Buddy V2.\nMissing configuration files were created with default settings.\nExisting settings are preserved.\n\n')
                   + '\n'.join(created))
        activity_event(f"Konfiguration: neu erstellt: {', '.join(created)}")
        dialog = QMessageBox(QMessageBox.Icon.Information,
                             _('Setting up Media-Buddy V2'), message,
                             QMessageBox.StandardButton.Ok)
        QTimer.singleShot(5000, dialog.accept)
        dialog.exec()
    window = MainWindow()
    window.show()
    activity_event("Hauptfenster bereit")
    _place_window(window, app)
    settings = QSettings("BuddyTools", "Media-Buddy-V2")
    if settings.value("gui/window_maximized", False, type=bool):
        window.showMaximized()
    result=app.exec()
    if getattr(window,'_restart_requested',False):
        # Start the successor only after the Qt event loop has stopped.
        args=[str(Path(__file__).resolve()), *sys.argv[1:]]
        ok, _pid=QProcess.startDetached(sys.executable,args,str(ROOT))
        if not ok:
            print('Media-Buddy: failed to start new process',file=sys.stderr)
            return 1
    return result


if __name__ == "__main__":
    sys.exit(main())
