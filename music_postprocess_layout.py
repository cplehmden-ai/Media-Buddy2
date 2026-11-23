"""Shared geometry for album and track post-processing views."""
RIGHT_MARGIN = 4
ACTION_COLUMN_SPACING = 8
ACTION_ROW_SPACING = 6
ACTION_RESERVED_HEIGHT = 190

def cover_side(right_widget):
    """Identical cover dimensions for album and track views."""
    width = max(1, right_widget.width() - 2 * RIGHT_MARGIN)
    available = max(1, right_widget.height() - ACTION_RESERVED_HEIGHT)
    return min(width, available)
