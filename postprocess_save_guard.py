"""Non-blocking handling for post-processing autosave failures.

Never recreate a media directory that was moved while the editor was open.
A failed autosave must not trap navigation or application shutdown.
"""
from pathlib import Path
from PySide6.QtWidgets import QMessageBox
from gui_i18n import _


def can_autosave(owner, destination, report=None):
    destination = Path(destination)
    if destination.parent.is_dir():
        return True
    message = _('Autosave skipped: folder no longer exists: {folder}').format(folder=destination.parent)
    if report:
        report(message)
    # No modal dialog: a moved/deleted folder is a normal external change.
    return False


def report_save_failure(owner, destination, exc, title, report=None):
    """Show each distinct failure once per editor; never block navigation."""
    message = f'{destination}: {exc}'
    if report:
        report(_('NFO could not be saved: {message}').format(message=message))
    seen = getattr(owner, '_autosave_errors_shown', None)
    if seen is None:
        seen = set()
        owner._autosave_errors_shown = seen
    if message not in seen:
        seen.add(message)
        QMessageBox.warning(owner, title, message)
