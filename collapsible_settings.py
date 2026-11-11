from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget,QVBoxLayout,QToolButton,QSizePolicy

class CollapsibleSettings(QWidget):
    """Reusable assistant-style collapsible settings group."""
    def __init__(self,title,content,opened=False,parent=None):
        super().__init__(parent)
        layout=QVBoxLayout(self);layout.setContentsMargins(0,0,0,0);layout.setSpacing(0)
        self.toggle=QToolButton()
        self.toggle.setText(title)
        self.toggle.setCheckable(True)
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Fixed)
        self.toggle.setStyleSheet('QToolButton { text-align:left; padding:11px 12px; border:1px solid #d6dfe8; background:#eef4fa; border-radius:5px; font-weight:600; } QToolButton:hover { background:#e1edf8; }')
        layout.addWidget(self.toggle)
        self.content=content;layout.addWidget(content)
        self.toggle.toggled.connect(self.set_open)
        self.toggle.setChecked(opened)
        self.set_open(opened)
    def set_open(self,opened):
        self.content.setVisible(opened)
        self.toggle.setArrowType(Qt.ArrowType.DownArrow if opened else Qt.ArrowType.RightArrow)

