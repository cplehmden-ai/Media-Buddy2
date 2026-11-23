"""Two-folder music assistant reusing the standard wizard progress and end actions."""
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QWidget,QFormLayout,QComboBox,QDoubleSpinBox,QCheckBox,QLabel,QMessageBox
from gui_i18n import _
from wizard_page import WizardPage,CollapsibleSection
from music_scraper_page import MusicScraperPage
from configuration_store import effective
from pathlib import Path

CODECS=[('mp3','MP3'),('flac','FLAC'),('opus','Opus'),('aac','AAC'),
        ('vorbis','Ogg Vorbis'),('wav16','WAV PCM 16-bit'),('wav24','WAV PCM 24-bit')]
META=[('lookup','Search MusicBrainz when no release ID is present',True),
      ('tagging','Write MusicBrainz metadata to audio files',False),
      ('lyrics','Download lyrics from LRCLIB',True),
      ('plain','Use plain lyrics when synchronized lyrics are unavailable',True),
      ('embed_lyrics','Embed lyrics in audio tags',False),
      ('clean_lyrics','Clean unsafe HTML and JavaScript from existing lyrics',False),
      ('album_artwork','Download missing album covers and embed in audio files',False),
      ('artist_artwork','Download missing artist artwork to configured locations',False),
      ('music_nfo','Create Kodi artist.nfo and album.nfo',False),
      ('combined_artist_nfo','Create combined artist NFOs for collaborations',False),
      ('rename','Rename audio files',False),
      ('organize','Organize artist and album folders',False),
      ('disc_folders','Create disc and bonus disc folders',False)]

class MusicWizardPage(WizardPage):
    def __init__(self,parent=None):
        super().__init__('Musik-Assistent',parent)
        self.title_label.setText(_('Music assistant'))
        self.settings=QSettings('BuddyTools','Media-Buddy-V2')
        for section in self.sections:section.hide()
        self.source.setText(str(self.settings.value('music_wizard/source','')))
        self.target.setText(str(self.settings.value('music_wizard/target','')))
        self.profiles={}
        for key,title,default in [('lossless',_('HiRes and lossless audio'),'flac'),
                                  ('lossy',_('Lossy audio'),'mp3')]:
            widget=QWidget();form=QFormLayout(widget)
            codec=QComboBox()
            for name,label in CODECS:codec.addItem(label,name)
            codec.setCurrentIndex(max(0,codec.findData(self.settings.value('music_wizard/'+key+'/codec',default))))
            quality=QComboBox()
            for label in (_('Low'),_('Medium'),_('High'),_('Very high'),_('Maximum')):quality.addItem(label)
            quality.setCurrentIndex(max(0,min(4,int(self.settings.value('music_wizard/'+key+'/quality',3))-1)))
            gain=QDoubleSpinBox();gain.setRange(-60,30);gain.setDecimals(1)
            gain.setSingleStep(.5);gain.setSuffix(' dB')
            gain.setValue(float(self.settings.value('music_wizard/'+key+'/gain',0)))
            form.addRow(_('Output format'),codec)
            form.addRow(_('Quality level'),quality)
            form.addRow(_('Constant gain (optional)'),gain)
            codec.currentIndexChanged.connect(lambda _,c=codec,q=quality:q.setEnabled(c.currentData() not in ('wav16','wav24')))
            quality.setEnabled(codec.currentData() not in ('wav16','wav24'))
            section=CollapsibleSection(title,widget)
            self.settings_scroll.widget().layout().insertWidget(0 if key=='lossless' else 1,section)
            self.profiles[key]=(codec,quality,gain)
        meta=QWidget();form=QFormLayout(meta)
        self.metadata={}
        for key,label,default in META:
            check=QCheckBox(_(label))
            check.setChecked(str(self.settings.value('music_scraper/'+key,str(default).lower())).lower()=='true')
            form.addRow(check);self.metadata[key]=check
        form.addRow(QLabel(_('Naming patterns and artwork locations are configured under Settings → Audio.')))
        self.settings_scroll.widget().layout().insertWidget(2,CollapsibleSection(_('Music metadata and artwork'),meta))

    def collect(self):
        data={'source':self.source.text().strip(),'target':self.target.text().strip()}
        for key,(codec,quality,gain) in self.profiles.items():
            data[key]={'codec':codec.currentData(),'quality':quality.currentIndex()+1,'gain':gain.value()}
        from configuration_store import effective
        config=effective(Path(__file__).with_name('Media-Buddy.json'))
        wizard=config.get('Audio',{}).get('MusicWizard',{})
        data['whitelist_enabled']=wizard.get('WhitelistEnabled',True)
        data['music_whitelist']=wizard.get('CodecWhitelist',['flac','mp3','opus'])
        # Use the existing scraper's options builder, without constructing another GUI.
        music=config.get('Audio',{}).get('Music',{})
        opts={key:check.isChecked() for key,check in self.metadata.items()}
        opts['write_tags']=opts.pop('tagging')
        opts['plain_lyrics']=opts.pop('plain')
        opts['embed_album_cover']=opts['album_artwork']
        opts['artist_artwork_root']=config.get('Audio',{}).get('ArtistArtworkPath','')
        opts.update(artist_folder_pattern=music.get('ArtistFolderPattern','{albumartist}'),
            album_folder_pattern=music.get('AlbumFolderPattern','{year} - {album}'),
            disc_folder_pattern=music.get('DiscFolderPattern','CD {disc}'),
            bonus_disc_folder=music.get('BonusDiscFolder','Bonus CD'),
            disc_mode=music.get('DiscMode','automatic'),year_mode=music.get('YearMode','original'),
            filename_pattern=music.get('AlbumTrackPattern','{track} - {title}'),
            single_filename_pattern=music.get('SingleTrackPattern','{artist} - {title}'),
            compilation_filename_pattern=music.get('CompilationTrackPattern','{track} - {artist} - {title}'),
            compilation_artists=music.get('CompilationArtists',['Various Artists','Various Artist','Various']),
            audio_extensions=music.get('Extensions',['.mp3','.flac','.wav','.m4a','.ogg','.mka']))
        data['scraper_options']=opts
        return data

    def save(self,silent=False):
        data=self.collect()
        for key in ('source','target'):self.settings.setValue('music_wizard/'+key,data[key])
        for key in ('lossless','lossy'):
            for field,value in data[key].items():self.settings.setValue('music_wizard/'+key+'/'+field,value)
        for key,check in self.metadata.items():
            self.settings.setValue('music_scraper/'+key,str(check.isChecked()).lower())
        self.settings.sync()
        if not silent:QMessageBox.information(self,_('Saved'),_('Settings saved'))
