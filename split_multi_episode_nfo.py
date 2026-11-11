#!/usr/bin/env python3
"""
Split multi-episode Kodi NFO files into one NFO per episode.

Usage:
    python split_multi_episode_nfo.py <folder>
    python split_multi_episode_nfo.py <folder> --dry-run

A multi-episode NFO is identified by having more than one top-level
<episodedetails> element. The corresponding video/stub file is not changed.

For each episode:
    <original>.nfo       -> <original>-SxxExx.nfo
    <original>-thumb.jpg -> <original>-SxxExx-thumb.jpg

Original NFOs and thumbnails are preserved when TVShow.MultiEpisode.Old
is enabled in Media-Buddy.json; otherwise removed after successful output.
"""

from __future__ import annotations
from linked_media_walk import linked_files

import argparse
from config_manager import _read_json, ConfigError
import re
import shutil
import sys
import tempfile
from pathlib import Path
import xml.etree.ElementTree as ET


EPISODE_OPEN_RE = re.compile(r"<episodedetails(?:\s[^>]*)?>", re.IGNORECASE)
EPISODE_CLOSE_RE = re.compile(r"</episodedetails\s*>", re.IGNORECASE)


def split_episode_blocks(text: str) -> tuple[str, list[str]]:
    """Return the text before the first episodedetails and all top-level blocks.

    A small tag scanner is used instead of XML parsing because the original
    multi-episode NFO is deliberately not a valid XML document: it has several
    top-level roots.
    """
    matches = list(
        re.finditer(
            r"</?episodedetails(?:\s[^>]*)?>",
            text,
            re.IGNORECASE,
        )
    )

    if not matches:
        return text, []

    prefix = text[: matches[0].start()]
    blocks: list[str] = []
    depth = 0
    block_start = None

    for match in matches:
        tag = match.group(0)
        if tag.startswith("</"):
            if depth == 0:
                raise ValueError("Unmatched </episodedetails> tag")
            depth -= 1
            if depth == 0 and block_start is not None:
                blocks.append(text[block_start : match.end()])
                block_start = None
        else:
            if depth == 0:
                block_start = match.start()
            depth += 1

    if depth != 0:
        raise ValueError("Unclosed <episodedetails> tag")

    return prefix, blocks


def episode_numbers(block: str) -> tuple[int, int]:
    """Read season and episode from one episodedetails block."""
    try:
        root = ET.fromstring(block)
    except ET.ParseError as exc:
        raise ValueError(f"Invalid XML in episodedetails: {exc}") from exc

    def child_text(name: str) -> str | None:
        for child in root:
            tag = child.tag
            if isinstance(tag, str) and tag.rsplit("}", 1)[-1].lower() == name:
                return (child.text or "").strip()
        return None

    season_text = child_text("season")
    episode_text = child_text("episode")

    if season_text is None or episode_text is None:
        raise ValueError("Missing <season> or <episode>")

    try:
        season = int(season_text)
        episode = int(episode_text)
    except ValueError as exc:
        raise ValueError(
            f"Invalid season/episode: season={season_text!r}, "
            f"episode={episode_text!r}"
        ) from exc

    if season < 0 or episode < 0:
        raise ValueError("Season and episode must not be negative")

    return season, episode


def find_local_thumbnails(nfo: Path) -> list[Path]:
    """Find local thumbnails belonging to the original NFO/video."""
    prefix = nfo.stem + "-thumb"
    result = []

    for candidate in nfo.parent.iterdir():
        if not candidate.is_file():
            continue
        if candidate.stem.lower() == prefix.lower():
            result.append(candidate)

    return sorted(result, key=lambda p: p.name.lower())


def write_atomic(path: Path, data: str) -> None:
    """Write a text file atomically in UTF-8."""
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    try:
        with open(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(data)
        Path(temp_name).replace(path)
    except Exception:
        try:
            Path(temp_name).unlink(missing_ok=True)
        except OSError:
            pass
        raise


def process_nfo(nfo: Path, dry_run: bool = False, keep_original: bool = False) -> tuple[bool, str]:
    """Split one NFO. Return (changed, message)."""
    try:
        # newline="" is intentional: keep the original LF/CRLF style.
        with nfo.open("r", encoding="utf-8-sig", newline="") as fh:
            text = fh.read()
    except UnicodeDecodeError as exc:
        return False, f"SKIP {nfo}: not valid UTF-8 ({exc})"

    try:
        prefix, blocks = split_episode_blocks(text)
    except ValueError as exc:
        return False, f"SKIP {nfo}: {exc}"

    if len(blocks) <= 1:
        return False, f"OK   {nfo}: single episode"

    # Parse every episode and determine all output names before changing anything.
    episodes = []
    seen = set()

    try:
        for block in blocks:
            season, episode = episode_numbers(block)
            suffix = f"-S{season:02d}E{episode:02d}"
            if suffix in seen:
                raise ValueError(f"duplicate episode {suffix}")
            seen.add(suffix)
            episodes.append((suffix, block))
    except ValueError as exc:
        return False, f"SKIP {nfo}: {exc}"

    thumbnails = find_local_thumbnails(nfo)

    outputs = []
    for suffix, block in episodes:
        out_nfo = nfo.with_name(nfo.stem + suffix + nfo.suffix)
        outputs.append(out_nfo)

        for thumb in thumbnails:
            out_thumb = thumb.with_name(
                nfo.stem + suffix + "-thumb" + thumb.suffix
            )
            outputs.append(out_thumb)

    # Never overwrite unrelated existing files.
    existing = [p for p in outputs if p.exists()]
    if existing:
        names = ", ".join(p.name for p in existing)
        return False, f"SKIP {nfo}: output already exists: {names}"

    if dry_run:
        thumb_text = (
            f", {len(thumbnails)} thumbnail(s) x {len(episodes)}"
            if thumbnails else ", no local thumbnail"
        )
        return True, f"DRY  {nfo}: {len(episodes)} episodes{thumb_text}"

    created: list[Path] = []
    # Keep the same final line ending as the source NFO.
    if text.endswith("\r\n"):
        final_eol = "\r\n"
    elif text.endswith("\n"):
        final_eol = "\n"
    elif text.endswith("\r"):
        final_eol = "\r"
    else:
        final_eol = ""

    try:
        # Validate each complete output before writing it.
        for suffix, block in episodes:
            candidate = prefix + block + final_eol
            ET.fromstring(candidate)
            out_nfo = nfo.with_name(nfo.stem + suffix + nfo.suffix)
            write_atomic(out_nfo, candidate)
            created.append(out_nfo)

        # Copy the original local thumbnail once per episode.
        for suffix, _block in episodes:
            for thumb in thumbnails:
                out_thumb = thumb.with_name(
                    nfo.stem + suffix + "-thumb" + thumb.suffix
                )
                shutil.copy2(thumb, out_thumb)
                created.append(out_thumb)

        # Keep legacy files if the global settings request both formats.
        if not keep_original:
            nfo.unlink()
            for thumb in thumbnails:
                thumb.unlink()

    except Exception as exc:
        # Best-effort rollback: remove files created by this operation.
        for path in reversed(created):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        return False, f"ERROR {nfo}: {exc}"

    thumb_text = (
        f", {len(thumbnails)} thumbnail(s) x {len(episodes)}"
        if thumbnails else ", no local thumbnail"
    )
    return True, f"DONE {nfo}: {len(episodes)} episodes{thumb_text}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Split Kodi multi-episode NFO files recursively."
    )
    parser.add_argument("folder", type=Path, help="Folder to scan recursively")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be changed without modifying files",
    )
    args = parser.parse_args()

    folder = args.folder.expanduser().resolve()

    if not folder.is_dir():
        print(f"ERROR: folder does not exist: {folder}", file=sys.stderr)
        return 2

    try:
        global_settings = _read_json(Path(__file__).resolve().parent / "Media-Buddy.json")
        multi = global_settings.get("TVShow", {}).get("MultiEpisode", {})
        if not isinstance(multi, dict) or any(type(multi.get(k)) is not bool for k in ("Old", "New")):
            raise ConfigError("TVShow.MultiEpisode.Old/New must be boolean")
        keep_original = multi["Old"]
        if not multi["New"]:
            print("INFO: New multi-episode NFO format disabled; nothing to split.")
            return 0
    except (ConfigError, TypeError, AttributeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    changed = 0
    skipped = 0
    errors = 0

    for nfo in sorted(linked_files(folder), key=lambda p: str(p).lower()):
        if not nfo.is_file() or nfo.suffix.lower() != ".nfo":
            continue

        did_change, message = process_nfo(nfo, args.dry_run, keep_original=keep_original)
        print(message)

        if message.startswith("ERROR"):
            errors += 1
        elif did_change:
            changed += 1
        else:
            skipped += 1

    print()
    print(
        f"Finished: {changed} multi-episode NFO(s) processed, "
        f"{skipped} skipped, {errors} error(s)."
    )

    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
