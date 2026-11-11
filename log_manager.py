"""Shared Media-Buddy console/file logging, standard library only."""
from __future__ import annotations
import contextlib
from datetime import datetime
from pathlib import Path
import re
import sys
import traceback

DEFAULT_MAX_LOGS = 30


def _redact(value, secrets=()):
    value = str(value)
    for secret in secrets:
        if secret:
            value = value.replace(str(secret), "[REDACTED]")
    value = re.sub(r"(?i)([?&](?:api[_-]?key|access[_-]?token|token|client[_-]?secret|password)=)[^&#\s]+", r"\1[REDACTED]", value)
    value = re.sub(r"(?i)(\b(?:authorization\s*:\s*bearer|bearer)\s+)[A-Za-z0-9._~+/-]+", r"\1[REDACTED]", value)
    return value


def _console_visible(line):
    text = line.strip()
    if not text:
        return False
    if text.startswith('[MB_METADATA_PROGRESS] '):
        return True
    upper = text.upper()
    if any(marker in upper for marker in (
        "[ERROR", "[WARN", "[ABORT", "[SKIP", "[NFO WRITE", "[RENAME", "[MOVE",
        "[RATING", "[CONFIG", "[ART WARN", "[ART WRITE", "[ART SKIP", "[NFO SKIP",
        "[RENAME CONFLICT", "[CHAPTER", "ERROR", "FINISHED"
    )):
        return True
    return text.startswith(("SERIES ", "Series found", "Processed", "Successful", "Failed",
                            "Skipped", "Batch summary", "Base folder", "Scraper started",
                            "Scraper finished", "Log file", "File:", "Series "))


class _Stream:
    def __init__(self, terminal, logfile, *, stderr=False, secrets=()):
        self.terminal, self.logfile = terminal, logfile
        self.stderr, self.secrets = stderr, secrets
        self.pending = ""

    @property
    def encoding(self):
        return getattr(self.terminal, "encoding", "utf-8")

    def isatty(self):
        return bool(getattr(self.terminal, "isatty", lambda: False)())

    def writable(self):
        return True

    def write(self, text):
        if not isinstance(text, str):
            text = str(text)
        self.pending += text
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            safe = _redact(line, self.secrets)
            self.logfile.write(safe + "\n")
            self.logfile.flush()
            if self.stderr or _console_visible(safe):
                self.terminal.write(safe + "\n")
                self.terminal.flush()
        return len(text)

    def flush(self):
        if self.pending:
            safe = _redact(self.pending, self.secrets)
            self.logfile.write(safe)
            if self.stderr or _console_visible(safe):
                self.terminal.write(safe)
            self.pending = ""
        self.logfile.flush()
        self.terminal.flush()


def _cleanup(directory, max_logs):
    """Keep max_logs newest Media-Buddy logs across all scraper types."""
    if isinstance(max_logs, bool) or not isinstance(max_logs, int) or max_logs < 1:
        raise ValueError("Logging.MaxFiles must be a positive integer")
    files = sorted((p for p in directory.glob("*.log") if p.is_file() and
                    p.name.startswith(("movie_", "series_", "musicvideo_"))),
                   key=lambda p: (p.stat().st_mtime_ns, p.name))
    for old in files[:max(0, len(files) - max_logs)]:
        try:
            old.unlink()
        except OSError as exc:
            print(f"[WARN] Could not delete old log {old}: {exc}", file=sys.stderr)


def run_with_file_logging(main, argv, *, scraper_name, script_dir=None, max_logs=None, secrets=()):
    """Run scraper with timestamped UTF-8 log under scriptdir/Log."""
    directory = Path(script_dir) if script_dir else Path(__file__).resolve().parent
    if max_logs is None:
        try:
            import json
            config = json.loads((directory / "Media-Buddy.json").read_text(encoding="utf-8-sig"))
            max_logs = config.get("Logging", {}).get("MaxFiles", DEFAULT_MAX_LOGS)
        except (OSError, ValueError, TypeError, AttributeError):
            max_logs = DEFAULT_MAX_LOGS
    started = datetime.now().astimezone()
    log_dir = directory / "Log"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        # Reserve one slot for this new run before opening it.
        # Archivierung erfolgt zentral beim GUI-Start.
        log_path = log_dir / f"{scraper_name}_{started.strftime('%Y-%m-%d_%H-%M-%S_%f')}.log"
        logfile = log_path.open("x", encoding="utf-8", buffering=1)
    except (OSError, ValueError) as exc:
        print(f"[WARN] File logging unavailable: {exc}", file=sys.stderr)
        return main(argv)
    stdout, stderr = sys.stdout, sys.stderr
    out = _Stream(stdout, logfile, secrets=secrets)
    err = _Stream(stderr, logfile, stderr=True, secrets=secrets)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            print(f"Scraper started: {started.isoformat(timespec='seconds')}")
            print(f"Log file       : {log_path}")
            try:
                return main(argv)
            except BaseException:
                traceback.print_exc()
                raise
            finally:
                print(f"Scraper finished: {datetime.now().astimezone().isoformat(timespec='seconds')}")
    finally:
        out.flush()
        err.flush()
        logfile.close()
