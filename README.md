# Buffered DVR Player

A desktop HLS livestream player for Windows/Linux built with **PySide6**, **Playwright**, and Qt's **FFmpeg** multimedia backend.

It opens a normal livestream webpage in Chromium, discovers the underlying HLS media playlist, verifies that it contains real MPEG-TS video, downloads segments into a rolling local buffer, and exposes them to `QMediaPlayer` through a local HTTP stream. This gives the player DVR-style controls such as pause, rewind, jump forward, playback speed, screenshots, and a safe jump-to-live position.

## Features

- Automatic HLS media-playlist discovery from a normal webpage URL
- Browser-session-aware fetching using Playwright/Chromium
- Protection against accidentally selecting WebP/JPEG/PNG/GIF thumbnail or preview playlists
- Strong MPEG-TS segment validation
- Configurable initial DVR buffer (3 minutes by default)
- Pause, rewind, fast-forward, timeline seeking, and jump-to-live
- Playback speeds from 0.5x to 2x
- Screenshot capture
- Fullscreen mode with auto-hiding controls
- Rolling cleanup of old buffered segments
- Automatic session refresh when HLS URLs/tokens expire

## Requirements

- Python 3.10+
- PySide6
- Playwright
- Chromium installed through Playwright

## Installation

Clone the repository:

```bash
git clone https://github.com/qasimrao789/buffered-dvr-player.git
cd buffered-dvr-player
```

Create and activate a virtual environment (recommended):

### Windows

```bat
py -m venv .venv
.venv\Scripts\activate
```

### Linux/macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install Python dependencies:

```bash
pip install -r requirements.txt
```

Install Chromium for Playwright:

```bash
python -m playwright install chromium
```

## Run

Start the player:

```bash
python dvr_player.py
```

Paste the **normal livestream webpage URL** into the dialog. You can also pass it directly:

```bash
python dvr_player.py "https://example.com/watch/live"
```

The Chromium window may occasionally require one manual click on the website's Play button before the media playlist becomes available.

## Default controls

| Action | Control |
| --- | --- |
| Play / pause | `Space` |
| Back 10 seconds | `Left Arrow` |
| Forward 10 seconds | `Right Arrow` |
| Back 30 seconds | `J` |
| Forward 30 seconds | `L` |
| Fullscreen | `F` |
| Mute | `M` |
| Screenshot | `Ctrl+S` |
| Exit fullscreen | `Esc` |

The **LIVE** button jumps close to the newest fully downloaded data while deliberately staying a few seconds behind the exact edge to reduce stalls.

## Buffer behavior

By default the application waits until it has a **180-second contiguous buffer** before starting playback. Old data is removed as playback advances while keeping a safety window behind the playhead.

The main settings are near the top of `dvr_player.py`:

```python
INITIAL_BUFFER_SECONDS = 180
KEEP_BEHIND_SECONDS = 120
CLEANUP_SAFETY_SECONDS = 30
DOWNLOAD_WORKERS = 2
LIVE_SAFETY_SECONDS = 8
```

Buffered transport-stream segments are stored under:

```text
~/Videos/Buffered Livestream/
```

and are deleted on normal exit by default. Screenshots are stored under:

```text
~/Pictures/Buffered Livestream Screenshots/
```

## Supported streams and limitations

The current implementation is designed for **unencrypted MPEG-TS HLS** media playlists. It intentionally rejects encrypted HLS and image/thumbnail playlists. CMAF/fMP4 HLS is not currently supported.

Seeking occurs at HLS segment boundaries, so exact seek precision depends on the stream's segment duration.

## Security note

The Playwright Chromium instance is launched with relaxed browser security options so it can reproduce the webpage's authenticated media requests. It also uses a persistent profile directory:

```text
~/stream-player-insecure-profile
```

Treat that Chromium instance as a dedicated media-capture browser and do not use it for unrelated sensitive browsing.

## Responsible use

Use the player only with streams you are authorized to access and in accordance with the website's terms and applicable law.

## Project status

This is an experimental personal project. Stream providers can change their HLS delivery behavior at any time, so additional playlist formats and edge cases may require future updates.
