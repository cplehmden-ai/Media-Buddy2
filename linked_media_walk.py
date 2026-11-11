"""Traverse media folders including directory symlinks and Windows junctions.

Paths returned retain the user-selected alias, so editing through a link works
like editing through a normal directory. Cyclic links are not descended into.
"""
from pathlib import Path


def linked_files(root: Path):
    root = Path(root)
    if not root.is_dir():
        return
    stack = [(root, frozenset())]
    while stack:
        folder, ancestors = stack.pop()
        try:
            identity = str(folder.resolve(strict=True))
        except (OSError, RuntimeError):
            continue
        # Only prune ancestor loops; different aliases may intentionally point
        # at the same folder and should remain independently accessible.
        if identity in ancestors:
            continue
        ancestors = ancestors | {identity}
        try:
            entries = sorted(folder.iterdir(), key=lambda p: p.name.casefold())
        except (OSError, PermissionError):
            continue
        dirs = []
        for entry in entries:
            try:
                if entry.is_dir():
                    dirs.append(entry)
                elif entry.is_file():
                    yield entry
            except (OSError, RuntimeError):
                continue
        for entry in reversed(dirs):
            stack.append((entry, ancestors))


def linked_walk(root: Path):
    """os.walk-compatible top-down traversal of linked directories.

    Callers may prune traversal by modifying the yielded dirs list in place.
    Cycles are blocked along each branch; separate aliases are preserved.
    """
    root = Path(root)
    if not root.is_dir():
        return
    stack = [(root, frozenset())]
    while stack:
        folder, ancestors = stack.pop()
        try:
            identity = str(folder.resolve(strict=True))
        except (OSError, RuntimeError):
            continue
        if identity in ancestors:
            continue
        next_ancestors = ancestors | {identity}
        try:
            entries = sorted(folder.iterdir(), key=lambda p: p.name.casefold())
        except (OSError, RuntimeError):
            continue
        dirs, files = [], []
        for entry in entries:
            try:
                if entry.is_dir():
                    dirs.append(entry.name)
                elif entry.is_file():
                    files.append(entry.name)
            except (OSError, RuntimeError):
                continue
        yield str(folder), dirs, files
        for name in reversed(dirs):
            child = folder / name
            if child.is_dir():
                stack.append((child, next_ancestors))


def linked_dirs(root: Path):
    for folder, dirs, _ in linked_walk(root):
        for name in dirs:
            yield Path(folder) / name
