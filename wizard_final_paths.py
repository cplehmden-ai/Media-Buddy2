"""Locate only this run's outputs after the series scraper renames/moves them.

Uses filesystem identity, not the release filename convention. Never guesses an
unrelated video when the identity is unavailable or ambiguous.
"""
from linked_media_walk import linked_files
from pathlib import Path

VIDEO_EXTENSIONS = {'.mkv', '.mk3d'}


def identity(path):
    try:
        stat = Path(path).stat()
        return (stat.st_dev, stat.st_ino) if stat.st_ino else None
    except OSError:
        return None


def locate_processed_outputs(paths, output_root, identities):
    """Return (found, missing); preserve order and avoid duplicate matches."""
    paths = [Path(p) for p in paths]
    root = Path(output_root)
    found, missing = [], []
    wanted = {ident for ident in identities.values() if ident is not None}
    matches = {ident: [] for ident in wanted}
    if wanted:
        for path in linked_files(root):
            if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS:
                ident = identity(path)
                if ident in matches:
                    matches[ident].append(path)
    for original in paths:
        if original.is_file():
            found.append(original)
            continue
        ident = identities.get(original)
        choices = matches.get(ident, []) if ident is not None else []
        if len(choices) == 1:
            found.append(choices[0])
        else:
            missing.append(original)
    return found, missing
