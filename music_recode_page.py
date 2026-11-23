"""Music encoding GUI, using the same layout/progress/end-actions as video processing."""
from PySide6.QtWidgets import QWidget, QFormLayout, QLabel, QComboBox, QDoubleSpinBox
from PySide6.QtCore import QSettings
from gui_i18n import _
from wizard_page import WizardPage, CollapsibleSection

class MusicRecodePage(WizardPage):
    def __init__(self, parent=None):
        super().__init__('Musik kodieren', parent)
        self.title_label.setText(_('Processing: Encode music'))
        self.processing_heading.setText(_('Music encoding in progress'))
        self.settings = QSettings('BuddyTools', 'Media-Buddy-V2')
        for section in self.sections:
            section.hide()
        options = QWidget()
        form = QFormLayout(options)
        self.music_codec = QComboBox()
        for name, label in [('mp3', 'MP3'), ('flac', 'FLAC'), ('opus', 'Opus'),
                            ('aac', 'AAC'), ('vorbis', 'Ogg Vorbis'),
                            ('wav16', 'WAV PCM 16-bit'), ('wav24', 'WAV PCM 24-bit')]:
            self.music_codec.addItem(label, name)
        self.music_quality = QComboBox()
        for label in (_('Low'), _('Medium'), _('High'), _('Very high'), _('Maximum')):
            self.music_quality.addItem(label)
        self.music_gain = QDoubleSpinBox()
        self.music_gain.setRange(-60, 30)
        self.music_gain.setDecimals(1)
        self.music_gain.setSingleStep(0.5)
        self.music_gain.setSuffix(' dB')
        form.addRow(_('Output format'), self.music_codec)
        form.addRow(_('Quality level'), self.music_quality)
        form.addRow(_('Constant gain (optional)'), self.music_gain)
        form.addRow(QLabel(_('Sampling rate and channel count are preserved by default.')))
        section = CollapsibleSection(_('Encoding settings'), options)
        self.settings_scroll.widget().layout().insertWidget(0, section)
        self.music_codec.currentIndexChanged.connect(self.update_codec)
        self._restore_music()
        self.update_codec()

    def _restore_music(self):
        self.source.setText(str(self.settings.value('music_recode/source', '')))
        self.target.setText(str(self.settings.value('music_recode/target', '')))
        index = self.music_codec.findData(self.settings.value('music_recode/codec', 'mp3'))
        self.music_codec.setCurrentIndex(max(0, index))
        self.music_quality.setCurrentIndex(max(0, min(4, int(self.settings.value('music_recode/quality', 4)) - 1)))
        self.music_gain.setValue(float(self.settings.value('music_recode/gain', 0)))

    def update_codec(self):
        self.music_quality.setEnabled(self.music_codec.currentData() not in ('wav16', 'wav24'))

    def collect(self):
        return {'source': self.source.text().strip(), 'target': self.target.text().strip(),
                'music_codec': self.music_codec.currentData(),
                'music_quality': self.music_quality.currentIndex() + 1,
                'music_gain': self.music_gain.value()}

    def save(self, silent=False):
        data = self.collect()
        for field, value in [('source', data['source']), ('target', data['target']),
                             ('codec', data['music_codec']), ('quality', data['music_quality']),
                             ('gain', data['music_gain'])]:
            self.settings.setValue('music_recode/' + field, value)
        self.settings.sync()
        if not silent:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(self, _('Saved'), _('Settings saved'))
