"""GUI-only localization. CLI/logging and scraper metadata languages stay independent."""
from __future__ import annotations
import gettext
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "gui_language_settings.json"
LANGUAGES = {"gui": "de", "metadata": "de", "audio": "de", "subtitles": "de"}


def load_languages():
    try:
        saved = json.loads(CONFIG.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            LANGUAGES.update({k: v for k, v in saved.items()
                              if k in LANGUAGES and isinstance(v, str) and v})
    except (OSError, ValueError):
        pass
    return dict(LANGUAGES)


def save_languages(settings):
    from configuration_bootstrap import ensure_configuration
    ensure_configuration(ROOT, files=["gui_language_settings.json"])
    from configuration_store import update_values
    changed = {k: v for k, v in settings.items()
               if k in LANGUAGES and isinstance(v, str) and v and v != LANGUAGES[k]}
    if changed:
        update_values(CONFIG, {(k,): v for k, v in changed.items()})
    updated = dict(LANGUAGES)
    updated.update(changed)
    LANGUAGES.update(updated)

load_languages()
_catalog = gettext.translation("media_buddy", localedir=str(ROOT / "locale"),
                               languages=[LANGUAGES["gui"]], fallback=True)

def _(message):
    return _catalog.gettext(message)


def available_gui_languages():
    """Discover installed GUI catalogs; English also works without a catalog."""
    installed = {path.parent.parent.name
                 for path in (ROOT / "locale").glob("*/LC_MESSAGES/media_buddy.mo")}
    return tuple(code for code in ("de", "en") if code in installed or code == "en") + tuple(
        sorted(installed - {"de", "en"}))


# Persisted page identifiers are independent of display text.
_NAVIGATION = {
    'Assistenten': _('Wizards'),
    'Bearbeitung': _('Processing'),
    'Metadaten': _('Metadata'),
    'Nacharbeiten': _('Post-process'),
    'Hilfsmittel': _('Tools'),
    'Einstellungen': _('Settings'),
    'Serien-Aufarbeitung': _('TV series processing'),
    'Spielfilme-Aufarbeitung': _('Movie processing'),
    'Musikvideo-Aufarbeitung': _('Music video processing'),
    'Video-Aufarbeitung': _('Video processing'),
    'Globale Aufarbeitung': _('Global processing'),
    'Audio-Aufarbeitung': _('Audio processing'),
    'Recode': _('Recode'),
    'Remux': _('Remux'),
    'Audio-Normalisieren': _('Normalize audio'),
    'Videos zusammenfügen': _('Join videos'),
    'Serien': _('TV series'),
    'Spielfilme': _('Movies'),
    'Musikvideos': _('Music videos'),
    'Sonstige Videos': _('Other videos'),
    'Videos-Test': _('Video test'),
    'Werkzeuge': _('Tools'),
    'Allgemein': _('General'),
    'Video': _('Video'),
    'Dateinamen': _('File names'),
    'Audio': _('Audio'),
    'Grafiken': _('Graphics'),
    'Tools': _('Tools'),
}


def navigation_label(key):
    return _NAVIGATION.get(key, key)


# Preserve the worker event protocol and tool diagnostics at the UI boundary.
_STAGES = {
    'Serien-Scraper': _('TV series scraper'),
    'Musikvideo-Scraper': _('Music video scraper'),
    'Film-Scraper': _('Movie scraper'),
    'Sonstige-Videos-Scraper': _('Other videos scraper'),
    'Matroska-Tags': _('Matroska tags'),
    'Musikvideo identifizieren': _('Identify music video'),
    'Musikvideo-Metadaten abrufen': _('Fetch music video metadata'),
    'NFO erstellen': _('Create NFO'),
    'Künstlergrafiken und Thumbnail prüfen': _('Check artist artwork and thumbnail'),
    'Scraper abgeschlossen': _('Scraper finished'),
    'Serie bearbeiten': _('Process TV series'),
    'Filminformationen abrufen': _('Fetch movie information'),
    'Grafiken und NFO bearbeiten': _('Process artwork and NFO'),
    'Konfiguration': _('Configuration'),
    'Nicht gefunden': _('Not found'),
    'Konfigurierter Pfad nicht gefunden; Fallback verwendet': _('Configured path not found; fallback used'),
    'Projektordner Tools/': _('Project folder Tools/'),
    'Projektordner Tools/ (Fallback)': _('Project folder Tools/ (fallback)'),
    'System-PATH': _('System PATH'),
    'System-PATH (Fallback)': _('System PATH (fallback)'),
}


def stage_label(key):
    return _STAGES.get(key, key)
