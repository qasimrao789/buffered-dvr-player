# StreamShift DVR — Live API Edition

A desktop DVR player that uses the Streamed public API as the event browser, then resolves the best playable quality across the selected event's available HD stream embeds.

## Daily workflow

1. Run `python dvr_player.py`.
2. The app calls `https://streamed.pk/api/matches/live` and lists the events that are currently live.
3. Search/filter the list and double-click an event (or select it and press **Watch**).
4. The app calls the documented `/api/stream/{source}/{id}` endpoint for every source attached to that event.
5. Every API stream marked `hd: true` is tested first. Each embed is opened in Chromium, its HLS traffic is inspected, image/thumbnail playlists are rejected, and real MPEG-TS renditions are verified from their bytes.
6. The highest verified compatible quality across all HD embeds wins. SD embeds are tried only if every HD embed fails.
7. The chosen live stream is downloaded into the existing rolling local DVR buffer.
8. Playback begins after a 180-second contiguous buffer is ready.

## DVR behavior

- 180-second initial contiguous buffer
- pause/resume
- rewind / fast-forward
- timeline seeking
- LIVE button with a safety offset
- speed control
- screenshot capture
- fullscreen controls
- rolling deletion of old segments
- browser-session refresh when stream tokens expire

## Important compatibility note

The current DVR transport concatenates MPEG-TS HLS segments and serves them locally to Qt. StreamShift therefore chooses the **highest verified MPEG-TS HLS quality it can actually ingest**. Image/trick-play playlists are rejected. CMAF/fMP4-only or encrypted renditions are not yet fed into this DVR transport.

This matters because an API stream can be labeled HD while an individual embed may expose a misleading high-resolution image playlist. StreamShift tests all HD embeds instead of trusting the first one.

## Install

Python 3.10+ is recommended.

```bat
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
```

Run:

```bat
python dvr_player.py
```

## Main settings

At the top of `dvr_player.py`:

```python
INITIAL_BUFFER_SECONDS = 180
KEEP_BEHIND_SECONDS = 120
CLEANUP_SAFETY_SECONDS = 30
DOWNLOAD_WORKERS = 2
SOURCE_PROBE_SECONDS = 7
```

Increasing `SOURCE_PROBE_SECONDS` gives slow-loading embeds longer to reveal their quality playlists, but makes the pre-buffer source-selection stage take longer.

## Responsible use

Use streams only where you are authorized to access them and follow the site's terms and applicable law.
