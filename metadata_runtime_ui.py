"""Shared metadata GUI runtime controls. No scraper or NFO logic lives here."""
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QProgressBar, QMessageBox
from system_actions import available_actions, perform_action
from wizard_page import ContrastProgressBar
from activity_log import event as activity_event
from gui_i18n import _, navigation_label, stage_label

ACTION_LABELS = {'none':_('Do nothing'), 'postprocess':_('Open post-processing'),
                 'exit':_('Exit program'), 'standby':_('Standby'),
                 'hibernate':_('Hibernate'), 'shutdown':_('Shut down')}

def _style_progress(bar):
    """Use exactly the same progress bar appearance as the wizard pages."""
    bar.setFixedHeight(27)
    bar.setAlignment(Qt.AlignmentFlag.AlignCenter)
    bar.setFormat('%p %')
    bar.setStyleSheet(
        'QProgressBar {border:2px solid #8196a8; border-radius:7px; '
        'background:#e2eaf1; text-align:center; font-size:13px; font-weight:bold;} '
        'QProgressBar::chunk {background:#1774b8; border-radius:5px;}'
    )

def install(page, root):
    """Separate wizard-style running view; original settings are untouched."""
    from PySide6.QtWidgets import QWidget, QVBoxLayout, QPushButton
    page._metadata_original_widgets = [root.itemAt(i).widget() for i in range(root.count()) if root.itemAt(i).widget()]
    page._runtime_panel = QWidget(page)
    panel = QVBoxLayout(page._runtime_panel)
    panel.setContentsMargins(14, 16, 14, 12)
    panel.setSpacing(10)
    title = QLabel(_('Processing metadata: {kind}').format(kind=navigation_label(page.kind)))
    title.setStyleSheet('font-size:19px; font-weight:bold;')
    panel.addWidget(title)
    page.runtime_stage = QLabel(_('Starting scraper…'))
    panel.addWidget(page.runtime_stage)
    panel.addWidget(QLabel(_('Overall progress')))
    page.batch_progress = ContrastProgressBar()
    _style_progress(page.batch_progress)
    page.batch_progress.setRange(0, 0)
    panel.addWidget(page.batch_progress)
    panel.addWidget(QLabel(_('Current file')))
    page.file_progress = ContrastProgressBar()
    _style_progress(page.file_progress)
    page.file_progress.setRange(0, 0)
    page.file_progress.setFormat(_('Processing …'))
    panel.addWidget(page.file_progress)
    buttons = QHBoxLayout()
    page.runtime_stop = QPushButton(_('Cancel after current file'))
    page.runtime_stop.clicked.connect(page.request_stop)
    buttons.addWidget(page.runtime_stop)
    page.runtime_stop_now = QPushButton(_('Cancel immediately'))
    page.runtime_stop_now.clicked.connect(page.request_immediate_stop)
    buttons.addWidget(page.runtime_stop_now)
    panel.addLayout(buttons)
    actions = QHBoxLayout()
    actions.addWidget(QLabel(_('After completion:')))
    page.end_action = QComboBox()
    for action in ['none', 'postprocess'] + [a for a in available_actions() if a != 'none']:
        page.end_action.addItem(ACTION_LABELS[action], action)
    actions.addWidget(page.end_action, 1)
    panel.addLayout(actions)
    panel.addStretch(1)
    root.addWidget(page._runtime_panel, 1)
    page._runtime_panel.hide()
    page._elapsed_seconds = 0
    page._timer = QTimer(page)
    page._timer.setInterval(1000)
    page._timer.timeout.connect(lambda: _tick(page))
    page._run_folder = None
    page._run_stopped = False
    page._metadata_stage_text = _('Starting scraper…')

def _tick(page):
    page._elapsed_seconds += 1
    _update_status(page)

def _update_status(page):
    """Metadata-specific status line; no changes to wizard/FFmpeg status."""
    elapsed = f'{page._elapsed_seconds // 60:02d}:{page._elapsed_seconds % 60:02d}'
    stage = getattr(page, '_metadata_stage_text', '')
    message = f'{stage}  |  {_('Elapsed:')} {elapsed} min' if stage else f'{_('Elapsed:')} {elapsed} min'
    # The hidden page.status QLabel is not part of the main-window status bar.
    # Update the actual status bar model, which the main window redraws each second.
    host = page.window()
    if hasattr(host, '_status_text') and hasattr(host, '_refresh_status'):
        host._status_text = message
        host._refresh_status()

def started(page, folder):
    page._run_folder = folder
    page._run_stopped = False
    page._elapsed_seconds = 0
    page.end_action.setCurrentIndex(0)
    for widget in page._metadata_original_widgets:
        widget.hide()
    page._runtime_panel.show()
    page.runtime_stop.setEnabled(True)
    page.runtime_stop_now.setEnabled(True)
    page._metadata_stage_text = _('Starting scraper…')
    page.runtime_stage.setText(page._metadata_stage_text)
    _update_status(page)
    page.batch_progress.setRange(0, 0)
    page.file_progress.setRange(0, 0)
    page._timer.start()

def update(page, event):
    index, total = event.get('index'), event.get('total')
    if isinstance(index, int) and isinstance(total, int) and total > 0:
        page.batch_progress.setRange(0, total)
        page.batch_progress.setValue(max(0, min(total, index-1)))
        page.batch_progress.setFormat(_('File/folder {index} of {total} (%p%)').format(index=index, total=total))
    pct = event.get('percent')
    if isinstance(pct, (int, float)):
        page.file_progress.setRange(0, 100)
        page.file_progress.setValue(max(0, min(100, round(pct))))
        page.file_progress.setFormat('%p%')
    else:
        page.file_progress.setRange(0, 0)
    name = event.get('file') or ''
    stage = event.get('stage') or ''
    stage = stage_label(stage)
    page.runtime_stage.setText(f'{name} – {stage}' if name else stage)
    page._metadata_stage_text = stage
    _update_status(page)

def finished(page, success, stopped):
    page._timer.stop()
    host = page.window()
    if hasattr(host, '_status_text') and hasattr(host, '_refresh_status'):
        host._status_text = _('Idle')
        host._refresh_status()
    action = page.end_action.currentData() if success and not stopped else 'none'
    page.end_action.setCurrentIndex(0)
    page._runtime_panel.hide()
    for widget in page._metadata_original_widgets:
        widget.show()
    if action == 'none': return
    if action == 'exit':
        page.window().close()
    elif action == 'postprocess':
        host = page.window()
        folder = page._run_folder
        method = {'Serien':'open_series_postprocess_for_folder',
                  'Spielfilme':'open_movie_postprocess_for_folder',
                  'Sonstige Videos':'open_other_video_postprocess_for_folder'}.get(page.kind)
        if method and hasattr(host, method):
            getattr(host, method)(folder)
        elif page.kind == 'Musikvideos':
            host.select_section('Nacharbeiten', initial_page='Musikvideos')
            editor = host.wizard_pages.get('Nacharbeiten/Musikvideos')
            if editor is not None: editor.open_folder(folder_override=folder)
    elif action in ('standby', 'hibernate', 'shutdown'):
        _power_countdown(page, action)

def _power_countdown(page, action):
    dialog = QMessageBox(page)
    dialog.setWindowTitle(_('System action after completion'))
    dialog.setIcon(QMessageBox.Icon.Warning)
    dialog.setStandardButtons(QMessageBox.StandardButton.Cancel)
    dialog.button(QMessageBox.StandardButton.Cancel).setText(_('Cancel action'))
    dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    remaining = [60]
    timer = QTimer(dialog)
    timer.setInterval(1000)
    def tick():
        remaining[0] -= 1
        if remaining[0] <= 0:
            timer.stop()
            dialog.accept()
            try:
                activity_event(f'{page.kind}: Endaktion {action}')
                perform_action(action)
            except Exception as exc:
                QMessageBox.critical(page,_('System action failed'),str(exc))
        else:
            dialog.setText(_('{action} in {seconds} seconds.\nSelect “Cancel action” to keep the computer running.').format(action=ACTION_LABELS[action],seconds=remaining[0]))
    dialog.setText(_('{action} in {seconds} seconds.\nSelect “Cancel action” to keep the computer running.').format(action=ACTION_LABELS[action],seconds=60))
    timer.timeout.connect(tick)
    timer.start()
    dialog.open()
