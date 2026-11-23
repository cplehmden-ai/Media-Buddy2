"""Canonical entry-page chrome for one- and two-folder workflows.

Only standard entry pages use this module. Settings and editing pages are free-form.
"""
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
                               QLabel, QPushButton, QGroupBox, QScrollArea, QFrame)
from gui_i18n import _
from collapsible_settings import CollapsibleSettings

_START_STYLE = ('QPushButton { background:#1769ad; color:white; font-weight:bold; '
                'border-radius:5px; padding:7px 18px; } '
                'QPushButton:hover { background:#0d568f; }')

class StandardEntryPage(QWidget):
    """Identical title, folder box, action row and collapsible options for entry pages."""
    def __init__(self, title, folders, start_callback, save_callback=None, parent=None):
        super().__init__(parent)
        if len(folders) not in (1, 2):
            raise ValueError('Standard entry pages require one or two folders')
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(9, 9, 9, 9)
        self.main_layout.setSpacing(7)
        heading = QLabel(title)
        heading.setObjectName('wizardTitle')
        self.main_layout.addWidget(heading)
        group = QGroupBox(_('Media folder') if len(folders) == 1 else _('Source and destination folders'))
        grid = QGridLayout(group)
        for index, (label, field, browse_callback) in enumerate(folders):
            field.setMinimumHeight(30)
            grid.addWidget(QLabel(label), index, 0)
            grid.addWidget(field, index, 1)
            browse = QPushButton(_('Browse …'))
            browse.clicked.connect(browse_callback)
            grid.addWidget(browse, index, 2)
        grid.setColumnStretch(1, 1)
        grid.setRowMinimumHeight(len(folders), 36)
        self.main_layout.addWidget(group)
        action_panel = QWidget()
        action_layout = QHBoxLayout(action_panel)
        action_layout.addStretch(1)
        self.save_button = None
        if save_callback is not None:
            self.save_button = QPushButton(_('Save settings'))
            self.save_button.clicked.connect(save_callback)
            action_layout.addWidget(self.save_button)
        self.start_button = QPushButton(_('Start'))
        self.start_button.setMinimumHeight(38)
        self.start_button.setMinimumWidth(145)
        self.start_button.setStyleSheet(_START_STYLE)
        self.start_button.clicked.connect(start_callback)
        action_layout.addWidget(self.start_button)
        self.main_layout.addWidget(action_panel)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.options_widget = QWidget()
        self.options_layout = QVBoxLayout(self.options_widget)
        self.options_layout.setContentsMargins(2, 3, 8, 3)
        self.options_layout.setSpacing(9)
        self.options_layout.addStretch(1)
        self.scroll.setWidget(self.options_widget)
        self.main_layout.addWidget(self.scroll, 1)

    def add_group(self, title):
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(12, 10, 12, 10)
        self.options_layout.insertWidget(self.options_layout.count() - 1,
                                         CollapsibleSettings(title, content, False))
        return layout

    def add_option_widget(self, widget):
        self.options_layout.insertWidget(self.options_layout.count() - 1, widget)


class SingleFolderEntryPage(StandardEntryPage):
    def __init__(self, title, label, field, browse_callback, start_callback,
                 save_callback=None, parent=None):
        super().__init__(title, [(label, field, browse_callback)], start_callback,
                         save_callback, parent)


class DualFolderEntryPage(StandardEntryPage):
    def __init__(self, title, source_label, source_field, source_browse,
                 target_label, target_field, target_browse, start_callback,
                 save_callback=None, parent=None):
        super().__init__(title, [(source_label, source_field, source_browse),
                                 (target_label, target_field, target_browse)],
                         start_callback, save_callback, parent)
