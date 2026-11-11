"""Optional cooperative stop, used only by the standalone metadata GUI."""
import os
from pathlib import Path

def requested():
    marker=os.environ.get("MB_METADATA_STOP_FILE")
    return bool(marker and Path(marker).is_file())
