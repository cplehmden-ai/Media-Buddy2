"""Standalone metadata scraper process; structured progress for the GUI."""
import json
import sys
from scraper_progress import progress_receiver
from log_manager import run_with_file_logging

# Keep structured events UTF-8 even when invoked outside the GUI launcher.
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError, ValueError):
    pass

PREFIX = '[MB_METADATA_PROGRESS] '

def progress(event):
    print(PREFIX + json.dumps(event, ensure_ascii=False), flush=True)

def main(argv):
    kind, folder, overrides_json = argv
    overrides = json.loads(overrides_json)
    with progress_receiver(progress):
        if kind == 'Serien':
            from series_scraper import main as scrape
            return scrape(['--type:series', f'--folder:{folder}'], metadata_overrides=overrides)
        if kind == 'Spielfilme':
            from movie_scraper import main as scrape
            return scrape([f'--folder:{folder}', '--type:movies'], wizard_overrides=overrides)
        if kind == 'Musikvideos':
            from musicvideo_scraper import main as scrape
            return scrape([f'--folder:{folder}'], wizard_overrides=overrides)
        if kind == 'Sonstige Videos':
            from other_video_scraper import batch
            def log(message): print(message, flush=True)
            def notify(message):
                log(message)
                import re
                match = re.match(r'^\[(\d+)/(\d+)\]\s+(.+)$', message)
                if match:
                    index, total, name = match.groups()
                    progress({'scraper':'other', 'stage':'Grafiken und NFO bearbeiten',
                              'file':name, 'index':int(index), 'total':int(total), 'percent':None})
            from metadata_stop import requested
            result = batch(folder, folder, overrides, notify, requested)
            log(f'[RESULT] {result}')
            return 1 if result.get('error') else 0
        raise ValueError(f'Unsupported metadata type: {kind}')

if __name__ == '__main__':
    kind = sys.argv[1]
    name = {'Serien':'series', 'Spielfilme':'movie', 'Musikvideos':'musicvideo',
            'Sonstige Videos':'other_video'}[kind]
    sys.exit(run_with_file_logging(main, sys.argv[1:], scraper_name=name))
