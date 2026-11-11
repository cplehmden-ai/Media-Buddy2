# Media-Buddy V2

**Media-Buddy V2** is a graphical application for preparing, organizing, and managing your personal video library, with a particular focus on media collections used with **Kodi**. It brings video conversion, metadata retrieval, artwork management, and post-processing together in one place, reducing the amount of repetitive work involved in maintaining a media collection.

Media-Buddy V2 is a new, Python-based, cross-platform successor to the original Windows-only Media-Buddy. Its interface is currently available in English and German.

## What can Media-Buddy V2 do?

- **TV series:** Process episodes, retrieve series and episode information, download artwork, create Kodi-compatible NFO files, and organize filenames. Multi-episode files and split episodes are supported.
- **Movies:** Retrieve movie information and artwork, create NFO files, organize movies, and work with trailers and chapter information.
- **Music videos:** Look up artists, albums, and tracks, create metadata files, and collect available artwork.
- **Other videos:** Process personal recordings and other videos that do not fit into the usual movie or TV-series categories, including metadata and artwork generated from video frames.
- **Video conversion and remuxing:** Convert video and audio streams or copy compatible streams without unnecessary re-encoding. Available options include codec, quality, audio, subtitle handling, and Dolby Vision/HDR10+ processing.
- **Video utilities:** Join compatible video parts, optimize files, and adjust audio normalization or dynamic compression where needed.
- **Post-processing:** Review and correct metadata, choose artwork or video stills, rename files, and update Matroska tags through a graphical interface.
- **Video checking:** Quickly review processed videos by automatically playing short samples from the middle and end of each file.
- **Batch processing:** Work through multiple files with minimal intervention. Errors are logged so they can be reviewed afterward.

The application is designed to **leave original media files untouched** during normal processing. Input and output locations are kept separate; deletion of originals requires an explicit user action and confirmation.

## Supported platforms — current development version

Media-Buddy V2 is designed to be cross-platform. **At present, it can only be run on Windows x64 and Linux x64.** One required component is distributed as a platform-specific compiled Python module, currently provided only for these two systems.

**macOS and other architectures are not supported yet.** Support for additional platforms and automated builds is planned for later development.

The current release is a **source-code test version**, not a standalone installer. You need Python to run it.

## Requirements

### Python

Python **3.12** is used for the current development version. The included compiled modules are built for CPython 3.12, so use a matching **64-bit Python 3.12** installation.

Install the four Python packages listed in `requirements.txt`:

| Package | Purpose |
| --- | --- |
| `PySide6` | Graphical user interface |
| `requests` | Online metadata and artwork services |
| `yt-dlp` | Trailer downloads |
| `smbprotocol` | SMB/network-share access through `smbclient` |

Some packages are needed only for particular features, but installing all four is recommended.

### External tools

Install the following applications separately and configure their executable paths in Media-Buddy V2. **Both `dovi_tool` and `hdr10plus_tool` are essential for the Dolby Vision processing workflows** and must not be omitted:

| Tool | Used for |
| --- | --- |
| [FFmpeg](https://ffmpeg.org/) (including `ffprobe`) | Video/audio processing, analysis, and extracting still images |
| [MKVToolNix](https://mkvtoolnix.download/) | Matroska remuxing, merging, and metadata operations |
| [ImageMagick](https://imagemagick.org/) **7 or newer** | Creating and processing artwork |
| [mpv](https://mpv.io/) | Video preview and playback |
| [dovi_tool](https://github.com/quietvoid/dovi_tool) | Dolby Vision metadata processing |
| [hdr10plus_tool](https://github.com/quietvoid/hdr10plus_tool) | HDR10+ dynamic metadata processing, including workflows needed for Dolby Vision conversion |

**Linux notes:**

- **Jellyfin FFmpeg 8 or newer** is recommended for the best compatibility with Media-Buddy's video-processing features.
- Use **regular, directly accessible installations** of the external command-line tools. **Flatpak-packaged tools are not supported**, because Media-Buddy needs to invoke their executables directly. In particular, make sure MKVToolNix is installed as a regular system application rather than relying on a Flatpak installation.
- **ImageMagick 7 or newer is required**; the older ImageMagick 6 commonly found in Linux distributions is not sufficient.

The tools are not included in this source repository and may be installed in different locations on different systems.

## Getting started

1. Download or clone this repository.
2. Install **64-bit Python 3.12** and the external tools listed above.
3. Open a terminal in the Media-Buddy V2 directory and install the Python dependencies:

   ```bash
   python -m pip install -r requirements.txt
   ```

4. Start the application:

   ```bash
   python Media_Buddy.py
   ```

   Depending on your system, the Python command may be `python3.12` instead of `python`.

5. Configure the paths to the external tools and your preferred media input/output directories in the application.

The repository includes compiled support modules for the currently supported platforms. Their source files containing service credentials are intentionally not distributed.

## Development status

**Media-Buddy V2 is still under active development.** This is a public test version, and **not every feature is finished or guaranteed to work correctly**. Some functions may be incomplete, change between releases, or contain bugs. Please keep backups of important media and test new workflows with copies before relying on them for large collections.

For more information about the project, visit [Media-Buddy V2 on Buddy Tools](https://buddytools.de/media-buddy-v2/).
