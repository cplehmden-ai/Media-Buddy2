"""Shared, log-free progress panel for future standard processing pages."""
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QHBoxLayout, QPushButton, QComboBox, QProgressBar
from wizard_page import ContrastProgressBar
from metadata_runtime_ui import _style_progress
from gui_i18n import _

class StandardProgressPage(QWidget):
    def __init__(self, title, actions, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 16, 14, 12)
        layout.setSpacing(12)
        heading = QLabel(title)
        heading.setStyleSheet('font-size:19px; font-weight:bold; color:#203a53;')
        layout.addWidget(heading)
        self.stage = QLabel(_('Starting scraper…'))
        self.stage.setWordWrap(True)
        layout.addWidget(self.stage)
        layout.addWidget(QLabel(_('Overall progress')))
        self.progress = ContrastProgressBar()
        _style_progress(self.progress)
        layout.addWidget(self.progress)
        layout.addWidget(QLabel(_('Current file')) )
        self.current_progress = QProgressBar()
        self.current_progress.setRange(0, 0)  # Indeterminate: scraper steps have no reliable percentage.
        _style_progress(self.current_progress)
        self.current_progress.setTextVisible(False)
        self.current_progress.setMinimumHeight(24)
        layout.addWidget(self.current_progress)
        self.elapsed = QLabel(_('Elapsed: {time}').format(time='00:00'))
        self.eta = QLabel(_('Estimated remaining: {time}').format(time='--:--'))
        stops = QHBoxLayout()
        self.stop_btn = QPushButton(_('Cancel after current album'))
        self.stop_now_btn = QPushButton(_('Cancel immediately'))
        stops.addWidget(self.stop_btn)
        stops.addWidget(self.stop_now_btn)
        layout.addLayout(stops)
        row = QHBoxLayout()
        row.addWidget(QLabel(_('After completion:')))
        self.end_action = QComboBox()
        for code, label in actions:
            self.end_action.addItem(label, code)
        row.addWidget(self.end_action, 1)
        layout.addLayout(row)
        layout.addStretch(1)
