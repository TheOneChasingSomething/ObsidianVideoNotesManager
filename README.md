<div align="center">

```
 ██╗   ██╗████████╗      ██████╗ ██╗      ██████╗ ██╗   ██╗██╗     ██╗███████╗████████╗
 ╚██╗ ██╔╝╚══██╔══╝      ██╔══██╗██║     ██╔════╝ ██║   ██║██║     ██║██╔════╝╚══██╔══╝
  ╚████╔╝    ██║   █████╗ ██████╔╝██║     ██║  ███╗██║   ██║██║     ██║███████╗   ██║
   ╚██╔╝     ██║   ╚════╝ ██╔═══╝ ██║     ██║   ██║╚██╗ ██╔╝██║     ██║╚════██║   ██║
    ██║       ██║          ██║     ███████╗╚██████╔╝ ╚████╔╝ ███████╗██║███████║   ██║
    ╚═╝       ╚═╝          ╚═╝     ╚══════╝ ╚═════╝   ╚═══╝  ╚══════╝╚═╝╚══════╝   ╚═╝
```

**ObsidianVideoNotesManager** — YouTube playlist downloader with Obsidian integration

[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![yt-dlp](https://img.shields.io/badge/yt--dlp-latest-red)](https://github.com/yt-dlp/yt-dlp)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

</div>

---

## Overview

**ObsidianVideoNotesManager** is a CLI tool that downloads YouTube videos and web articles, then automatically generates structured Markdown notes in your [Obsidian](https://obsidian.md/) vault.

It supports:
- Downloading individual videos, full playlists, or batch URLs from a file
- Downloading and archiving web articles (HTML + images)
- Enriching metadata via the YouTube Data API v3 (author, tags, description, publish date)
- Generating Obsidian notes with YAML frontmatter (author, URL, tags, project, knowledge index, download path)
- Tracking download state in a local SQLite database
- Secure credential management via system keyring (GNOME/KDE/macOS)

---

## Requirements

- Python 3.11+
- `ffmpeg` (for merging video and audio streams)

```bash
sudo apt install ffmpeg        # Debian / Ubuntu / Kali
sudo pacman -S ffmpeg          # Arch Linux
brew install ffmpeg            # macOS
```

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/your-username/ObsidianVideoNotesManager.git
cd ObsidianVideoNotesManager
```

### 2. Create a virtual environment

```bash
python3 -m venv venv
source venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -e .
```

### 4. Create a wrapper script (optional, for global access)

```bash
cat > ~/bin/ObsidianVideoNoteManager << 'EOF'
#!/bin/bash
source /path/to/ObsidianVideoNotesManager/venv/bin/activate
exec python3 /path/to/ObsidianVideoNotesManager/main.py "$@"
EOF
chmod +x ~/bin/ObsidianVideoNoteManager
```

Make sure `~/bin` is in your `PATH`:
```bash
echo 'export PATH="$HOME/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc
```

---

## Google OAuth2 Setup

The app uses the **YouTube Data API v3** to fetch playlist and video metadata. A Google Cloud project with OAuth2 credentials is required.

### Steps

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project
3. Enable the **YouTube Data API v3**
4. Go to **APIs & Services → Credentials → Create OAuth 2.0 Client ID**
   - Application type: **Desktop app**
5. Download `client_secrets.json`
6. Go to **OAuth consent screen → Test users** and add your Gmail address

### Store credentials securely (recommended)

Instead of keeping `client_secrets.json` on disk, store it in the system keyring:

```bash
python3 - << 'EOF'
import keyring, json
from pathlib import Path
f = next(Path(".").glob("client_secrets*.json"), None)
if f:
    keyring.set_password("yt_playlist_dl", "client_secrets", f.read_text())
    print(f"✓ Stored in keyring: {f.name}")
EOF

# Then safely delete the file
rm client_secrets*.json
```

Alternatively, set an environment variable:
```bash
export YT_DL_SECRETS=/path/to/client_secrets.json
```

---

## Configuration

Copy the example config and adapt it to your setup:

```bash
cp config.toml.example config.toml
```

```toml
log_level = "INFO"

[obsidian]
vault_path              = "~/Documents/MyVault"
notes_subfolder         = "inbox"
overwrite_notes         = false
default_tags            = ["literature-note", "resource"]
default_project         = ""        # leave empty to be prompted at runtime
default_task            = ""
default_knowledge_index = ""        # leave empty to be prompted at runtime

[download]
output_dir         = "~/Videos/YouTube"
article_output_dir = "~/Documents/Articles"
browser            = ""             # unused — android_vr client bypasses cookies
cookiefile         = ""             # optional: path to exported cookies.txt
max_filesize       = ""             # e.g. "500m" or "2g"
max_duration       = 0              # seconds, 0 = unlimited
embed_metadata     = true
write_subtitles    = false
video_format       = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
```

---

## Usage

### Interactive mode

```bash
ObsidianVideoNoteManager
# or
python3 main.py
```

The interactive menu guides you through all available actions:

```
? What would you like to do?
  📋  Download playlists
  🎬  Download a video (URL)
  📰  Download a web article (URL)
  📋  Download from URL file
  🔄  Re-download known videos
  📄  List my playlists
  📊  Download statistics
  🔍  Resource status
  🗑️   Flush database
  🔑  Revoke credentials
  ❌  Quit
```

### CLI commands

```bash
# Download a single video
python3 main.py download video "https://youtube.com/watch?v=xxx"
python3 main.py download video "https://youtube.com/watch?v=xxx" --force --no-enrich

# Download playlists (interactive selection if no ID given)
python3 main.py download playlist
python3 main.py download playlist PLxxxxxxxx --max-videos 50

# Download a web article
python3 main.py download article "https://example.com/post"
python3 main.py download article "https://example.com/post" --output ~/Articles

# Download from a URL file (YouTube + web articles auto-detected)
python3 main.py download urlfile ./urls.txt
python3 main.py download urlfile ./urls.txt --output-video ~/Videos --output-article ~/Articles

# List playlists
python3 main.py playlists

# Show download statistics
python3 main.py stats

# Show resource status (with optional retry for failed downloads)
python3 main.py resources
python3 main.py resources --status failed
python3 main.py resources --retry

# Revoke OAuth2 credentials
python3 main.py revoke
```

### URL file format

Create a plain text file with one URL per line. Lines starting with `#` are ignored. YouTube and web URLs are automatically detected.

```
# YouTube videos
https://www.youtube.com/watch?v=dQw4w9WgXcQ
https://www.youtube.com/watch?v=J4oO7PT_nzQ

# Web articles
https://learn.sparkfun.com/tutorials/serial-communication
https://cturt.github.io/mast1c0re.html
```

---

## Obsidian Note Format

Each downloaded video generates a Markdown note with the following structure:

```yaml
---
Author: Channel Name
URL: https://www.youtube.com/watch?v=xxx
publication: 2024-03-15
lecture: 2026-05-04
tags:
  - literature-note
  - resource
  - MachineLearning
download: /home/user/Videos/YouTube/Video Title.mp4
Knowledge-index: "[[202409201035]]"
Project: "[[202409201035 - My Thesis]]"
Task: "[[202409201035d - V8 Analysis]]"
---

# Video Title

Description of the video…
```

If a download fails, the note is still created with `download: KO` so you can track and retry failed resources from Obsidian.

---

## Known Issues

### YouTube n-challenge (JS runtime warning)

Recent versions of yt-dlp require a JavaScript runtime to resolve YouTube's `n` parameter challenge. This app uses the `android_vr` client to bypass this requirement without a JS runtime.

If you see:
```
WARNING: [youtube] No supported JavaScript runtime could be found.
```

This is expected behavior — the `android_vr` client works around it. If downloads fail for a specific video, try:

```bash
yt-dlp --extractor-args "youtube:player_client=android_vr" "URL"
```

### Some videos require sign-in

Videos that require age verification or regional restrictions may fail. Export cookies from Firefox using [Get cookies.txt LOCALLY](https://addons.mozilla.org/firefox/addon/get-cookiestxt-locally/) and set:

```toml
[download]
cookiefile = "~/cookies.txt"
```

### AV1 codec not playing in VLC

Some videos are only available in AV1 format. Install the codec or force H264 in config:

```toml
video_format = "bestvideo[vcodec^=avc1]+bestaudio[acodec^=mp4a]/best"
```

---

## Project Structure

```
ObsidianVideoNotesManager/
├── main.py                    # Entry point
├── config.toml                # Local configuration (gitignored)
├── state.db                   # SQLite download state (gitignored)
├── requirements.txt
├── pyproject.toml
└── yt_playlist_dl/
    ├── cli.py                 # Typer CLI + interactive menu
    ├── config.py              # TOML configuration parser
    ├── downloader.py          # yt-dlp wrapper + state management
    ├── obsidian.py            # Obsidian note generator
    ├── youtube.py             # YouTube Data API v3 client
    ├── article.py             # Web article downloader
    ├── auth.py                # Google OAuth2 + keyring
    ├── state.py               # SQLite state manager
    └── logger.py              # Rich logging setup
```

---

## Contributing

Issues and pull requests are welcome. Please open an issue before submitting large changes.

---

## License

MIT — see [LICENSE](LICENSE).
