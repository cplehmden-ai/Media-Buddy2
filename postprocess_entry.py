"""Shared, localized entry layout for all four post-processing modules."""
from PySide6.QtWidgets import QWidget,QVBoxLayout,QHBoxLayout,QGridLayout,QLabel,QPushButton,QGroupBox
from gui_i18n import _

def make_entry(folder_edit, browse_callback, start_callback, title, folder_label):
    entry=QWidget()
    layout=QVBoxLayout(entry)
    layout.setContentsMargins(9,9,9,9)
    layout.setSpacing(5)
    heading=QLabel(title);heading.setObjectName('wizardTitle')
    layout.addWidget(heading)
    group=QGroupBox(_('Media folder'))
    grid=QGridLayout(group)
    folder_edit.setMinimumHeight(30)
    grid.addWidget(QLabel(folder_label),0,0)
    grid.addWidget(folder_edit,0,1)
    browse=QPushButton(_('Browse …'))
    browse.clicked.connect(browse_callback)
    grid.addWidget(browse,0,2)
    grid.setRowMinimumHeight(1,36)
    layout.addWidget(group)
    actions=QHBoxLayout()
    actions.addStretch(1)
    start=QPushButton(_('Start'))
    start.setMinimumHeight(38)
    start.setMinimumWidth(145)
    start.setStyleSheet('QPushButton { background:#1769ad; color:white; font-weight:bold; border-radius:5px; padding:7px 18px; } QPushButton:hover { background:#0d568f; }')
    start.clicked.connect(start_callback)
    actions.addWidget(start)
    layout.addLayout(actions)
    layout.addStretch(1)
    return entry
