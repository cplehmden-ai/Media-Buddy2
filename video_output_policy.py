"""Output naming policy shared by all video batch controllers."""
from pathlib import Path

SUPPORTED = {"mkv": ".mkv", "mp4": ".mp4", "ts": ".ts"}

def configured_suffix(config):
    value = str(config.get("VideoSettings", {}).get("OutputContainer", "mkv")).lower().lstrip(".")
    if value not in SUPPORTED:
        raise ValueError(f"Ungültiger Ausgabecontainer: {value!r} (mkv, mp4, ts erlaubt)")
    return SUPPORTED[value]

def effective_suffix(config, video):
    suffix = configured_suffix(config)
    for record in video.get("side_data_list", []):
        if "DOVI" in str(record.get("side_data_type", "")).upper():
            if str(record.get("dv_profile", "")) == "20":
                return ".mp4"
    return suffix

def output_path(target, relative, config, video):
    return Path(target) / Path(relative).with_suffix(effective_suffix(config, video))
