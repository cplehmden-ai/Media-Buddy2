#!/usr/bin/env python3
"""
Media-Buddy Series Scraper - TVDB/TMDB test 12

V1-compatible CLI:
    python mediabuddy_scraper.py --type:series --folder:"D:\TV\Death in Paradise"

This second test writes episode NFO files only:
- scans the supplied folder recursively for video files
- gets the TVDB series id from tvshow.nfo or TVDB-<id>.txt
- detects season/episode from filenames
- logs in to TVDB v4
- loads German (or configured) series/episode data
- writes one .nfo next to each matched video\n- does not rename/move/delete videos and downloads no artwork
"""

from __future__ import annotations
from linked_media_walk import linked_files, linked_walk

import json
import html
import time
import difflib
from urllib.parse import urlencode
import re
import os
import sys
import requests
from translation_manager import translate_plot
from api_keys import get_api_key
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from artwork_naming import names_for, replicate


TVDB_BASE_URL = "https://api4.thetvdb.com/v4"
USER_AGENT = "TheTVDB v.4 TV Scraper for Kodi"

TVDB_API_KEY = get_api_key("TVDB")

VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".avi", ".mov", ".m4v", ".mpg", ".mpeg",
    ".wmv", ".ts", ".m2ts", ".vob", ".webm", ".flv", ".strm", ".stub"
}

# TVDB uses 3-letter language codes for translation endpoints.
LANGUAGE = "deu"
LANGUAGE_SHORT = "de"
TMDB_LANGUAGE = "de-DE"
SCRAPER_NAME = "Media-Buddy V2 Scraper"
SCRAPER_VERSION = "0.1.0"


@dataclass(frozen=True)
class EpisodeFile:
    path: Path
    season: int
    episode: int
    episodes: tuple[tuple[int, int], ...] = ()


class TVDBError(RuntimeError):
    pass


class TVDBClient:
    def __init__(self, api_key: str, user_agent: str = USER_AGENT):
        self.api_key = api_key
        self.token = ""
        self._cache: dict[str, object] = {}
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept": "application/json",
        })

    def _request(self, method: str, url: str, body: dict | None = None,
                 authenticated: bool = True):
        headers = {}
        if authenticated and self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        try:
            response = self.session.request(
                method,
                url,
                json=body,
                headers=headers,
                timeout=NETWORK_TIMEOUT,
            )
            response.raise_for_status()
        except requests.exceptions.SSLError as exc:
            raise TVDBError(f"SSL error for {url}: {exc}") from exc
        except requests.exceptions.Timeout as exc:
            raise TVDBError(f"Timeout for {url}: {exc}") from exc
        except requests.exceptions.ConnectionError as exc:
            raise TVDBError(f"Network error for {url}: {exc}") from exc
        except requests.exceptions.HTTPError as exc:
            detail = response.text
            raise TVDBError(
                f"HTTP {response.status_code} for {url}\n{detail}"
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise TVDBError(f"Request error for {url}: {exc}") from exc

        try:
            payload = response.json()
        except requests.exceptions.JSONDecodeError as exc:
            raise TVDBError(
                f"Invalid JSON returned by {url}: {response.text[:500]}"
            ) from exc

        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    def login(self) -> None:
        data = self._request(
            "POST",
            f"{TVDB_BASE_URL}/login",
            {"apikey": self.api_key},
            authenticated=False,
        )
        try:
            self.token = data["token"]
        except (TypeError, KeyError) as exc:
            raise TVDBError(f"TVDB login returned no token: {data!r}") from exc

    def get(self, path: str):
        url = f"{TVDB_BASE_URL}{path}"
        if url not in self._cache:
            self._cache[url] = self._request("GET", url)
        return self._cache[url]

    def series_extended(self, series_id: int) -> dict:
        return self.get(f"/series/{series_id}/extended")

    def series_translation(self, series_id: int, language: str) -> dict:
        return self.get(f"/series/{series_id}/translations/{language}")

    def episode_translation(self, episode_id: int, language: str) -> dict:
        return self.get(f"/episodes/{episode_id}/translations/{language}")

    def episode_extended(self, episode_id: int) -> dict:
        return self.get(f"/episodes/{episode_id}/extended")

    def person_extended(self, person_id: int) -> dict:
        return self.get(f"/people/{person_id}/extended")

    def artwork_types(self) -> list[dict]:
        data = self.get("/artwork/types")
        return data if isinstance(data, list) else []

    def series_artworks(self, series_id: int):
        return self.get(f"/series/{series_id}/artworks")

    def season_extended(self, season_id: int) -> dict:
        return self.get(f"/seasons/{season_id}/extended")

    def all_default_episodes(self, series_id: int) -> list[dict]:
        episodes: list[dict] = []
        page = 0
        while True:
            data = self.get(
                f"/series/{series_id}/episodes/default?page={page}"
            )
            page_items = (data or {}).get("episodes", [])
            if not page_items:
                break
            episodes.extend(page_items)
            page += 1
        return episodes


def parse_v1_args(argv: list[str]) -> dict[str, str]:
    """
    Accepts V1-style arguments such as:
        --type:series
        --folder:D:\TV
        --folder:"D:\TV Shows\Death in Paradise"

    Also accepts GNU-style --type=series / --folder=...
    """
    result: dict[str, str] = {}
    for arg in argv:
        if not arg.startswith("--"):
            continue
        raw = arg[2:]
        if ":" in raw:
            key, value = raw.split(":", 1)
        elif "=" in raw:
            key, value = raw.split("=", 1)
        else:
            continue
        result[key.strip().lower()] = value.strip().strip('"')
    return result


def read_tvshow_nfo_tolerant(nfo: Path):
    """Parse NFO in memory; recover bare ampersands without modifying the file."""
    try:
        return ET.parse(nfo).getroot()
    except ET.ParseError as first_error:
        try:
            original = nfo.read_text(encoding="utf-8-sig")
            # Preserve valid XML entities, including numeric references.
            repaired = re.sub(
                r"&(?!#(?:[0-9]+|[xX][0-9A-Fa-f]+);|[A-Za-z_][A-Za-z0-9_.:-]*;)",
                "&amp;", original
            )
            root = ET.fromstring(repaired)
            print(f"[WARN] tvshow.nfo has invalid XML ({first_error}); "
                  "bare '&' repaired in memory")
            return root
        except (OSError, UnicodeError, ET.ParseError) as exc:
            raise ValueError(f"tvshow.nfo cannot be parsed: {exc}") from exc


def find_tvdb_id_from_nfo(root: Path) -> int | None:
    """Read ONLY root/tvshow.nfo. Never borrow an ID from another series."""
    nfo = root / "tvshow.nfo"
    if not nfo.is_file():
        return None

    try:
        elem = read_tvshow_nfo_tolerant(nfo)
    except (ValueError, OSError):
        return None

    for uid in elem.findall(".//uniqueid"):
        if (uid.get("type") or "").lower() == "tvdb":
            text = (uid.text or "").strip()
            if text.isdigit():
                return int(text)

    for tag in ("tvdbid",):
        node = elem.find(f".//{tag}")
        if node is not None:
            text = (node.text or "").strip()
            if text.isdigit():
                return int(text)
    return None


def find_tvdb_id_from_marker(root: Path) -> int | None:
    """Read ONLY TVDB markers directly in root. Never recurse."""
    pattern = re.compile(r"^TVDB[-_ ]?(\d+)\.txt$", re.IGNORECASE)
    try:
        markers = sorted(
            (p for p in root.iterdir() if p.is_file()),
            key=lambda p: p.name.casefold()
        )
    except OSError:
        return None

    for marker in markers:
        match = pattern.match(marker.name)
        if match:
            return int(match.group(1))
    return None


def find_series_id(root: Path) -> tuple[int, str]:
    tvdb_id = find_tvdb_id_from_nfo(root)
    if tvdb_id:
        return tvdb_id, "tvshow.nfo"

    tvdb_id = find_tvdb_id_from_marker(root)
    if tvdb_id:
        return tvdb_id, "TVDB marker"

    raise RuntimeError(
        "No TVDB series id found. For this first test, put either a tvshow.nfo "
        "with a TVDB <uniqueid> or a file named TVDB-<id>.txt in the series folder."
    )


def parse_episode_numbers(filename: str) -> list[tuple[int, int]]:
    """Expand multi-episode ranges, including multiple season groups.

    Supported: S01E01E02, S01E01-E03, S01E05-E08S02E01-E04,
    1x01-1x02, 1x01x02, S2017E04, 2017x04, and conservative terminal 101 / 1203.
    """
    stem = Path(filename).stem
    matches = list(re.finditer(
        r"(?i)(?<![A-Za-z])S(\d+)E(\d{1,3})"
        r"((?:(?:-?E|E)\d{1,3})*)",
        stem,
    ))
    # An immediately following Sxx group is valid (no separator required).
    if not matches:
        matches = list(re.finditer(
            r"(?i)(?<![A-Za-z0-9])(?<!\d)(\d+)x(\d{1,3})"
            r"((?:-?(?:\d+x|x)\d{1,3})*)",
            stem,
        ))
        x_style = True
    else:
        x_style = False

    episodes: list[tuple[int, int]] = []
    for match in matches:
        season = int(match.group(1))
        first = int(match.group(2))
        group = [(season, first)]
        tail = match.group(3)
        if x_style:
            tokens = list(re.finditer(r"(-?)(?:(\d+)x|x)(\d{1,3})", tail, re.I))
            for t in tokens:
                end_season = int(t.group(2)) if t.group(2) else season
                end_episode = int(t.group(3))
                if t.group(1) and end_season == season and end_episode >= group[-1][1]:
                    group.extend((season, ep) for ep in range(group[-1][1] + 1, end_episode + 1))
                else:
                    group.append((end_season, end_episode))
        else:
            tokens = list(re.finditer(r"(-?)E(\d{1,3})", tail, re.I))
            for t in tokens:
                end_episode = int(t.group(2))
                if t.group(1) and end_episode >= group[-1][1]:
                    group.extend((season, ep) for ep in range(group[-1][1] + 1, end_episode + 1))
                else:
                    group.append((season, end_episode))
        episodes.extend(group)

    if not episodes:
        m = re.search(r"(?<!\d)(\d{1,2})(\d{2})(?!\d)\\s*$", stem)
        if m:
            episodes = [(int(m.group(1)), int(m.group(2)))]

    # Deduplicate while preserving filename order.
    return list(dict.fromkeys(episodes))


def parse_episode_number(filename: str) -> tuple[int, int] | None:
    """Compatibility helper: first episode of a video."""
    found = parse_episode_numbers(filename)
    return found[0] if found else None



from config_manager import ConfigManager, ConfigError
from rating_manager import resolve_rating, tvdb_records, tmdb_records
from chapter_downloader import download_tv_chapters

# Test 16 configuration: global settings and module overrides.
CONFIG = {}
NETWORK_TIMEOUT = (15.0, 30.0)
IMAGE_TIMEOUT = (15.0, 30.0)
OVERWRITE_NFO = False
OVERWRITE_ARTWORK = False
DOWNLOAD_IMAGES = True
DOWNLOAD_EPISODE_IMAGES = True
ARTWORK_WRITTEN = set()
RENAME_FILES = True
CREATE_SEASON_FOLDER = True
RENAME_PATTERN = "{SeasonEpisode} - {Title}"
EPISODE_SCHEME = "SxxExx"
SEASON_FOLDER_PREFIX = "Staffel"
SEASON_FOLDER_SPECIAL = "00"
CREATE_MISSING_EPISODE_NFO = True
RENAME_SERIES_FOLDER = False
AUTO_TRANSLATE_PLOTS = False

def process_episode_chapters(video_path, keys, ids, series, episode_tvdb_id=None):
    """Independent optional chapter step, after the final video rename."""
    if CONFIG.get('TVShow', {}).get('Download_Chapters', False) is not True:
        return
    if len(keys) != 1:
        print(f'[CHAPTER SKIP] {video_path.name}: multi-episode file')
        return
    season, episode = keys[0]
    cache = {}
    translate_enabled = CONFIG.get('TVShow', {}).get('Translate_Chapters', False) is True

    def translate_name(name):
        if not translate_enabled:
            return name
        if name not in cache:
            # Use the existing MyMemory translation routine, independent of plot settings.
            global AUTO_TRANSLATE_PLOTS
            old = AUTO_TRANSLATE_PLOTS
            try:
                AUTO_TRANSLATE_PLOTS = True
                cache[name] = translate_fallback_plot(name, 'en')
            finally:
                AUTO_TRANSLATE_PLOTS = old
        return cache[name]

    names = [series.get(k) for k in ('name', 'original_name', 'slug') if series.get(k)]
    year = str(series.get('firstAired') or series.get('first_air_date') or '')[:4]
    manager = ConfigManager(Path(__file__).parent)
    try:
        download_tv_chapters(video_path, manager,
                             episode_tvdb_id=episode_tvdb_id,
                             series_tvdb_id=ids.get('tvdb'), titles=names, year=year,
                             season=season, episode=episode,
                             translator=translate_name if translate_enabled else None,
                             timeout=NETWORK_TIMEOUT)
    except (OSError, ValueError, TypeError, requests.RequestException) as exc:
        print(f'[CHAPTER ERROR] {video_path.name}: {exc}')


def load_test15_config():
    global CONFIG, VIDEO_EXTENSIONS, OVERWRITE_NFO, OVERWRITE_ARTWORK, NETWORK_TIMEOUT, IMAGE_TIMEOUT
    global RENAME_FILES, CREATE_SEASON_FOLDER, RENAME_PATTERN, EPISODE_SCHEME, SEASON_FOLDER_PREFIX, MULTI_NFO_MODE
    global SEASON_FOLDER_SPECIAL, CREATE_MISSING_EPISODE_NFO, RENAME_SERIES_FOLDER, AUTO_TRANSLATE_PLOTS
    global DOWNLOAD_IMAGES, DOWNLOAD_EPISODE_IMAGES, LANGUAGE, LANGUAGE_SHORT, TMDB_LANGUAGE
    manager = ConfigManager(Path(__file__).parent)
    CONFIG = manager.effective
    LANGUAGE_SHORT = CONFIG.get("Language", "de")
    LANGUAGE = CONFIG.get("Language_ThreeLetter", "deu")
    if not isinstance(LANGUAGE_SHORT, str) or not re.fullmatch(r"[a-zA-Z]{2}", LANGUAGE_SHORT):
        raise ConfigError("Language must be a two-letter ISO 639-1 code")
    if not isinstance(LANGUAGE, str) or not re.fullmatch(r"[a-zA-Z]{3}", LANGUAGE):
        raise ConfigError("Language_ThreeLetter must be a three-letter ISO 639-2 code")
    LANGUAGE_SHORT = LANGUAGE_SHORT.lower()
    LANGUAGE = LANGUAGE.lower()
    TMDB_LANGUAGE = LANGUAGE_SHORT
    NETWORK_TIMEOUT = manager.network_timeout()
    IMAGE_TIMEOUT = manager.network_timeout(image=True)
    VIDEO_EXTENSIONS = {"." + v.lstrip("*.").lower() for v in CONFIG.get("FileExtensions", [])}
    overwrite = CONFIG.get("Overwrite", {})
    OVERWRITE_NFO = overwrite.get("NFO", False) is True
    OVERWRITE_ARTWORK = overwrite.get("Artwork", False) is True
    tv = CONFIG.get("TVShow", {})
    DOWNLOAD_IMAGES = CONFIG.get("Download_Images", True) is True
    DOWNLOAD_EPISODE_IMAGES = DOWNLOAD_IMAGES and tv.get("Image_Episode", True) is True
    rename = CONFIG.get("Filenaming", {}).get("Rename", {})
    RENAME_FILES = rename.get("RenameFiles", True) is True
    CREATE_SEASON_FOLDER = tv.get("CreateSeasonFolder", True) is True
    RENAME_SERIES_FOLDER = tv.get("RenameFolder", False) is True
    AUTO_TRANSLATE_PLOTS = tv.get("TranslateMissingPlots", False) is True
    RENAME_PATTERN = str(rename.get("TvShow") or "{SeasonEpisode} - {Title}")
    EPISODE_SCHEME = str(rename.get("TvShowEpisodeScheme", "SxxExx"))
    if EPISODE_SCHEME not in ("SxxExx", "1x01"):
        raise ConfigError("Filenaming.Rename.TvShowEpisodeScheme must be SxxExx or 1x01")
    SEASON_FOLDER_PREFIX = str(tv.get("FolderSeason") or "Staffel").strip()
    SEASON_FOLDER_SPECIAL = str(tv.get("FolderSeasonSpecial", "00")).strip()
    CREATE_MISSING_EPISODE_NFO = tv.get("Create_Episode_NFO_On_Missing_Info", True)
    if type(CREATE_MISSING_EPISODE_NFO) is not bool:
        raise ConfigError("TVShow.Create_Episode_NFO_On_Missing_Info must be boolean")
    if not SEASON_FOLDER_SPECIAL or clean_filename(SEASON_FOLDER_SPECIAL) != SEASON_FOLDER_SPECIAL:
        raise ConfigError("TVShow.FolderSeasonSpecial must be a valid folder suffix/name")
    multi_config = tv.get("MultiEpisode", {"Old": True, "New": True})
    if not isinstance(multi_config, dict) or any(
        type(multi_config.get(k, True)) is not bool for k in ("Old", "New")
    ):
        raise ConfigError("TVShow.MultiEpisode.Old/New must be boolean")
    old = multi_config.get("Old", True)
    new = multi_config.get("New", True)
    MULTI_NFO_MODE = "both" if old and new else "legacy" if old else "modern" if new else "none"
    # Fail early on invalid artwork templates, before any file operations.
    for kind in ("poster", "fanart", "banner", "clearlogo", "clearart", "episode"):
        artwork_filename(kind, episodefile="S01E01")
    for kind in ("seasonposter", "seasonbanner"):
        artwork_filename(kind, season=1)
    print(f"[CONFIG] Rename={RENAME_FILES}, season folders={CREATE_SEASON_FOLDER}, "
          f"overwrite NFO={OVERWRITE_NFO}, artwork={OVERWRITE_ARTWORK}")

def clean_filename(text):
    text = str(text or "").replace("–", "-").replace("—", "-").replace("−", "-")
    replacement = CONFIG.get("ReplaceChar", " ")
    if not isinstance(replacement, str):
        raise ConfigError("ReplaceChar must be a string")
    # Replacement must itself be valid within a Windows/SMB filename.
    if re.search(r'[<>:"/\\|?*\x00-\x1f]', replacement):
        raise ConfigError("ReplaceChar contains forbidden filename characters")
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', lambda _: replacement, text)
    return re.sub(r"\s+", " ", text).strip(" .")

def episode_code(season, episode):
    return f"S{season:02d}E{episode:02d}"

def video_episode_code(season, episode):
    """Configurable VIDEO identifier; Kodi-22 sidecars use episode_code()."""
    if EPISODE_SCHEME == "1x01":
        return f"{season}x{episode:02d}"
    return episode_code(season, episode)


def video_episode_group(keys):
    if not keys:
        raise ValueError("Episode list cannot be empty")
    first_season, first_episode = keys[0]
    if EPISODE_SCHEME == "1x01":
        return f"{first_season}x{first_episode:02d}" + "".join(
            f"x{episode:02d}" if season == first_season else f"-{season}x{episode:02d}"
            for season, episode in keys[1:])
    return episode_code(first_season, first_episode) + "".join(
        f"E{episode:02d}" if season == first_season else f"-{episode_code(season, episode)}"
        for season, episode in keys[1:])


# A placeholder may contain an XML attribute filter, e.g.
# {uniqueid type="tvdb"}. Never evaluate arbitrary XPath from user config.
NFO_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")
NFO_TAG_QUERY = re.compile(
    r"""^([A-Za-z_][\w.-]*)(?:\s+([A-Za-z_][\w.-]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s{}"']+)))?$"""
)


def nfo_tag_value(root, expression):
    """Resolve one episode NFO tag, optionally selected by one XML attribute."""
    if root is None:
        return ""
    match = NFO_TAG_QUERY.fullmatch(expression.strip())
    if not match:
        return ""
    tag, attr, quoted_double, quoted_single, unquoted = match.groups()
    expected = next((v for v in (quoted_double, quoted_single, unquoted) if v is not None), "")
    for child in root:
        if child.tag.lower() != tag.lower():
            continue
        if attr and child.attrib.get(attr) != expected:
            continue
        value = (child.text or "").strip()
        if value:
            return value
    return ""


def target_episode_stem(keys, title, episode_nfo=None):
    """Format video name from the NFO that will be written, not from guessed metadata.

    Special numbering placeholders use the user-selected video scheme.
    Every other {tag} resolves against the first episode's <episodedetails>.
    Missing values disappear with their braces; NFO content is unchanged.
    """
    season, episode = keys[0]
    code = video_episode_group(keys)
    root = episode_nfo.getroot() if isinstance(episode_nfo, ET.ElementTree) else episode_nfo
    fields = {"seasonepisode": code, "season": f"{season:02d}",
              "episode": f"{episode:02d}"}
    if root is None:
        fields["title"] = title or ""
    def resolve(match):
        expression = match.group(1).strip()
        if expression.lower() in fields:
            return str(fields[expression.lower()])
        return nfo_tag_value(root, expression)
    result = NFO_PLACEHOLDER.sub(resolve, RENAME_PATTERN)
    # Legacy S{Season}E{Episode} template must still support both schemes.
    if "{SeasonEpisode}" not in RENAME_PATTERN:
        old_code = f"S{season:02d}E{episode:02d}"
        if old_code in result:
            result = result.replace(old_code, code, 1)
    # Empty tags should not leave doubled or trailing separators in filenames.
    result = re.sub(r"\s+[-–—]\s+(?=[-–—])", " ", result)
    result = re.sub(r"(?:\s+[-–—:]\s*)+$", "", result)
    result = re.sub(r"^\s*[-–—:]\s+", "", result)
    result = clean_filename(result)
    if not re.search(r"(?i)(?:S\d+E\d+|\d+x\d+)", result):
        result = code + (" - " + result if result else "")
    return clean_filename(result)

def season_folder_name(season):
    """FolderSeasonSpecial accepts '00' (default) or a full name such as 'Specials'."""
    if season == 0:
        special = SEASON_FOLDER_SPECIAL
        return f"{SEASON_FOLDER_PREFIX} {special}" if special.isdigit() else special
    return f"{SEASON_FOLDER_PREFIX} {season:02d}"


def episode_target_directory(video: Path, series_folder: Path, season: int) -> Path:
    """Avoid nesting a season directory inside an existing season directory.

    A caller (notably V1 integration) may pass a season folder as the
    processing root. In that case the episode belongs in that folder itself.
    """
    if not CREATE_SEASON_FOLDER:
        return video.parent
    season_name = season_folder_name(season)
    # The configured folder already is the processing root.
    if series_folder.name.casefold() == season_name.casefold():
        return series_folder
    # An episode already in its configured season folder stays there.
    if video.parent.name.casefold() == season_name.casefold():
        return video.parent
    return series_folder / season_name


def missing_episode_metadata(details):
    """A missing episode record is different from a valid record without plot/title."""
    return not details or not details.get("_mediabuddy_episode_found", True)


def related_sidecars(video):
    stem = video.stem
    matches = []
    for candidate in video.parent.iterdir():
        if candidate == video or not candidate.is_file():
            continue
        name = candidate.name
        if candidate.suffix.lower() not in (".nfo", ".jpg", ".jpeg", ".png", ".webp"):
            continue
        if name.lower().startswith(stem.lower() + "-") or candidate.stem.lower() == stem.lower():
            matches.append(candidate)
    return matches

def rename_episode_group(video, series_folder, keys, title, episode_nfo=None):
    if not RENAME_FILES:
        return video
    stem = target_episode_stem(keys, title, episode_nfo)
    season = keys[0][0]
    target_dir = episode_target_directory(video, series_folder, season)
    target_dir.mkdir(parents=True, exist_ok=True)
    related = related_sidecars(video)
    # Preserve already correctly named source (avoid _1 on re-runs).
    base = stem
    if video.parent == target_dir and re.fullmatch(re.escape(stem) + r"_\d+", video.stem, flags=re.I):
        return video
    number = 0
    while True:
        candidate = target_dir / (stem + (f"_{number}" if number else "") + video.suffix)
        if candidate == video or not candidate.exists():
            # Do not silently merge sidecars into unrelated existing files.
            planned = [target_dir / ((stem + (f"_{number}" if number else "")) +
                       f.name[len(video.stem):]) for f in related]
            if all(t == old or not t.exists() for old,t in zip(related,planned)):
                break
        number += 1
    if number:
        print(f"[RENAME CONFLICT] {stem}: using _{number}")
    new_stem = candidate.stem
    if candidate == video and all(target_dir / (new_stem + f.name[len(video.stem):]) == f for f in related):
        return video
    # First move support files, then the video. Never overwrite an existing video.
    for old in related:
        new = target_dir / (new_stem + old.name[len(video.stem):])
        if new != old:
            old.rename(new)
            print(f"[MOVE] {old} -> {new}")
    if candidate != video:
        video.rename(candidate)
        print(f"[RENAME] {video} -> {candidate}")
    return candidate

def nfo_allowed(path):
    if path.exists() and not OVERWRITE_NFO:
        print(f"[NFO SKIP] {path.name} (preserved)")
        return False
    return True

def artwork_allowed(path):
    if path.exists() and not OVERWRITE_ARTWORK:
        print(f"[ART SKIP] {path.name} (preserved)")
        return False
    return True

def scan_episode_files(root: Path) -> list[EpisodeFile]:
    """
    Recursively scan episodes inside ONE already identified series root.
    Nested directories that are themselves series roots are pruned.
    """
    found: list[EpisodeFile] = []

    for current, dirs, files in linked_walk(root):
        current_path = Path(current)

        if current_path != root:
            names_lower = {name.lower() for name in files}
            has_tvshow = "tvshow.nfo" in names_lower
            has_marker = any(TVDB_MARKER_RE.match(name) for name in files)
            if has_tvshow or has_marker:
                print(f"[WARN] Nested series root skipped: {current_path}")
                dirs[:] = []
                continue

        for name in files:
            path = current_path / name
            if path.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            parsed = parse_episode_numbers(path.name)
            if parsed:
                season, episode = parsed[0]
                found.append(EpisodeFile(path, season, episode, tuple(parsed)))
            else:
                print(f"[WARN] No season/episode detected: {path.name}")

    found.sort(key=lambda x: (x.season, x.episode, str(x.path).lower()))
    return found


# TVDB's three-letter language identifiers are normalized for MyMemory.
# Never infer a plot language from the production country.
LANGUAGE_CODES_3_TO_2 = {
    "eng":"en", "deu":"de", "ger":"de", "fra":"fr", "fre":"fr",
    "spa":"es", "ita":"it", "por":"pt", "nld":"nl", "dut":"nl",
    "swe":"sv", "nor":"no", "dan":"da", "fin":"fi", "pol":"pl",
    "ces":"cs", "cze":"cs", "slk":"sk", "hun":"hu", "ron":"ro",
    "rum":"ro", "bul":"bg", "hrv":"hr", "srp":"sr", "slv":"sl",
    "ell":"el", "gre":"el", "tur":"tr", "rus":"ru", "ukr":"uk",
    "ara":"ar", "heb":"he", "hin":"hi", "jpn":"ja", "kor":"ko",
    "zho":"zh", "chi":"zh", "tha":"th", "vie":"vi", "ind":"id",
    "msa":"ms", "may":"ms", "cat":"ca", "eus":"eu", "baq":"eu",
    "glg":"gl", "isl":"is", "ice":"is", "est":"et", "lav":"lv",
    "lit":"lt", "fas":"fa", "per":"fa", "ben":"bn", "tam":"ta",
}


def normalize_plot_language(value) -> str:
    """Return MyMemory-compatible source language or empty if unknown."""
    if not isinstance(value, str):
        return ""
    code = value.strip().lower().replace("_", "-").split("-", 1)[0]
    if code in LANGUAGE_CODES_3_TO_2:
        return LANGUAGE_CODES_3_TO_2[code]
    return code if re.fullmatch(r"[a-z]{2}", code) else ""


def tvdb_original_language(series: dict) -> str:
    for key in ("originalLanguage", "original_language", "originalLanguageCode"):
        code = normalize_plot_language(series.get(key))
        if code:
            return code
    return ""


def tvdb_translation_language(code: str) -> str:
    """TVDB translation endpoints require ISO 639-2 language identifiers."""
    for three, two in LANGUAGE_CODES_3_TO_2.items():
        if two == code and len(three) == 3:
            return three
    return ""


def translate_fallback_plot(plot: str, source_language: str) -> str:
    """Opt-in translation via the shared MyMemory manager; retain original on failure."""
    if not AUTO_TRANSLATE_PLOTS or not plot or not source_language:
        return plot
    source = normalize_plot_language(source_language)
    if source == LANGUAGE_SHORT or not source:
        return plot
    result = translate_plot(plot, source, LANGUAGE_SHORT,
                            timeout=NETWORK_TIMEOUT, user_agent=USER_AGENT)
    if result.success:
        print(f"    [TRANSLATE] {source} -> {LANGUAGE_SHORT}: {len(plot)} chars")
    else:
        print(f"    [TRANSLATE WARN] Original plot retained: {result.error}")
    return result.text


def safe_translation(call, language: str) -> dict:
    try:
        return call(language) or {}
    except TVDBError:
        return {}


def translated_series(client: TVDBClient, series_id: int,
                      language: str) -> tuple[dict, dict]:
    base = client.series_extended(series_id)

    local = safe_translation(
        lambda lang: client.series_translation(series_id, lang), language
    )
    english = {}
    if language != "eng":
        english = safe_translation(
            lambda lang: client.series_translation(series_id, lang), "eng"
        )

    name = local.get("name") or english.get("name") or base.get("name") or ""
    overview = (
        local.get("overview") or english.get("overview")
        or base.get("overview") or ""
    )
    translated = dict(base)
    translated["name"] = name
    source = "" if local.get("overview") else ("en" if english.get("overview") else tvdb_original_language(base))
    translated["overview"] = translate_fallback_plot(overview, source)
    return translated, local


def translated_episode(client: TVDBClient, episode_id: int,
                       language: str) -> dict:
    base = client.episode_extended(episode_id)

    local = safe_translation(
        lambda lang: client.episode_translation(episode_id, lang), language
    )
    english = {}
    if language != "eng":
        english = safe_translation(
            lambda lang: client.episode_translation(episode_id, lang), "eng"
        )

    result = dict(base)
    result["name"] = (
        local.get("name") or english.get("name") or base.get("name") or ""
    )
    result["overview"] = (
        local.get("overview") or english.get("overview")
        or base.get("overview") or ""
    )
    result["_translation_language"] = (
        local.get("language") or english.get("language") or ""
    )
    return result


def build_episode_index(episodes: list[dict]) -> dict[tuple[int, int], dict]:
    index: dict[tuple[int, int], dict] = {}
    for ep in episodes:
        season = ep.get("seasonNumber")
        number = ep.get("number")
        if isinstance(season, int) and isinstance(number, int):
            index[(season, number)] = ep
    return index



def xml_text(parent: ET.Element, tag: str, value) -> ET.Element | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    node = ET.SubElement(parent, tag)
    node.text = text
    return node


def first_year(value) -> str:
    if not value:
        return ""
    m = re.search(r"\b(\d{4})\b", str(value))
    return m.group(1) if m else ""


def get_remote_ids(series: dict) -> dict[str, str]:
    """
    Normalize series-level remote IDs returned by TVDB extended data.
    TVDB itself is known from the selected series id and is added by caller.
    """
    ids: dict[str, str] = {}
    for remote in series.get("remoteIds") or []:
        rid = str(remote.get("id") or "").strip()
        if not rid:
            continue
        source = (
            str(remote.get("sourceName") or "") + " " +
            str(remote.get("type") or "")
        ).lower()

        if "imdb" in source or rid.lower().startswith("tt"):
            ids.setdefault("imdb", rid)
        elif "themoviedb" in source or "tmdb" in source:
            # A numeric TMDB series id.
            if rid.isdigit():
                ids.setdefault("tmdb", rid)
    return ids


def extract_episode_rating(details: dict) -> tuple[str, str]:
    """Only use explicit episode rating structures; never treat TVDB score as Kodi rating."""
    rating = ""
    votes = ""
    for item in details.get("ratings") or []:
        if not isinstance(item, dict):
            continue
        value = item.get("average") or item.get("rating") or item.get("value")
        count = item.get("count") or item.get("votes")
        if value not in (None, "") and not rating:
            rating = str(value)
        if count not in (None, "") and not votes:
            votes = str(count)
        if rating and votes:
            break
    return rating, votes


def extract_runtime(details: dict) -> str:
    for key in ("runtime", "runtimeMinutes"):
        value = details.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return str(int(value))
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def extract_content_ratings(details: dict) -> list[dict]:
    result = []
    for item in details.get("contentRatings") or []:
        if isinstance(item, dict):
            result.append(item)
    return result


def person_name(person: dict) -> str:
    return str(
        person.get("personName")
        or person.get("name")
        or person.get("peopleName")
        or ""
    ).strip()


def person_image(person: dict) -> str:
    return str(
        person.get("image")
        or person.get("imageUrl")
        or ""
    ).strip()


def add_actor(root: ET.Element, name: str, role: str = "", thumb: str = "",
              order=None) -> None:
    if not name:
        return
    actor = ET.SubElement(root, "actor")
    xml_text(actor, "name", name)
    xml_text(actor, "role", role)
    xml_text(actor, "thumb", thumb)
    if order is not None:
        xml_text(actor, "order", order)


def character_person_id(ch: dict) -> int | None:
    """Return the TVDB people/person id exposed by an episode character record."""
    candidates = (
        ch.get("peopleId"),
        ch.get("personId"),
        ch.get("people_id"),
        ch.get("person_id"),
    )
    people = ch.get("people")
    if isinstance(people, dict):
        candidates += (people.get("id"),)

    for value in candidates:
        try:
            if value is not None and str(value).strip():
                return int(value)
        except (TypeError, ValueError):
            pass
    return None


def best_person_image(person: dict) -> str:
    for key in ("image", "imageUrl", "photo", "thumbnail"):
        value = person.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def collect_episode_people(details: dict, client=None) -> tuple[list[dict], list[str], list[str]]:
    actors: list[dict] = []
    directors: list[str] = []
    writers: list[str] = []

    for ch in details.get("characters") or []:
        if not isinstance(ch, dict):
            continue

        name = person_name(ch)
        if not name:
            continue

        ptype = str(ch.get("peopleType") or ch.get("type") or "").strip().lower()
        role = str(ch.get("name") or ch.get("characterName") or "").strip()

        if ptype in ("guest star", "actor"):
            thumb = person_image(ch)
            person_id = character_person_id(ch)

            # Episode records often contain only the person's name. If an ID is
            # available, enrich only the missing portrait from TVDB people data.
            if not thumb and client is not None and person_id is not None:
                try:
                    person = client.person_extended(person_id)

                    thumb = best_person_image(person or {})
                except TVDBError as exc:
                    print(f"  [WARN] Could not load person {person_id} ({name}): {exc}")

            actors.append({
                "name": name,
                "role": role,
                "thumb": thumb,
                "person_id": person_id,
            })

        elif ptype == "director":
            if name not in directors:
                directors.append(name)
        elif ptype in ("writer", "screenplay"):
            if name not in writers:
                writers.append(name)

    for person in details.get("people") or []:
        if not isinstance(person, dict):
            continue
        name = person_name(person)
        if not name:
            continue
        ptype = str(
            person.get("type")
            or person.get("peopleType")
            or person.get("job")
            or ""
        ).strip().lower()
        if ptype == "director" and name not in directors:
            directors.append(name)
        elif ptype in ("writer", "screenplay") and name not in writers:
            writers.append(name)

    return actors, directors, writers


def build_episode_nfo(series_id: int, series: dict, details: dict,
                      season: int, episode: int, client=None) -> ET.ElementTree:
    root = ET.Element("episodedetails")

    title = details.get("name") or ""
    aired = details.get("aired") or details.get("firstAired") or ""
    year = first_year(series.get("firstAired"))

    ET.SubElement(root, "title").text = title
    xml_text(root, "showtitle", series.get("name"))
    ET.SubElement(root, "plot").text = str(details.get("overview") or "").strip()
    xml_text(root, "aired", aired)
    xml_text(root, "season", season)
    xml_text(root, "episode", episode)

    image = details.get("image")
    if image:
        xml_text(root, "thumb", image)

    xml_text(root, "year", year)

    rating, votes = extract_episode_rating(details)
    xml_text(root, "rating", rating)
    xml_text(root, "votes", votes)

    remote_ids = get_remote_ids(series)
    ids = {"tvdb": str(series_id), **remote_ids}
    for id_type in ("tvdb", "imdb", "tmdb"):
        value = ids.get(id_type)
        if value:
            uid = ET.SubElement(root, "uniqueid", {"type": id_type})
            uid.text = value

    # Test 4: MPAA is deliberately present even when no mapped value exists.
    # It must serialize as <mpaa></mpaa>, never as <mpaa />.
    mpaa = ET.SubElement(root, "mpaa")
    mpaa.text = series.get("_mediabuddy_mpaa", "")

    xml_text(root, "runtime", extract_runtime(details))
    xml_text(root, "language", LANGUAGE_SHORT)

    for genre in series.get("genres") or []:
        if isinstance(genre, dict):
            genre = genre.get("name")
        xml_text(root, "genre", genre)

    # For episode NFOs keep network-type companies only, not production companies.
    # This is intentionally conservative until the raw company structures are reviewed.
    for company in series.get("companies") or []:
        if not isinstance(company, dict):
            continue
        ctype = company.get("companyType")
        ctype_name = ""
        if isinstance(ctype, dict):
            ctype_name = str(ctype.get("companyTypeName") or ctype.get("name") or "").lower()
        else:
            ctype_name = str(company.get("type") or "").lower()
        if "network" in ctype_name:
            xml_text(root, "studio", company.get("name"))

    # IMPORTANT Test-3 rule:
    # Episode NFO receives episode characters only. No series-cast fallback.
    actors, directors, writers = collect_episode_people(details, client)
    for actor in actors:
        add_actor(root, actor["name"], actor["role"], actor["thumb"])
    for name in directors:
        xml_text(root, "director", name)
    for name in writers:
        xml_text(root, "credits", name)

    status = series.get("status")
    if isinstance(status, dict):
        status = status.get("name")
    xml_text(root, "status", status)

    try:
        ET.indent(root, space="  ")
    except AttributeError:
        pass
    return ET.ElementTree(root)


def write_episode_nfo(path: Path, tree: ET.ElementTree) -> None:
    # ElementTree cannot emit standalone="yes" in the declaration in the exact
    # format wanted here, so write the small header ourselves.
    created = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    comment = (
        f"<!--created on {created} - "
        f"{SCRAPER_NAME} v{SCRAPER_VERSION}-->"
    )

    xml_body = ET.tostring(
        tree.getroot(),
        encoding="unicode",
        short_empty_elements=False,
    )

    content = (
        '<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n'
        + comment + '\n'
        + xml_body + '\n'
    )
    if not nfo_allowed(path):
        return
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(content, encoding="utf-8")
    temp.replace(path)



def print_episode_diagnostics(details: dict) -> None:
    print("Extended episode diagnostics:")
    print(f"  runtime            : {details.get('runtime')!r}")
    print(f"  runtimeMinutes     : {details.get('runtimeMinutes')!r}")
    print(f"  score              : {details.get('score')!r}")
    print(f"  ratings            : {details.get('ratings')!r}")
    print(f"  contentRatings     : {details.get('contentRatings')!r}")
    chars = details.get("characters") or []
    people = details.get("people") or []
    print(f"  characters         : {len(chars)}")
    print(f"  people             : {len(people)}")

    if chars:
        print("  character records:")
        for ch in chars:
            if not isinstance(ch, dict):
                print(f"    {ch!r}")
                continue
            print(
                "    - "
                f"person={person_name(ch)!r}, "
                f"role={str(ch.get('name') or ch.get('characterName') or '')!r}, "
                f"type={str(ch.get('peopleType') or ch.get('type') or '')!r}, "
                f"image={person_image(ch)!r}, "
                f"peopleId={ch.get('peopleId')!r}, "
                f"personId={ch.get('personId')!r}, "
                f"id={ch.get('id')!r}"
            )

    if people:
        print("  people records:")
        for person in people:
            if not isinstance(person, dict):
                print(f"    {person!r}")
                continue
            print(
                "    - "
                f"name={person_name(person)!r}, "
                f"type={str(person.get('type') or person.get('peopleType') or person.get('job') or '')!r}"
            )



# Kodi names are the Test-10 default only. Keeping all local naming here makes
# it possible to replace them later with Media-Buddy's configurable profiles.
KODI_SERIES_ARTWORK_NAMES = {
    "banner": "banner.jpg",
    "clearart": "clearart.png",
    "clearlogo": "clearlogo.png",
    "background": "fanart.jpg",
    "fanart": "fanart.jpg",
    "poster": "poster.jpg",
}


ARTWORK_OPTIONS = {
    "fanart": ("Image_Fanart", "Image_Fanart"),
    "background": ("Image_Fanart", "Image_Fanart"),
    "poster": ("Image_Poster", "Image_Poster"),
    "banner": ("Image_Banner", "Image_Banner"),
    "clearlogo": ("Image_ClearLogo", "Image_ClearLogo"),
    "clearart": ("Image_ClearArt", "Image_ClearArt"),
    "seasonposter": ("Image_SeasonPoster", "Image_Season_Poster"),
    "seasonbanner": ("Image_SeasonPoster", "Image_Season_Banner"),
    "episode": ("Image_Episode", "Image_Episode"),
}

def artwork_enabled(kind):
    setting, _ = ARTWORK_OPTIONS[kind]
    return DOWNLOAD_IMAGES and CONFIG.get("TVShow", {}).get(setting, True) is True

def artwork_filename(kind, season=None, episodefile=None):
    _, setting = ARTWORK_OPTIONS[kind]
    defaults = {
        "fanart": "fanart.jpg", "background": "fanart.jpg",
        "poster": "poster.jpg", "banner": "banner.jpg",
        "clearlogo": "clearlogo.png", "clearart": "clearart.png",
        "seasonposter": "season{season}-poster.jpg",
        "seasonbanner": "season{season}-banner.jpg",
        "episode": "{episodefile}-thumb.jpg",
    }
    profile_names = names_for(kind)
    pattern = profile_names[0] if profile_names else CONFIG.get("Filenaming", {}).get("TVShow", {}).get(setting, defaults[kind])
    if not isinstance(pattern, str) or not pattern.strip():
        raise ConfigError(f"Filenaming.TVShow.{setting} must be a nonempty string")
    if season == 0 and pattern == defaults[kind] and kind in ("seasonposter", "seasonbanner"):
        return "season-specials-poster.jpg" if kind == "seasonposter" else "season-specials-banner.jpg"
    season_token = "specials" if season == 0 else f"{season:02d}" if season is not None else ""
    result = pattern.replace("{season}", season_token).replace("{episodefile}", episodefile or "")
    if result in ("", ".", "..") or "/" in result or "\\" in result or result != Path(result).name:
        raise ConfigError(f"Invalid artwork filename: {result!r}")
    return result

def kodi_season_artwork_name(season_number: int, kind: str) -> str:
    return artwork_filename("seasonposter" if kind == "poster" else "seasonbanner", season=season_number)



def artwork_url(record: dict) -> str:
    for key in ("image", "imageUrl", "imageURL", "url", "thumbnail"):
        value = str(record.get(key) or "").strip()
        if value:
            if value.startswith("/"):
                return "https://artworks.thetvdb.com" + value
            return value
    return ""


def artwork_language(record: dict) -> str:
    value = record.get("language")
    if isinstance(value, dict):
        value = value.get("id") or value.get("code") or value.get("name")
    return str(value or record.get("languageCode") or "").strip().lower()


def artwork_score(record: dict):
    for key in ("score", "voteAverage", "rating"):
        try:
            return float(record.get(key) or 0)
        except (TypeError, ValueError):
            pass
    return 0.0


def choose_artwork(records: list[dict], language: str) -> dict | None:
    if not records:
        return None

    lang = language.lower()
    # Prefer requested language, then language-neutral, then anything else.
    def key(rec):
        rec_lang = artwork_language(rec)
        lang_rank = 2 if rec_lang == lang else (1 if not rec_lang else 0)
        return (lang_rank, artwork_score(rec))

    return max(records, key=key)


def normalize_artwork_type_name(value) -> str:
    if isinstance(value, dict):
        value = value.get("name") or value.get("slug") or value.get("type")
    name = str(value or "").strip().lower().replace(" ", "")
    aliases = {
        "background": "background",
        "backgrounds": "background",
        "fanart": "background",
        "poster": "poster",
        "posters": "poster",
        "banner": "banner",
        "banners": "banner",
        "clearart": "clearart",
        "clearlogo": "clearlogo",
        "clearlogos": "clearlogo",
    }
    return aliases.get(name, name)


def build_artwork_type_map(client: TVDBClient) -> dict:
    result = {}
    try:
        for item in client.artwork_types():
            if not isinstance(item, dict):
                continue
            aid = item.get("id")
            name = normalize_artwork_type_name(
                item.get("name") or item.get("slug") or item.get("type")
            )
            if aid is not None and name:
                result[aid] = name
                result[str(aid)] = name
    except Exception as exc:
        print(f"[WARN] Could not load TVDB artwork types: {exc}")
    return result


def record_artwork_type(record: dict, type_map: dict) -> str:
    raw = record.get("type")
    if isinstance(raw, dict):
        return normalize_artwork_type_name(raw)
    if raw in type_map:
        return type_map[raw]
    if str(raw) in type_map:
        return type_map[str(raw)]
    return normalize_artwork_type_name(
        record.get("typeName") or record.get("artworkType") or raw
    )


def artwork_records(data) -> list[dict]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("artworks", "data", "images"):
            value = data.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []



# Test 14: Fanart.tv first, TVDB/TMDB fill only missing image types.
FANART_TV_API_KEY = get_api_key("FANART_TV")
FANART_TV_ENABLED = {
    "poster": True, "fanart": True, "banner": True,
    "clearlogo": True, "clearart": True,
    "seasonposter": True, "seasonbanner": True,
}
FANART_TV_FIELDS = {
    "poster": ("tvposter", "poster.jpg"),
    "fanart": ("showbackground", "fanart.jpg"),
    "banner": ("tvbanner", "banner.jpg"),
    "clearlogo": ("hdtvlogo", "clearlogo.png"),
    "clearart": ("hdclearart", "clearart.png"),
}
FANART_TV_ALTERNATES = {
    "fanart": ("tvbackground",),
    "clearlogo": ("clearlogo",),
    "clearart": ("clearart",),
}
def fanart_tv_choose(items):
    if not isinstance(items, list):
        return None
    rank = {LANGUAGE_SHORT: 0, "00": 1, "": 1, "en": 2}
    candidates = [i for i in items if isinstance(i, dict) and
                  isinstance(i.get("url"), str) and i["url"].startswith("https://")]
    candidates.sort(key=lambda i: (rank.get(str(i.get("lang") or "").lower(), 3),
                                   -int(i.get("likes") or 0)))
    return candidates[0] if candidates else None

def fanart_tv_artwork(tvdb_id, folder, seasons, session):
    if not DOWNLOAD_IMAGES or not any(artwork_enabled(k) for k in FANART_TV_ENABLED):
        return
    if not tvdb_id:
        print("[FANART] No TVDB ID, skipping fanart.tv")
        return
    print(f"[FANART] Loading TV artwork for TVDB ID {tvdb_id}...")
    try:
        response = session.get(
            f"https://webservice.fanart.tv/v3/tv/{int(tvdb_id)}",
            params={"api_key": FANART_TV_API_KEY}, timeout=NETWORK_TIMEOUT)
        if response.status_code == 404:
            print("[FANART] No artwork for this series")
            return
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError, TypeError) as exc:
        print(f"[FANART WARN] Artwork API unavailable: {exc}")
        return
    if not isinstance(data, dict):
        return
    for kind, (api_key, filename) in FANART_TV_FIELDS.items():
        if not FANART_TV_ENABLED.get(kind) or not artwork_enabled(kind):
            continue
        target = folder / artwork_filename(kind)
        if str(target).lower() in ARTWORK_WRITTEN or (target.exists() and not OVERWRITE_ARTWORK):
            continue
        options = list(data.get(api_key) or [])
        for alt in FANART_TV_ALTERNATES.get(kind, ()):
            options.extend(data.get(alt) or [])
        chosen = fanart_tv_choose(options)
        if chosen:
            download_artwork_client_session(session, chosen["url"], target, "FANART")
    for season in sorted(set(seasons)):
        for kind, api_key, filename in (
            ("seasonposter", "seasonposter",
             "season-specials-poster.jpg" if season == 0 else f"season{season:02d}-poster.jpg"),
            ("seasonbanner", "seasonbanner",
             "season-specials-banner.jpg" if season == 0 else f"season{season:02d}-banner.jpg"),
        ):
            if not FANART_TV_ENABLED.get(kind) or not artwork_enabled(kind):
                continue
            target = folder / artwork_filename(kind, season=season)
            if str(target).lower() in ARTWORK_WRITTEN or (target.exists() and not OVERWRITE_ARTWORK):
                continue
            options = [a for a in (data.get(api_key) or [])
                       if isinstance(a, dict) and str(a.get("season", "")).isdigit()
                       and int(a["season"]) == season]
            chosen = fanart_tv_choose(options)
            if chosen:
                download_artwork_client_session(session, chosen["url"], target, "FANART")

def download_artwork_client_session(session, url, target, source):
    if not DOWNLOAD_IMAGES:
        return False
    if str(target).lower() in ARTWORK_WRITTEN or (target.exists() and not OVERWRITE_ARTWORK):
        for _kind in ('poster','clearlogo'):
            if target.name in names_for(_kind):
                replicate(target, _kind, False)
                break
        return False
    try:
        with session.get(url, stream=True, timeout=IMAGE_TIMEOUT) as response:
            response.raise_for_status()
            with target.with_name(target.name + ".tmp").open("wb") as fh:
                for chunk in response.iter_content(chunk_size=131072):
                    if chunk:
                        fh.write(chunk)
        target.with_name(target.name + ".tmp").replace(target)
        ARTWORK_WRITTEN.add(str(target).lower())
        for _kind in ('poster','clearlogo'):
            if target.name in names_for(_kind):
                replicate(target, _kind, OVERWRITE_ARTWORK)
                break
        print(f"[{source} WRITE] {target.name}")
        return True
    except (OSError, requests.RequestException) as exc:
        target.with_name(target.name + ".tmp").unlink(missing_ok=True)
        print(f"[{source} WARN] {target.name}: {exc}")
        return False

def download_artwork(client: TVDBClient, url: str, target: Path) -> bool:
    if not DOWNLOAD_IMAGES:
        return False
    if not url:
        return False
    if str(target).lower() in ARTWORK_WRITTEN or (target.exists() and not OVERWRITE_ARTWORK):
        reason = "bereits in diesem Lauf geschrieben" if str(target).lower() in ARTWORK_WRITTEN else "Zieldatei vorhanden (Überschreiben aus)"
        print(f"[ART SKIP] {target}: {reason}")
        return False
    try:
        with client.session.get(url, stream=True, timeout=IMAGE_TIMEOUT) as response:
            response.raise_for_status()
            with target.with_name(target.name + ".tmp").open("wb") as fh:
                for chunk in response.iter_content(chunk_size=128 * 1024):
                    if chunk:
                        fh.write(chunk)
        target.with_name(target.name + ".tmp").replace(target)
        ARTWORK_WRITTEN.add(str(target).lower())
        for _kind in ('poster','clearlogo'):
            if target.name in names_for(_kind):
                replicate(target, _kind, OVERWRITE_ARTWORK)
                break
        print(f"[ART WRITE] {target.name}")
        return True
    except (OSError, requests.exceptions.RequestException) as exc:
        # Do not leave a broken partial image behind.
        try:
            target.with_name(target.name + ".tmp").unlink(missing_ok=True)
        except OSError:
            pass
        print(f"[ART WARN] Could not download {target.name}: {exc}")
        return False


def download_episode_thumb(client: TVDBClient, details: dict, video_path: Path) -> None:
    if not DOWNLOAD_EPISODE_IMAGES:
        return
    url = str(details.get("image") or "").strip()
    if url.startswith("/"):
        url = "https://artworks.thetvdb.com" + url
    target = video_path.with_name(artwork_filename("episode", episodefile=video_path.stem))
    if url:
        download_artwork(client, url, target)
    else:
        print(f"[ART MISS] Episode thumb: {video_path.name}")




def download_series_artwork(client: TVDBClient, series_id: int, folder: Path,
                            type_map: dict, present_seasons: set[int]) -> None:
    if not DOWNLOAD_IMAGES:
        return
    print("[TVDB] Loading series artwork...")
    try:
        records = artwork_records(client.series_artworks(series_id))
    except TVDBError as exc:
        print(f"[ART WARN] Could not load series artwork: {exc}")
        records = []

    grouped = {}
    for record in records:
        kind = record_artwork_type(record, type_map)
        grouped.setdefault(kind, []).append(record)

    for kind, filename in KODI_SERIES_ARTWORK_NAMES.items():
        if not artwork_enabled(kind):
            continue
        filename = artwork_filename(kind)
        # fanart and background are the same local Kodi target.
        lookup = "background" if kind == "fanart" else kind
        if kind == "fanart" and (folder / filename).exists():
            continue
        selected = choose_artwork(grouped.get(lookup, []), LANGUAGE)
        if selected:
            download_artwork(client, artwork_url(selected), folder / filename)
        elif kind != "fanart":  # background/fanart share the same Kodi target
            print(f"[ART MISS] Series {kind}")

    # Only request season artwork for episodes actually present in this work folder.
    if not present_seasons or not any(artwork_enabled("season" + kind) for kind in ("poster", "banner")):
        return

    # Series extended already contains the default-season records and IDs.
    try:
        series = client.series_extended(series_id)
    except TVDBError as exc:
        print(f"[ART WARN] Could not reload seasons: {exc}")
        return

    seasons = series.get("seasons") or []
    default_type = series.get("defaultSeasonType")
    for season in seasons:
        if not isinstance(season, dict):
            continue

        stype = season.get("type")
        stype_id = stype.get("id") if isinstance(stype, dict) else stype
        # If TVDB exposes multiple orderings, only use the default/aired one.
        if default_type is not None and stype_id is not None:
            try:
                if int(stype_id) != int(default_type):
                    continue
            except (TypeError, ValueError):
                pass

        number = season.get("number")
        sid = season.get("id")
        try:
            number = int(number)
            sid = int(sid)
        except (TypeError, ValueError):
            continue
        if number not in present_seasons:
            continue

        try:
            sdata = client.season_extended(sid)
        except TVDBError as exc:
            print(f"[ART WARN] Season {number:02d}: {exc}")
            continue

        srecords = artwork_records(sdata.get("artwork") or sdata.get("artworks") or [])
        # Some API records expose a single best season image directly.
        if not srecords:
            image = str(sdata.get("image") or "").strip()
            if image:
                # Treat unknown single season image as poster only.
                srecords = [{"type": "poster", "image": image}]

        sgrouped = {}
        for record in srecords:
            kind = record_artwork_type(record, type_map)
            sgrouped.setdefault(kind, []).append(record)

        for kind in ("poster", "banner"):
            if not artwork_enabled("season" + kind):
                continue
            selected = choose_artwork(sgrouped.get(kind, []), LANGUAGE)
            target = folder / kodi_season_artwork_name(number, kind)
            if selected:
                download_artwork(client, artwork_url(selected), target)
            else:
                print(f"[ART MISS] Season {number:02d} {kind}")

MULTI_NFO_MODE = "both"  # Test 11: "legacy", "modern", "both"


def episode_nfo_path(video_path: Path, season: int, episode: int) -> Path:
    return video_path.with_name(
        f"{video_path.stem}-S{season:02d}E{episode:02d}.nfo"
    )


def episode_thumb_path(video_path: Path, season: int, episode: int) -> Path:
    return video_path.with_name(
        artwork_filename("episode", episodefile=f"{video_path.stem}-S{season:02d}E{episode:02d}")
    )


def write_legacy_multi_nfo(path: Path, trees: list[ET.ElementTree]) -> None:
    """Kodi <=21 legacy multi-root XML-like NFO (intentionally not valid XML)."""
    created = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header = ('<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n'
              f'<!--created on {created} - {SCRAPER_NAME} v{SCRAPER_VERSION}-->\n')
    content = header + "".join(
        ET.tostring(t.getroot(), encoding="unicode", short_empty_elements=False) + "\n"
        for t in trees
    )
    if not nfo_allowed(path):
        return
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(content, encoding="utf-8")
    temp.replace(path)


def download_episode_thumb_named(client: TVDBClient, details: dict,
                                 target: Path) -> None:
    if not DOWNLOAD_EPISODE_IMAGES:
        return
    url = details.get("image") or ""
    if not url:
        print(f"[ART MISS] {target.name}")
    elif str(target).lower() in ARTWORK_WRITTEN or (target.exists() and not OVERWRITE_ARTWORK):
        reason = "bereits in diesem Lauf geschrieben" if str(target).lower() in ARTWORK_WRITTEN else "Zieldatei vorhanden (Überschreiben aus)"
        print(f"[ART SKIP] {target}: {reason}")
    else:
        download_artwork(client, url, target)



 # TMDB key is provided by the central API-key layer.
# # installed Kodi TMDB series scraper; replace before publication.
TMDB_API_KEY = get_api_key("TMDB")
TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMAGES = "https://image.tmdb.org/t/p/original"

class TMDBError(RuntimeError):
    pass

class TMDBClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Media-Buddy-V2-Test12/1.0"})
        self.cache = {}

    def get(self, path, **params):
        cache_key = (path, tuple(sorted(params.items())))
        if cache_key in self.cache:
            return self.cache[cache_key]
        try:
            response = self.session.get(
                TMDB_BASE + path,
                params={"api_key": TMDB_API_KEY, **params},
                timeout=NETWORK_TIMEOUT
            )
            response.raise_for_status()
            result = response.json()
            self.cache[cache_key] = result
            return result
        except (requests.RequestException, ValueError) as exc:
            raise TMDBError(f"TMDB request failed {path}: {exc}") from exc

    def details(self, series_id, language=TMDB_LANGUAGE):
        return self.get(f"/tv/{series_id}", language=language,
                        append_to_response="external_ids,credits,images",
                        include_image_language=f"{LANGUAGE_SHORT},en,null")

    def episode(self, series_id, season, episode, language=TMDB_LANGUAGE):
        return self.get(f"/tv/{series_id}/season/{season}/episode/{episode}",
                        language=language, append_to_response="credits,images",
                        include_image_language=f"{LANGUAGE_SHORT},en,null")

    def season(self, series_id, season, language=TMDB_LANGUAGE):
        return self.get(f"/tv/{series_id}/season/{season}", language=language)

def _search_name(folder):
    """Clean a folder name only for an ID-less provider lookup."""
    name = folder.name.strip()
    year = re.search(r"(?<!\d)(?:19|20)\d{2}(?!\d)", name)
    year = int(year.group()) if year else None
    name = re.sub(r"[._]+", " ", name)
    for part in CONFIG.get("FileIgnoreStrings", []):
        if isinstance(part, str) and part.strip():
            name = re.sub(re.escape(part.strip()), " ", name, flags=re.I)
    name = re.sub(r"\b(?:19|20)\d{2}\b", " ", name)
    name = re.sub(r"[\[\]{}()]", " ", name)
    return " ".join(name.split()) or folder.name, year


def _rank_series_candidates(candidates, title, year):
    def score(item):
        candidate_title = str(item.get("name") or item.get("seriesName") or "")
        similarity = difflib.SequenceMatcher(None, title.casefold(), candidate_title.casefold()).ratio()
        first = str(item.get("first_air_time") or item.get("firstAired") or item.get("first_air_date") or item.get("year") or "")
        year_bonus = 0.20 if year and first[:4] == str(year) else (-0.12 if year and first[:4].isdigit() else 0)
        return (similarity + year_bonus, similarity)
    return sorted(candidates, key=score, reverse=True)


def _lookup_series_without_id(folder):
    """Noninteractive title lookup. Returns IDs without touching existing NFOs."""
    title, year = _search_name(folder)
    default = str(CONFIG.get("TVShow", {}).get("Default_Grabber", "TVDB")).upper()
    providers = ("TMDB", "TVDB") if default == "TMDB" else ("TVDB", "TMDB")
    print(f"[SEARCH] No series ID; searching title {title!r} (year={year or 'unknown'})")
    for provider in providers:
        try:
            if provider == "TMDB":
                data = TMDBClient().get("/search/tv", query=title, **({"first_air_date_year": year} if year else {}))
                candidates = data.get("results") or []
                if not candidates and year:
                    candidates = TMDBClient().get("/search/tv", query=title).get("results") or []
                ranked = _rank_series_candidates(candidates, title, year)
                if ranked:
                    selected = ranked[0]
                    result = {"tmdb": int(selected["id"]), "primary": "tmdb"}
                    print(f"[SEARCH] Selected TMDB: {selected.get('name')} ({result['tmdb']})")
                    return result
            else:
                client = TVDBClient(TVDB_API_KEY)
                client.login()
                data = client.get("/search?" + urlencode({"query": title, "type": "series"}))
                candidates = data if isinstance(data, list) else (data.get("data") or [])
                ranked = _rank_series_candidates(candidates, title, year)
                if ranked:
                    selected = ranked[0]
                    result = {"tvdb": int(selected["tvdb_id"] if selected.get("tvdb_id") else selected["id"]), "primary": "tvdb"}
                    print(f"[SEARCH] Selected TVDB: {selected.get('name')} ({result['tvdb']})")
                    return result
            print(f"[SEARCH] No results from {provider} for {title!r}")
        except (TVDBError, TMDBError, ValueError, TypeError, KeyError) as exc:
            print(f"[SEARCH WARN] {provider} search failed: {exc}")
    raise RuntimeError(f"No series found for folder title: {title}")


def identify_series(folder):
    """Prefer explicit marker; otherwise use IDs in the existing tvshow.nfo."""
    marker_ids = {}
    for item in sorted(folder.iterdir(), key=lambda x: x.name.casefold()):
        match = re.fullmatch(r"(TVDB|TMDB)[-_ ]?(\d+)\.txt", item.name, re.I)
        if item.is_file() and match:
            marker_ids[match.group(1).lower()] = int(match.group(2))
    ids = {}
    nfo = folder / "tvshow.nfo"
    if nfo.is_file():
        try:
            root = read_tvshow_nfo_tolerant(nfo)
            for uid in root.findall(".//uniqueid"):
                typ = (uid.get("type") or "").lower()
                val = (uid.text or "").strip()
                if typ in ("tvdb", "tmdb") and val.isdigit():
                    ids[typ] = int(val)
            for typ in ("tvdb", "tmdb"):
                node = root.find(".//" + typ + "id")
                if typ not in ids and node is not None and (node.text or "").strip().isdigit():
                    ids[typ] = int(node.text.strip())
        except (OSError, ValueError) as exc:
            print(f"[WARN] Cannot read tvshow.nfo: {exc}")
    # Explicit markers are authoritative when supplied.
    ids.update(marker_ids)
    if not ids:
        return _lookup_series_without_id(folder)
    ids["primary"] = "tvdb" if "tvdb" in ids else "tmdb"
    # If both are present, preserve previous TVDB-first behaviour.
    return ids

def tmdb_image(path):
    return TMDB_IMAGES + path if path and path.startswith("/") else ""

def tmdb_localized(client, path, **params):
    """Fieldwise configured-language -> English fallback."""
    local = client.get(path, language=TMDB_LANGUAGE, **params)
    english = client.get(path, language="en-US", **params) if LANGUAGE_SHORT != "en" else {}
    merged = dict(english)
    merged.update({k: v for k, v in local.items()
                   if v is not None and v != "" and v != []})
    if not local.get("overview"):
        if english.get("overview"):
            merged["overview"] = translate_fallback_plot(english["overview"], "en")
        else:
            original = normalize_plot_language(local.get("original_language") or english.get("original_language"))
            if not original:
                # Episode responses often omit original_language; TMDB TV series has it.
                original = normalize_plot_language(getattr(client, "_mediabuddy_original_language", ""))
            if original and original != LANGUAGE_SHORT:
                original_record = client.get(path, language=original, **params)
                if original_record.get("overview"):
                    merged["overview"] = translate_fallback_plot(original_record["overview"], original)
    return merged

def tmdb_episode(client, series_id, season, episode):
    path = f"/tv/{series_id}/season/{season}/episode/{episode}"
    try:
        data = tmdb_localized(client, path, append_to_response="credits,images",
                              include_image_language=f"{LANGUAGE_SHORT},en,null")
        data["_mediabuddy_episode_found"] = bool(data.get("id"))
        return data
    except TMDBError as exc:
        print(f"[WARN] TMDB episode S{season:02d}E{episode:02d}: {exc}")
        return {}

def tmdb_add_ids(node, ids):
    for typ in ("tvdb", "tmdb", "imdb"):
        if ids.get(typ):
            ET.SubElement(node, "uniqueid", {"type": typ}).text = str(ids[typ])

def tmdb_cast(node, people):
    for actor in people or []:
        name = actor.get("name")
        if not name:
            continue
        elem = ET.SubElement(node, "actor")
        xml_text(elem, "name", name)
        xml_text(elem, "role", actor.get("character"))
        xml_text(elem, "thumb", tmdb_image(actor.get("profile_path")))

def tmdb_episode_nfo(series, details, season, episode, ids):
    root = ET.Element("episodedetails")
    ET.SubElement(root, "title").text = str(details.get("name") or "").strip()
    xml_text(root, "showtitle", series.get("name"))
    ET.SubElement(root, "plot").text = str(details.get("overview") or "").strip()
    xml_text(root, "aired", details.get("air_date"))
    xml_text(root, "season", season)
    xml_text(root, "episode", episode)
    xml_text(root, "year", (series.get("first_air_date") or "")[:4])
    xml_text(root, "thumb", tmdb_image(details.get("still_path")))
    if details.get("vote_average"):
        xml_text(root, "rating", details["vote_average"])
    if details.get("vote_count"):
        xml_text(root, "votes", details["vote_count"])
    tmdb_add_ids(root, ids)
    ET.SubElement(root, "mpaa").text = series.get("_mediabuddy_mpaa", "")
    xml_text(root, "runtime", details.get("runtime") or next(
        iter(series.get("episode_run_time") or []), None))
    xml_text(root, "language", LANGUAGE_SHORT if details.get("overview") else "en")
    for genre in series.get("genres") or []:
        xml_text(root, "genre", genre.get("name"))
    for network in series.get("networks") or []:
        xml_text(root, "studio", network.get("name"))
    credits = details.get("credits") or {}
    tmdb_cast(root, credits.get("cast"))
    for person in credits.get("crew") or []:
        job = (person.get("job") or "").lower()
        if job == "director":
            xml_text(root, "director", person.get("name"))
        elif job in ("writer", "screenplay", "story"):
            xml_text(root, "credits", person.get("name"))
    xml_text(root, "status", series.get("status"))
    ET.indent(root, space="  ")
    return ET.ElementTree(root)

def tmdb_write_tvshow(folder, series, ids):
    target = folder / "tvshow.nfo"
    if target.exists() and not OVERWRITE_NFO:
        print("[NFO SKIP] tvshow.nfo already exists")
        return
    root = ET.Element("tvshow")
    xml_text(root, "title", series.get("name"))
    xml_text(root, "sorttitle", series.get("name"))
    ET.SubElement(root, "plot").text = str(series.get("overview") or "").strip()
    xml_text(root, "premiered", series.get("first_air_date"))
    xml_text(root, "year", (series.get("first_air_date") or "")[:4])
    tmdb_add_ids(root, ids)
    ET.SubElement(root, "mpaa").text = series.get("_mediabuddy_mpaa", "")
    for genre in series.get("genres") or []:
        xml_text(root, "genre", genre.get("name"))
    for network in series.get("networks") or []:
        xml_text(root, "studio", network.get("name"))
    tmdb_cast(root, (series.get("credits") or {}).get("cast"))
    xml_text(root, "status", series.get("status"))
    ET.indent(root, space="  ")
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    content = ET.tostring(root, encoding="unicode", short_empty_elements=False)
    target_tmp = target.with_name(target.name + ".tmp")
    target_tmp.write_text('<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n'
                      f'<!--created on {stamp} - Media-Buddy V2 Scraper Test12-->\n'
                      + content + "\n", encoding="utf-8")
    target_tmp.replace(target)
    print(f"[NFO WRITE] {target.name}")

def tmdb_download(client, path, target):
    if not DOWNLOAD_IMAGES:
        return
    url = tmdb_image(path)
    if not url:
        return
    if str(target).lower() in ARTWORK_WRITTEN or (target.exists() and not OVERWRITE_ARTWORK):
        print(f"[ART SKIP] {target.name}")
        return
    try:
        response = client.session.get(url, timeout=NETWORK_TIMEOUT)
        response.raise_for_status()
        temp = target.with_name(target.name + ".tmp")
        temp.write_bytes(response.content)
        temp.replace(target)
        target.with_name(target.name + ".tmp").replace(target)
        ARTWORK_WRITTEN.add(str(target).lower())
        for _kind in ('poster','clearlogo'):
            if target.name in names_for(_kind):
                replicate(target, _kind, OVERWRITE_ARTWORK)
                break
        print(f"[ART WRITE] {target.name}")
    except (requests.RequestException, OSError) as exc:
        print(f"[WARN] Artwork {target.name}: {exc}")

def tmdb_series_artwork(client, series, folder, series_id, seasons):
    if not DOWNLOAD_IMAGES:
        return
    if artwork_enabled("poster"):
        tmdb_download(client, series.get("poster_path"), folder / artwork_filename("poster"))
    if artwork_enabled("fanart"):
        tmdb_download(client, series.get("backdrop_path"), folder / artwork_filename("fanart"))
    # TMDB images may contain language-specific posters and logos.
    images = series.get("images") or {}
    logos = sorted(images.get("logos") or [],
                   key=lambda x: (x.get("iso_639_1") != LANGUAGE_SHORT, -(x.get("vote_average") or 0)))
    for logo in logos:
        if not artwork_enabled("clearlogo"):
            break
        path = logo.get("file_path")
        if path:
            ext = ".svg" if path.lower().endswith(".svg") else ".png"
            target = artwork_filename("clearlogo")
            if ext == ".svg" and target.lower().endswith(".png"):
                continue  # Do not save SVG bytes under a PNG extension.
            tmdb_download(client, path, folder / target)
            break
    for season in sorted(seasons):
        if not artwork_enabled("seasonposter"):
            break
        try:
            info = client.season(series_id, season)
            poster = info.get("poster_path")
            if poster:
                name = artwork_filename("seasonposter", season=season)
                tmdb_download(client, poster, folder / name)
        except TMDBError as exc:
            print(f"[WARN] Season artwork {season}: {exc}")

def run_tmdb_series(folder, ids):
    files = scan_episode_files(folder)
    if not files:
        print(f"[SKIP] No recognizable episode files: {folder}")
        return 3
    series_id = ids["tmdb"]
    client = TMDBClient()
    print("=" * 72)
    print("Media-Buddy Series Scraper - TEST 12 (TMDB PRIMARY)")
    print(f"Folder      : {folder}")
    print(f"TMDB ID     : {series_id}")
    print(f"Videos      : {len(files)}")
    print("=" * 72)
    series = tmdb_localized(client, f"/tv/{series_id}",
                            append_to_response="external_ids,credits,images",
                            include_image_language=f"{LANGUAGE_SHORT},en,null")
    client._mediabuddy_original_language = normalize_plot_language(series.get("original_language"))
    if not series.get("id"):
        raise TMDBError(f"TMDB series not found: {series_id}")
    remote = series.get("external_ids") or {}
    if remote.get("tvdb_id") and not ids.get("tvdb"):
        ids["tvdb"] = remote["tvdb_id"]
    if remote.get("imdb_id"):
        ids["imdb"] = remote["imdb_id"]
    # TMDB ratings are a separate endpoint, not part of /tv/{id} details.
    try:
        certification_data = client.get(f"/tv/{series_id}/content_ratings")
    except (TMDBError, requests.exceptions.RequestException) as exc:
        print(f"[RATING WARN] TMDB certifications unavailable: {exc}")
        certification_data = {}
    series["_mediabuddy_mpaa"] = resolve_rating(
        tmdb_records(certification_data), series, CONFIG, provider="TMDB")
    print(f"Series      : {series.get('name')}")
    print(f"Linked TVDB : {ids.get('tvdb', 'none')}")
    tmdb_write_tvshow(folder, series, ids)
    seasons = {s for item in files for s, _ in (item.episodes or ((item.season,item.episode),))}
    if DOWNLOAD_IMAGES:
        fanart_tv_artwork(ids.get("tvdb"), folder, seasons, client.session)
        tmdb_series_artwork(client, series, folder, series_id, seasons)
    for item in files:
        from metadata_stop import requested as metadata_stop_requested
        if metadata_stop_requested():
            print("[ABORT] Stop after previous episode file requested")
            break
        keys = item.episodes or ((item.season, item.episode),)
        multi = len(keys) > 1
        if multi and MULTI_NFO_MODE == "none":
            print(f"[SKIP] Multi-episode disabled: {item.path.name}")
            continue
        trees = []
        details_list = []
        print(f"\nFile: {item.path.name}")
        for season, episode in keys:
            details = tmdb_episode(client, series_id, season, episode)
            if missing_episode_metadata(details) and not CREATE_MISSING_EPISODE_NFO:
                print(f"[SKIP] Missing episode metadata S{season:02d}E{episode:02d}; configured to skip NFO")
                trees = []
                break
            trees.append(tmdb_episode_nfo(series, details, season, episode, ids))
            details_list.append(details)
            print(f"  S{season:02d}E{episode:02d}: {details.get('name') or 'no title'}")

        if not trees:
            continue
        video_path = rename_episode_group(item.path, folder, keys, details_list[0].get("name"), trees[0])
        if multi:
            if MULTI_NFO_MODE in ("modern", "both"):
                for (season, episode), tree, details in zip(keys, trees, details_list):
                    nfo = episode_nfo_path(video_path, season, episode)
                    write_episode_nfo(nfo, tree)
                    if DOWNLOAD_EPISODE_IMAGES:
                        tmdb_download(client, details.get("still_path"), episode_thumb_path(video_path, season, episode))
            if MULTI_NFO_MODE in ("legacy", "both"):
                nfo = video_path.with_suffix(".nfo")
                write_legacy_multi_nfo(nfo, trees)
                print(f"  [NFO WRITE] {nfo.name} (legacy)")
                image = next((d.get("still_path") for d in details_list if d.get("still_path")), "")
                if DOWNLOAD_EPISODE_IMAGES:
                    tmdb_download(client, image, video_path.with_name(artwork_filename("episode", episodefile=video_path.stem)))
        else:
            nfo = video_path.with_suffix(".nfo")
            write_episode_nfo(nfo, trees[0])
            print(f"  [NFO WRITE] {nfo.name}")
            if DOWNLOAD_EPISODE_IMAGES:
                tmdb_download(client, details_list[0].get("still_path"),
                              video_path.with_name(artwork_filename("episode", episodefile=video_path.stem)))
        process_episode_chapters(video_path, keys, ids, series)
    print("\nTEST 15 COMPLETE - rename and overwrite settings applied.")
    return 0



def resolve_episode_fields(tvdb, client, episode_id, tmdb_client, tmdb_id, season, episode):
    """Per-field German-first resolution, keeping TVDB for all other metadata."""
    tvdb_de = safe_translation(lambda lang: client.episode_translation(episode_id, lang), LANGUAGE) if episode_id else {}
    tvdb_en = safe_translation(lambda lang: client.episode_translation(episode_id, lang), "eng") if episode_id else {}
    tmdb_de, tmdb_en = {}, {}
    if tmdb_client and tmdb_id:
        path = f"/tv/{tmdb_id}/season/{season}/episode/{episode}"
        for lang in dict.fromkeys((TMDB_LANGUAGE, "en-US")):
            try:
                record = tmdb_client.get(path, language=lang)
                if lang == TMDB_LANGUAGE:
                    tmdb_de = record
                else:
                    tmdb_en = record
            except TMDBError as exc:
                print(f"    [FALLBACK WARN] TMDB {lang}: {exc}")
    original_lang = tvdb_original_language(tvdb)
    if not original_lang:
        original_lang = tvdb_original_language(getattr(client, "_mediabuddy_series_metadata", {}))
    tvdb_original = {}
    tmdb_original = {}
    if not tvdb_de.get("overview") and not tmdb_de.get("overview") and not tvdb_en.get("overview") and not tmdb_en.get("overview"):
        if original_lang and original_lang not in (LANGUAGE_SHORT, "en") and episode_id:
            code = tvdb_translation_language(original_lang)
            if code:
                tvdb_original = safe_translation(lambda lang: client.episode_translation(episode_id, lang), code)
        tmdb_original_lang = normalize_plot_language(getattr(tmdb_client, "_mediabuddy_original_language", "")) if tmdb_client else ""
        if tmdb_client and tmdb_id and not tmdb_original_lang and not getattr(tmdb_client, "_mediabuddy_original_checked", False):
            tmdb_client._mediabuddy_original_checked = True
            try:
                tmdb_series = tmdb_client.get(f"/tv/{tmdb_id}", language=TMDB_LANGUAGE)
                tmdb_original_lang = normalize_plot_language(tmdb_series.get("original_language"))
                tmdb_client._mediabuddy_original_language = tmdb_original_lang
            except (TMDBError, requests.RequestException) as exc:
                print(f"    [FALLBACK WARN] TMDB original language lookup: {exc}")
        if tmdb_client and tmdb_id and tmdb_original_lang and tmdb_original_lang not in (LANGUAGE_SHORT, "en"):
            try:
                tmdb_original = tmdb_client.get(path, language=tmdb_original_lang)
            except (TMDBError, requests.RequestException) as exc:
                print(f"    [FALLBACK WARN] TMDB original language: {exc}")
    result = dict(tvdb)
    for field, label in (("name", "title"), ("overview", "plot")):
        options = ((f"TVDB {LANGUAGE_SHORT}", tvdb_de), (f"TMDB {LANGUAGE_SHORT}", tmdb_de),
                   ("TVDB en", tvdb_en), ("TMDB en", tmdb_en),
                   ("TVDB original", tvdb_original), ("TMDB original", tmdb_original),
                   ("TVDB base", tvdb))
        value, source = "", "missing"
        for candidate_source, record in options:
            candidate = record.get(field) if isinstance(record, dict) else None
            if isinstance(candidate, str) and candidate.strip():
                value, source = candidate.strip(), candidate_source
                break
        if field == "overview":
            source_lang = {"TVDB en": "en", "TMDB en": "en",
                           "TVDB original": original_lang,
                           "TMDB original": normalize_plot_language(getattr(tmdb_client, "_mediabuddy_original_language", "")) if tmdb_client else "",
                           "TVDB base": original_lang}.get(source, "")
            value = translate_fallback_plot(value, source_lang)
        result[field] = value
        display = value if field == "name" else f"{len(value)} characters"
        print(f"    [FIELD] {label}: {source} -> {display}")
    if not result.get("image"):
        still = tmdb_de.get("still_path") or tmdb_en.get("still_path")
        if still:
            result["image"] = tmdb_image(still)
            print("    [FIELD] thumb: TMDB")
    return result


def lookup_tmdb_id(tvdb_id, series, explicit_id=None):
    if explicit_id:
        return int(explicit_id)
    ids = get_remote_ids(series)
    for key in ("tmdb", "themoviedb"):
        val = ids.get(key)
        if val and str(val).isdigit():
            return int(val)
    try:
        data = TMDBClient().get(f"/find/{tvdb_id}", external_source="tvdb_id")
        found = data.get("tv_results") or []
        if found:
            return int(found[0]["id"])
    except (TMDBError, KeyError, ValueError, TypeError) as exc:
        print(f"[FALLBACK] TMDB ID lookup: {exc}")
    return None


def rename_series_folder_after_scrape(folder):
    """Rename a completed series folder using the tvshow.nfo title.

    Never replace or merge an existing destination folder. If a source
    contains no readable title, keep the existing directory unchanged.
    """
    if not RENAME_SERIES_FOLDER:
        return folder
    nfo = folder / "tvshow.nfo"
    if not nfo.is_file():
        print(f"[FOLDER SKIP] No tvshow.nfo for rename: {folder}")
        return folder
    try:
        title = (read_tvshow_nfo_tolerant(nfo).findtext("title") or "").strip()
    except (ET.ParseError, OSError) as exc:
        print(f"[FOLDER SKIP] Cannot read series title: {exc}")
        return folder
    if not title:
        print(f"[FOLDER SKIP] Empty series title: {folder}")
        return folder
    target = folder.with_name(clean_filename(title))
    if target == folder:
        return folder
    if target.exists():
        print(f"[FOLDER SKIP] Target already exists: {target}")
        return folder
    try:
        folder.rename(target)
    except OSError as exc:
        print(f"[FOLDER WARN] Could not rename {folder}: {exc}")
        return folder
    print(f"[FOLDER RENAME] {folder} -> {target}")
    return target


def run_series(folder: Path) -> int:
    if not folder.is_dir():
        print(f"[ERROR] Folder does not exist: {folder}")
        return 2

    ids = identify_series(folder)
    if ids["primary"] == "tmdb":
        rc = run_tmdb_series(folder, ids)
        if rc in (None, 0):
            rename_series_folder_after_scrape(folder)
        return rc
    series_id, id_source = int(ids["tvdb"]), "identified series"
    files = scan_episode_files(folder)

    print("=" * 72)
    print("Media-Buddy Series Scraper - TEST 13 (FIELD FALLBACK)")
    print("=" * 72)
    print(f"Folder       : {folder}")
    print(f"TVDB series  : {series_id} ({id_source})")
    print(f"Language     : {LANGUAGE}")
    print(f"Video files  : {len(files)}")
    print()

    if not files:
        print("[ERROR] No recognizable episode video files found.")
        return 3

    print("[TVDB] Logging in...")
    client = TVDBClient(TVDB_API_KEY)
    client.login()
    print("[TVDB] Login OK.")

    print("[TVDB] Loading series...")
    series, _ = translated_series(client, series_id, LANGUAGE)
    client._mediabuddy_series_metadata = series
    tmdb_id = lookup_tmdb_id(series_id, series, ids.get("tmdb"))
    tmdb_client = TMDBClient() if tmdb_id else None
    print(f"[FALLBACK] TVDB primary; TMDB ID: {tmdb_id or 'not found'}")
    print(f"[FALLBACK] Field priority: TVDB {LANGUAGE_SHORT} > TMDB {LANGUAGE_SHORT} > TVDB en > TMDB en")
    series["_mediabuddy_mpaa"] = resolve_rating(
        tvdb_records(series), series, CONFIG, provider="TVDB")
    print(f"Series       : {series.get('name', '')}")
    print(f"First aired  : {series.get('firstAired', '')}")
    print(f"Status       : {(series.get('status') or {}).get('name', '')}")
    print()

    print("[TVDB] Writing series NFO...")
    write_tvshow_nfo(folder, series, series_id, client)
    print()

    if DOWNLOAD_IMAGES:
        present_seasons = {season for item in files for season, _ in (item.episodes or ((item.season, item.episode),))}
        print(f"[ART] Staffeln mit Videos im Arbeitsordner: {sorted(present_seasons)}")
        fanart_tv_artwork(series_id, folder, present_seasons, client.session)
        artwork_type_map = build_artwork_type_map(client)
        download_series_artwork(client, series_id, folder, artwork_type_map, present_seasons)
    print()

    print("[TVDB] Loading episode index...")
    all_episodes = client.all_default_episodes(series_id)
    episode_index = build_episode_index(all_episodes)
    print(f"[TVDB] {len(all_episodes)} episode records loaded.")
    print()

    for item in files:
        from metadata_stop import requested as metadata_stop_requested
        if metadata_stop_requested():
            print("[ABORT] Stop after previous episode file requested")
            break
        episode_keys = item.episodes or ((item.season, item.episode),)
        multi = len(episode_keys) > 1
        print("-" * 72)
        print(f"File         : {item.path.name}")
        print("Detected     : " + ", ".join(
            f"S{season:02d}E{episode:02d}" for season, episode in episode_keys
        ))
        if multi:
            print(f"Multi mode   : {MULTI_NFO_MODE}")

        if len(episode_keys) > 1 and MULTI_NFO_MODE == "none":
            print(f"[SKIP] Multi-episode disabled: {item.path.name}")
            continue
        trees = []
        details_list = []
        for season, episode in episode_keys:
            api_ep = episode_index.get((season, episode))
            if not api_ep:
                print(f"[WARN] TVDB missing S{season:02d}E{episode:02d}; partial NFO")
                details = {"_mediabuddy_episode_found": False}
            else:
                try:
                    details = translated_episode(client, int(api_ep["id"]), LANGUAGE)
                except (TVDBError, TypeError, ValueError, KeyError) as exc:
                    print(f"[WARN] S{season:02d}E{episode:02d}: {exc}; partial NFO")
                    details = {"_mediabuddy_episode_found": False}

            episode_id = int(api_ep["id"]) if api_ep and api_ep.get("id") else None
            details["_mediabuddy_episode_found"] = bool(api_ep and api_ep.get("id"))
            details = resolve_episode_fields(
                details, client, episode_id, tmdb_client, tmdb_id, season, episode
            )
            # A valid fallback provider can supply an episode absent from TVDB.
            if not details.get("_mediabuddy_episode_found") and (details.get("name") or details.get("overview")):
                details["_mediabuddy_episode_found"] = True
            if missing_episode_metadata(details) and not CREATE_MISSING_EPISODE_NFO:
                print(f"[SKIP] Missing episode metadata S{season:02d}E{episode:02d}; configured to skip NFO")
                trees = []
                break
            print(f"  S{season:02d}E{episode:02d}: {details.get('name', '')}")
            tree = build_episode_nfo(series_id, series, details, season, episode, client)
            trees.append(tree)
            details_list.append(details)



        if not trees:
            continue
        first_title = details_list[0].get("name") if details_list else ""
        video_path = rename_episode_group(item.path, folder, episode_keys, first_title, trees[0])
        if multi:
            if MULTI_NFO_MODE in ("modern", "both"):
                for (season, episode), tree, details in zip(episode_keys, trees, details_list):
                    nfo_path = episode_nfo_path(video_path, season, episode)
                    write_episode_nfo(nfo_path, tree)
                    download_episode_thumb_named(client, details, episode_thumb_path(video_path, season, episode))
            if MULTI_NFO_MODE in ("legacy", "both"):
                legacy_path = video_path.with_suffix(".nfo")
                write_legacy_multi_nfo(legacy_path, trees)
                print(f"  [NFO WRITE] {legacy_path.name} (legacy)")
                first_image = next((d for d in details_list if d.get("image")), None)
                if first_image:
                    download_episode_thumb_named(
                        client, first_image, video_path.with_name(artwork_filename("episode", episodefile=video_path.stem))
                    )
        else:
            nfo_path = video_path.with_suffix(".nfo")
            write_episode_nfo(nfo_path, trees[0])
            print(f"  [NFO WRITE] {nfo_path.name}")
            download_episode_thumb(client, details_list[0], video_path)
        process_episode_chapters(video_path, episode_keys, {"tvdb": series_id}, series,
                                 episode_tvdb_id=(episode_index.get(episode_keys[0]) or {}).get("id") if not multi else None)

    print()
    print("=" * 72)
    print("TEST 15 COMPLETE - rename and overwrite settings applied.")
    rename_series_folder_after_scrape(folder)
    return 0



def _tvshow_remote_ids(series):
    ids = {}
    for item in (series.get("remoteIds") or []):
        if not isinstance(item, dict):
            continue
        source = str(item.get("sourceName") or item.get("type") or "").lower()
        rid = str(item.get("id") or "").strip()
        if not rid:
            continue
        if "imdb" in source or rid.startswith("tt"):
            ids["imdb"] = rid
        elif "themoviedb" in source or "tmdb" in source:
            ids["tmdb"] = rid
    return ids


def _tvshow_cast(series):
    cast, seen = [], set()
    for ch in (series.get("characters") or []):
        if not isinstance(ch, dict):
            continue
        if str(ch.get("peopleType") or "").strip().lower() != "actor":
            continue
        name = str(ch.get("personName") or "").strip()
        role = str(ch.get("name") or "").strip()
        if not name:
            continue
        key = (name.casefold(), role.casefold())
        if key in seen:
            continue
        seen.add(key)
        thumb = str(ch.get("image") or ch.get("personImgURL") or "").strip()
        if thumb.startswith("/"):
            thumb = "https://artworks.thetvdb.com" + thumb
        cast.append({
            "name": name, "role": role, "thumb": thumb,
            "peopleId": ch.get("peopleId"),
            "sort": ch.get("sort")
        })
    cast.sort(key=lambda a: (a["sort"] is None, a["sort"] if isinstance(a["sort"], int) else 999999))
    return cast


def write_tvshow_nfo(folder, series, series_id, client):
    target = folder / "tvshow.nfo"
    if target.exists() and not OVERWRITE_NFO:
        print(f"[SKIP] tvshow.nfo already exists: {target}")
        return False

    root = ET.Element("tvshow")
    title = str(series.get("name") or "").strip()
    aired = str(series.get("firstAired") or "").strip()
    ET.SubElement(root, "title").text = title
    ET.SubElement(root, "sorttitle").text = title
    ET.SubElement(root, "plot").text = str(series.get("overview") or "").strip()
    ET.SubElement(root, "premiered").text = aired
    ET.SubElement(root, "year").text = aired[:4] if len(aired) >= 4 else str(series.get("year") or "").strip()

    ET.SubElement(root, "uniqueid", {"type": "tvdb"}).text = str(series_id)
    ids = _tvshow_remote_ids(series)
    if ids.get("imdb"):
        ET.SubElement(root, "uniqueid", {"type": "imdb"}).text = ids["imdb"]
    if ids.get("tmdb"):
        ET.SubElement(root, "uniqueid", {"type": "tmdb"}).text = ids["tmdb"]

    ET.SubElement(root, "mpaa").text = series.get("_mediabuddy_mpaa", "")

    for genre in (series.get("genres") or []):
        name = str(genre.get("name") if isinstance(genre, dict) else genre).strip()
        if name:
            ET.SubElement(root, "genre").text = name

    # Same conservative network choice already proven for episode NFOs.
    networks = []
    for company in (series.get("companies") or []):
        if not isinstance(company, dict):
            continue
        ctype = str(company.get("companyType") or company.get("type") or "").lower()
        name = str(company.get("name") or "").strip()
        if name and "network" in ctype:
            networks.append(name)
    if not networks:
        for company in (series.get("companies") or []):
            if isinstance(company, dict) and str(company.get("name") or "").strip() == "BBC One":
                networks.append("BBC One")
    for name in dict.fromkeys(networks):
        ET.SubElement(root, "studio").text = name

    for actor in _tvshow_cast(series):
        thumb = actor["thumb"]
        if not thumb and actor.get("peopleId"):
            try:
                person = client.person_extended(actor["peopleId"])
                thumb = best_person_image(person or {})
            except Exception as exc:
                print(f"[WARN] Could not load portrait for {actor['name']}: {exc}")
        node = ET.SubElement(root, "actor")
        ET.SubElement(node, "name").text = actor["name"]
        if actor["role"]:
            ET.SubElement(node, "role").text = actor["role"]
        if thumb:
            ET.SubElement(node, "thumb").text = thumb

    status = series.get("status") or {}
    status_name = str(status.get("name") if isinstance(status, dict) else status).strip()
    if status_name:
        ET.SubElement(root, "status").text = status_name

    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode", short_empty_elements=False)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header = '<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n'
    header += f'<!--created on {stamp} - Media-Buddy V2 Scraper v0.1.0-test10b-->\n'
    temp = target.with_name(target.name + ".tmp")
    temp.write_text(header + body + "\n", encoding="utf-8")
    temp.replace(target)
    print(f"[WRITE] {target}")
    return True


TVDB_MARKER_RE = re.compile(r"^(?:TVDB|TMDB)-(\d+)\.txt$", re.IGNORECASE)


def _has_own_episodes(folder: Path) -> bool:
    """Only direct videos or recognizable season subfolders, not arbitrary descendants."""
    try:
        for entry in folder.iterdir():
            if entry.is_file() and entry.suffix.lower() in VIDEO_EXTENSIONS and parse_episode_numbers(entry.name):
                return True
            if entry.is_dir() and re.match(r"(?i)^(?:season|staffel|series|specials?|saison)[ _.-]*\d*", entry.name):
                for video in linked_files(entry):
                    if video.is_file() and video.suffix.lower() in VIDEO_EXTENSIONS and parse_episode_numbers(video.name):
                        return True
    except OSError:
        return False
    return False


def is_series_root(folder: Path) -> bool:
    """A series root has an ID marker, an NFO, or episode video files."""
    try:
        if (folder / "tvshow.nfo").is_file():
            return True
        if any(entry.is_file() and TVDB_MARKER_RE.match(entry.name) for entry in folder.iterdir()):
            return True
        return _has_own_episodes(folder)
    except OSError:
        return False


def discover_series_roots(base: Path) -> list[Path]:
    """
    Find independent series roots below base.
    If base itself is a series root, return only base.
    Once a directory is recognized as a series root, do not descend further.
    """
    base = base.resolve()
    if not base.is_dir():
        raise RuntimeError(f"Folder does not exist or is not a directory: {base}")

    if is_series_root(base):
        return [base]

    found = []
    for current, dirs, files in linked_walk(base):
        current_path = Path(current)

        # Ignore the base itself here; it was already tested above.
        if current_path == base:
            continue

        names = {name.lower() for name in files}
        has_tvshow = "tvshow.nfo" in names
        has_marker = any(TVDB_MARKER_RE.match(name) for name in files)

        if has_tvshow or has_marker or _has_own_episodes(current_path):
            found.append(current_path)
            # Critical: season/subfolders of a recognized series are not searched
            # as separate series roots.
            dirs[:] = []

    found.sort(key=lambda p: str(p).casefold())
    return found


def series_has_video_files(root: Path) -> bool:
    """Cheap batch precheck. No API access and no metadata parsing."""
    for current, dirs, files in linked_walk(root):
        current_path = Path(current)

        if current_path != root:
            names_lower = {name.lower() for name in files}
            has_tvshow = "tvshow.nfo" in names_lower
            has_marker = any(TVDB_MARKER_RE.match(name) for name in files)
            if has_tvshow or has_marker:
                dirs[:] = []
                continue

        for name in files:
            if Path(name).suffix.lower() in VIDEO_EXTENSIONS:
                return True

    return False

def run_series_batch(base: Path, roots_override=None) -> int:
    roots = list(roots_override) if roots_override is not None else discover_series_roots(base)

    print("=" * 72)
    print("Series discovery")
    print("=" * 72)
    print(f"Base folder   : {base}")
    print(f"Series found  : {len(roots)}")
    print()

    if not roots:
        print("[WARN] No series folders containing recognizable episodes were found.")
        return 0

    processed = 0
    skipped = 0
    failed = 0
    ARTWORK_WRITTEN.clear()  # Kein Zustand aus einem vorherigen Batch übernehmen.

    from scraper_progress import emit
    for index, root in enumerate(roots, 1):
        from metadata_stop import requested as metadata_stop_requested
        if metadata_stop_requested():
            print("[ABORT] Stop after previous file requested")
            break
        emit('series', 'Serie bearbeiten', file=root.name, index=index, total=len(roots))
        # Important: empty/archived series folders are normal in Media-Buddy's
        # working tree. Skip them before run_series(), hence before TVDB login.
        if not series_has_video_files(root):
            skipped += 1
            continue

        processed += 1
        print()
        print("#" * 72)
        print(f"SERIES {index}/{len(roots)}: {root}")
        print("#" * 72)

        try:
            rc = run_series(root)
            if rc not in (None, 0):
                failed += 1
                print(f"[ERROR] Series returned exit code {rc}: {root}")
        except (RuntimeError, TVDBError, TMDBError) as exc:
            failed += 1
            print(f"[ERROR] {root}: {exc}")
        except Exception as exc:
            failed += 1
            print(f"[ERROR] Unexpected error in {root}: {exc}")

    successful = processed - failed

    print()
    print("=" * 72)
    print("Batch summary")
    print("=" * 72)
    print(f"Series total  : {len(roots)}")
    print(f"Processed     : {processed}")
    print(f"Successful    : {successful}")
    print(f"Skipped       : {skipped} (no video files)")
    print(f"Failed        : {failed}")

    return 10 if failed else 0


def main(argv: list[str], *, metadata_overrides=None) -> int:
    load_test15_config()
    if metadata_overrides is not None:
        # Metadata-page overrides are process-local; do not modify scraper JSON
        # or the separate series wizard's options.
        global DOWNLOAD_IMAGES, DOWNLOAD_EPISODE_IMAGES, OVERWRITE_NFO, OVERWRITE_ARTWORK
        from copy import deepcopy
        from config_manager import _merge
        CONFIG.update(_merge(deepcopy(CONFIG), metadata_overrides))
        DOWNLOAD_IMAGES = CONFIG.get("Download_Images", True) is True
        DOWNLOAD_EPISODE_IMAGES = DOWNLOAD_IMAGES and CONFIG.get("TVShow", {}).get("Image_Episode", True) is True
        OVERWRITE_NFO = CONFIG.get("Overwrite", {}).get("NFO", False) is True
        OVERWRITE_ARTWORK = CONFIG.get("Overwrite", {}).get("Artwork", False) is True
    args = parse_v1_args(argv)

    media_type = args.get("type", "").lower()
    folder_arg = args.get("folder", "")

    if not folder_arg:
        print(
            'Usage: mediabuddy_scraper.py --type:series '
            '--folder:"D:\\TV"'
        )
        return 1


    global MULTI_NFO_MODE
    MULTI_NFO_MODE = args.get("multi", MULTI_NFO_MODE).lower()
    if MULTI_NFO_MODE not in ("legacy", "modern", "both", "none"):
        print("[ERROR] --multi must be legacy, modern, both, or none")
        return 1

    try:
        return run_series_batch(Path(folder_arg))
    except (RuntimeError, TVDBError, TMDBError) as exc:
        print(f"[ERROR] {exc}")
        return 10
    except KeyboardInterrupt:
        print("\n[ABORTED]")
        return 130



from log_manager import run_with_file_logging

if __name__ == "__main__":
    raise SystemExit(run_with_file_logging(main, sys.argv[1:], scraper_name='series'))
