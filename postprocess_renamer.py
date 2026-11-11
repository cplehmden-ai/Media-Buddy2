"""Safe, configuration-based episode and associated sidecar renaming."""
from __future__ import annotations
import re
from pathlib import Path
from configuration_store import effective

PLACEHOLDER = re.compile(r'\{([^{}]+)\}')
QUERY = re.compile(r"""^([A-Za-z_][\w.-]*)(?:\s+([A-Za-z_][\w.-]*)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s{}\"']+)))?$""")

def desired_stem(tree, config_dir):
    config_dir = Path(config_dir)
    config = effective(config_dir / 'series_scraper_config.json')
    global_config = effective(config_dir / 'Media-Buddy.json')
    renaming = config.get('Filenaming', {}).get('Rename', {})
    if renaming.get('RenameFiles', True) is not True:
        return None
    root = tree.getroot()
    season = int(root.findtext('season') or 0)
    episode = int(root.findtext('episode') or 0)
    if season < 0 or episode < 0:
        raise ValueError('Ungültige Staffel-/Episodennummer')
    scheme = renaming.get('TvShowEpisodeScheme', 'SxxExx')
    if scheme not in ('SxxExx', '1x01'):
        raise ValueError('Unbekanntes Episoden-Namensschema')
    code = f'{season}x{episode:02d}' if scheme == '1x01' else f'S{season:02d}E{episode:02d}'
    pattern = str(renaming.get('TvShow') or '{SeasonEpisode} - {Title}')
    fields = {'seasonepisode': code, 'season': f'{season:02d}', 'episode': f'{episode:02d}'}
    def resolve(match):
        expression = match.group(1).strip()
        if expression.lower() in fields:
            return fields[expression.lower()]
        query = QUERY.fullmatch(expression)
        if not query:
            return ''
        tag, attr, q1, q2, q3 = query.groups()
        expected = next((x for x in (q1,q2,q3) if x is not None), '')
        for child in root:
            if child.tag.lower() == tag.lower() and (not attr or child.get(attr) == expected):
                if (child.text or '').strip():
                    return child.text.strip()
        return ''
    result = PLACEHOLDER.sub(resolve,pattern)
    if '{SeasonEpisode}' not in pattern:
        result = result.replace(f'S{season:02d}E{episode:02d}', code, 1)
    result = re.sub(r'\s+[-–—]\s+(?=[-–—])',' ',result)
    result = re.sub(r'(?:\s+[-–—:]\s*)+$','',result)
    result = re.sub(r'^\s*[-–—:]\s+','',result)
    replacement = global_config.get('ReplaceChar', ' ')
    if not isinstance(replacement,str) or re.search(r'[<>:"/\\|?*\x00-\x1f]',replacement):
        raise ValueError('Ungültige ReplaceChar-Konfiguration')
    def clean(s):
        s = str(s).replace('–','-').replace('—','-').replace('−','-')
        s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', lambda _: replacement,s)
        return re.sub(r'\s+',' ',s).strip(' .')
    result=clean(result)
    if not re.search(r'(?i)(?:S\d+E\d+|\d+x\d+)',result):
        result=code + (' - '+result if result else '')
    result=clean(result)
    if not result or result.upper() in {'CON','PRN','AUX','NUL','COM1','LPT1'}:
        raise ValueError('Ungültiger Zieldateiname')
    return result

def rename_episode_files(video, tree, config_dir):
    """Rename media and same-stem sidecars, no overwrite; roll back on failure."""
    video=Path(video)
    new_stem=desired_stem(tree,config_dir)
    if new_stem is None or new_stem == video.stem:
        return video, 0
    old_stem=video.stem
    files=[video]
    for file in video.parent.iterdir():
        if not file.is_file() or file == video:
            continue
        # Exact-stem NFO, chapters, episode thumbs, language-specific subtitles.
        suffix=file.name[len(old_stem):] if file.name.lower().startswith(old_stem.lower()) else ''
        if suffix and (suffix[0] in '.-' and not suffix.lower().startswith(('.part','.tmp'))):
            # Do not capture another episode with a similar prefix.
            if suffix[0] == '-' and not (suffix.lower().startswith(('-thumb.','-chapters.','-poster.','-fanart.'))):
                continue
            files.append(file)
    moves=[(file,file.with_name(new_stem+file.name[len(old_stem):])) for file in files]
    sources={p for p,_ in moves}
    for old,new in moves:
        if new != old and new.exists() and new not in sources:
            raise FileExistsError(f'Zieldatei existiert bereits: {new.name}')
    completed=[]
    try:
        for old,new in moves:
            if old != new:
                old.rename(new)
                completed.append((old,new))
    except OSError:
        for old,new in reversed(completed):
            try:new.rename(old)
            except OSError:pass
        raise
    return moves[0][1], len(completed)
