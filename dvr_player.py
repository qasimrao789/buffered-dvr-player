import os
os.environ.setdefault("QT_MEDIA_BACKEND", "ffmpeg")

import asyncio
import base64
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

from playwright.async_api import async_playwright

from PySide6.QtCore import QTimer, Qt, QUrl
from PySide6.QtGui import QCursor, QKeySequence, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)


# ============================================================
# USER SETTINGS
# ============================================================

INITIAL_BUFFER_SECONDS = 180
KEEP_BEHIND_SECONDS = 120
CLEANUP_SAFETY_SECONDS = 30
DOWNLOAD_WORKERS = 2

PLAYLIST_POLL_SECONDS = 1.0
INITIAL_PLAYLIST_TIMEOUT = 120
PLAYLIST_FETCH_TIMEOUT = 20
SEGMENT_FETCH_TIMEOUT = 180

RETRY_DELAY = 2
MAX_RETRY_DELAY = 15
CLEANUP_CHECK_SECONDS = 5

# When LIVE is pressed, stay a little behind the newest completely
# downloaded data instead of sitting on the exact edge.
LIVE_SAFETY_SECONDS = 8

PROFILE_DIR = Path.home() / "stream-player-insecure-profile"
OUTPUT_ROOT = Path.home() / "Videos" / "Buffered Livestream"
SCREENSHOT_DIR = Path.home() / "Pictures" / "Buffered Livestream Screenshots"

DELETE_SESSION_ON_EXIT = True


# ============================================================
# DATA TYPES
# ============================================================

@dataclass
class Segment:
    sequence: int
    url: str
    duration: float = 0.0
    range_start: int | None = None
    range_length: int | None = None


# ============================================================
# SHARED STATE
# ============================================================

class SharedState:
    def __init__(self):
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.stop_event = threading.Event()

        self.folder: Path | None = None

        self.segments: dict[int, Segment] = {}
        self.start_times: dict[int, float] = {}
        self.saved_files: dict[int, Path] = {}
        self.downloaded: set[int] = set()

        self.origin_sequence: int | None = None
        self.retained_floor_sequence: int | None = None
        self.latest_discovered_sequence: int | None = None
        self.target_duration = 4.0
        self.endlist_seen = False

        self.queue_size = 0
        self.inflight_count = 0

        self.status = "Starting..."
        self.error = ""

        self.playback_abs_seconds = 0.0
        self.player_started = False

        # Every new player source gets a new stream id. Old HTTP
        # connections exit as soon as the id no longer matches.
        self.active_stream_id = 0

    def set_status(self, text: str):
        with self.lock:
            self.status = text

    def set_error(self, text: str):
        with self.lock:
            self.error = text

    def _duration_locked(self, seq: int) -> float:
        segment = self.segments.get(seq)
        if segment is None:
            return self.target_duration
        return segment.duration if segment.duration > 0 else self.target_duration

    def rebuild_timeline_locked(self):
        if self.origin_sequence is None:
            return

        seq = self.origin_sequence
        t = 0.0
        self.start_times.clear()

        # HLS media sequences should be continuous. Stop at the first
        # metadata gap; later playlist refreshes can extend the chain.
        while seq in self.segments:
            self.start_times[seq] = t
            t += self._duration_locked(seq)
            seq += 1

    def register_segments(self, segments: list[Segment], target_duration: float):
        with self.condition:
            self.target_duration = target_duration or self.target_duration

            if segments and self.origin_sequence is None:
                first = min(s.sequence for s in segments)
                self.origin_sequence = first
                self.retained_floor_sequence = first

            for segment in segments:
                self.segments[segment.sequence] = segment

            if segments:
                newest = max(s.sequence for s in segments)
                if (
                    self.latest_discovered_sequence is None
                    or newest > self.latest_discovered_sequence
                ):
                    self.latest_discovered_sequence = newest

            self.rebuild_timeline_locked()
            self.condition.notify_all()

    def mark_saved(self, sequence: int, path: Path):
        with self.condition:
            self.saved_files[sequence] = path
            self.downloaded.add(sequence)
            self.condition.notify_all()

    def segment_start_locked(self, seq: int) -> float | None:
        return self.start_times.get(seq)

    def segment_end_locked(self, seq: int) -> float | None:
        start = self.start_times.get(seq)
        if start is None:
            return None
        return start + self._duration_locked(seq)

    def first_available_sequence_locked(self) -> int | None:
        available = [
            seq
            for seq, path in self.saved_files.items()
            if path.exists()
        ]
        return min(available) if available else None

    def contiguous_end_from_locked(self, start_seq: int | None) -> tuple[int | None, float]:
        if start_seq is None:
            return None, 0.0

        seq = start_seq
        last_seq = None
        end_time = self.start_times.get(seq, 0.0)

        while True:
            path = self.saved_files.get(seq)
            if (
                seq not in self.downloaded
                or path is None
                or not path.exists()
                or seq not in self.start_times
            ):
                break

            last_seq = seq
            end_time = self.start_times[seq] + self._duration_locked(seq)
            seq += 1

        return last_seq, end_time

    def initial_buffer_seconds(self) -> float:
        with self.lock:
            if self.origin_sequence is None:
                return 0.0
            start = self.start_times.get(self.origin_sequence, 0.0)
            _, end = self.contiguous_end_from_locked(self.origin_sequence)
            return max(0.0, end - start)

    def retained_floor_time_locked(self) -> float:
        seq = self.first_available_sequence_locked()
        if seq is None:
            return 0.0
        return self.start_times.get(seq, 0.0)

    def contiguous_download_end_locked(self) -> float:
        floor = self.first_available_sequence_locked()
        if floor is None:
            return 0.0
        _, end = self.contiguous_end_from_locked(floor)
        return end

    def latest_discovered_end_locked(self) -> float:
        seq = self.latest_discovered_sequence
        if seq is None:
            return 0.0
        end = self.segment_end_locked(seq)
        return end or 0.0

    def sequence_for_time_locked(self, target: float) -> int | None:
        if not self.start_times:
            return None

        available = sorted(
            seq
            for seq, path in self.saved_files.items()
            if path.exists() and seq in self.start_times
        )
        if not available:
            return None

        chosen = available[0]
        for seq in available:
            start = self.start_times[seq]
            end = start + self._duration_locked(seq)
            if start <= target < end:
                return seq
            if start <= target:
                chosen = seq
            else:
                break

        return chosen

    def snapshot(self):
        with self.lock:
            floor_time = self.retained_floor_time_locked()
            downloaded_end = self.contiguous_download_end_locked()
            live_end = self.latest_discovered_end_locked()
            playhead = self.playback_abs_seconds

            return {
                "status": self.status,
                "error": self.error,
                "queue": self.queue_size,
                "inflight": self.inflight_count,
                "saved": len(self.saved_files),
                "initial_buffer": self.initial_buffer_seconds(),
                "floor_time": floor_time,
                "downloaded_end": downloaded_end,
                "live_end": live_end,
                "playhead": playhead,
                "ahead": max(0.0, downloaded_end - playhead),
                "behind_live": max(0.0, live_end - playhead),
                "endlist": self.endlist_seen,
                "player_started": self.player_started,
            }


STATE = SharedState()


# ============================================================
# FILE / MPEG-TS HELPERS
# ============================================================

def make_session_folder() -> Path:
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    folder = OUTPUT_ROOT / stamp
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def looks_like_image(data: bytes) -> str | None:
    """Return a short image type name if the payload is obviously an image."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if data.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "GIF"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WebP"
    return None


def looks_like_mpeg_ts(data: bytes, packets_to_check: int = 12) -> bool:
    """Stricter MPEG-TS validation than the old three-sync-byte heuristic."""
    packet_size = 188
    minimum = packet_size * packets_to_check
    if len(data) < minimum:
        return False

    # A valid TS stream repeats the 0x47 sync byte every 188 bytes. Search
    # the first packet's worth of offsets so small leading junk is tolerated.
    max_offset = min(packet_size, len(data) - minimum + 1)
    for offset in range(max_offset):
        good = True
        for index in range(packets_to_check):
            pos = offset + index * packet_size
            if data[pos] != 0x47:
                good = False
                break

            # transport_error_indicator should normally be clear. This also
            # makes accidental 0x47 spacing matches in unrelated binary data
            # substantially less likely.
            if pos + 1 >= len(data) or (data[pos + 1] & 0x80):
                good = False
                break

        if good:
            return True

    return False


# ============================================================
# CHROMIUM FETCH
# ============================================================

async def browser_fetch(
    frame,
    url,
    *,
    range_start=None,
    range_length=None,
    want_text=False,
    timeout=60,
):
    range_header = None

    if range_start is not None and range_length is not None:
        end = range_start + range_length - 1
        range_header = f"bytes={range_start}-{end}"

    try:
        return await asyncio.wait_for(
            frame.evaluate(
                """
                async ({url, rangeHeader, wantText}) => {
                    try {
                        const headers = {"Accept": "*/*"};
                        if (rangeHeader) headers["Range"] = rangeHeader;

                        const response = await fetch(url, {
                            method: "GET",
                            headers,
                            credentials: "include",
                            cache: "no-store",
                            redirect: "follow",
                            referrer: location.href,
                            referrerPolicy: "strict-origin-when-cross-origin"
                        });

                        if (wantText) {
                            const text = await response.text();
                            return {
                                status: response.status,
                                url: response.url,
                                text,
                                error: null
                            };
                        }

                        const buffer = await response.arrayBuffer();
                        const blob = new Blob([buffer]);
                        const dataUrl = await new Promise((resolve, reject) => {
                            const reader = new FileReader();
                            reader.onload = () => resolve(reader.result);
                            reader.onerror = () => reject(reader.error);
                            reader.readAsDataURL(blob);
                        });

                        const comma = dataUrl.indexOf(",");
                        const b64 = comma >= 0 ? dataUrl.substring(comma + 1) : "";

                        return {
                            status: response.status,
                            url: response.url,
                            b64,
                            byteLength: buffer.byteLength,
                            contentType: response.headers.get("content-type") || "",
                            error: null
                        };
                    } catch (error) {
                        return {
                            status: -1,
                            error: (error?.name || "Error") + ": " +
                                   (error?.message || String(error))
                        };
                    }
                }
                """,
                {
                    "url": url,
                    "rangeHeader": range_header,
                    "wantText": want_text,
                },
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return {"status": -2, "error": f"Timeout after {timeout}s"}
    except Exception as exc:
        return {"status": -1, "error": f"{type(exc).__name__}: {exc}"}


# ============================================================
# HLS PARSER
# ============================================================

def parse_media_playlist(text: str, playlist_url: str):
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    media_sequence = 0
    target_duration = 4.0
    duration = 0.0

    range_length = None
    range_start = None
    implicit_range = False

    previous_uri = None
    previous_end = None

    segment_index = 0
    segments = []
    endlist = False

    for line in lines:
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                media_sequence = int(line.split(":", 1)[1])
            except Exception:
                pass
            continue

        if line.startswith("#EXT-X-TARGETDURATION:"):
            try:
                target_duration = float(line.split(":", 1)[1])
            except Exception:
                pass
            continue

        if line.startswith("#EXTINF:"):
            try:
                duration = float(line.split(":", 1)[1].split(",", 1)[0])
            except Exception:
                duration = 0.0
            continue

        if line.startswith("#EXT-X-BYTERANGE:"):
            value = line.split(":", 1)[1]
            if "@" in value:
                length_text, start_text = value.split("@", 1)
                range_length = int(length_text)
                range_start = int(start_text)
                implicit_range = False
            else:
                range_length = int(value)
                range_start = None
                implicit_range = True
            continue

        if line.startswith("#EXT-X-KEY:"):
            if "METHOD=NONE" not in line.upper():
                raise RuntimeError(
                    "Encrypted HLS detected. This version does not handle encrypted segments."
                )
            continue

        if line.startswith("#EXT-X-ENDLIST"):
            endlist = True
            continue

        if line.startswith("#"):
            continue

        absolute_url = urljoin(playlist_url, line)
        actual_start = range_start

        if implicit_range and range_length is not None:
            if previous_uri == absolute_url and previous_end is not None:
                actual_start = previous_end
            else:
                actual_start = 0

        sequence = media_sequence + segment_index

        segments.append(
            Segment(
                sequence=sequence,
                url=absolute_url,
                duration=duration,
                range_start=actual_start,
                range_length=range_length,
            )
        )

        if actual_start is not None and range_length is not None:
            previous_uri = absolute_url
            previous_end = actual_start + range_length
        else:
            previous_uri = None
            previous_end = None

        segment_index += 1
        duration = 0.0
        range_length = None
        range_start = None
        implicit_range = False

    return segments, target_duration, endlist


# ============================================================
# DOWNLOADER ENGINE
# ============================================================

class DownloaderEngine:
    def __init__(self, webpage_url: str, state: SharedState):
        self.webpage_url = webpage_url
        self.state = state

        self.page = None
        self.media_url = None
        self.media_frame = None

        self.accept_session_updates = False
        self.session_event = asyncio.Event()
        self.refresh_event = asyncio.Event()
        self.refresh_lock = asyncio.Lock()

        self.queue = asyncio.PriorityQueue()
        self.queued = set()
        self.inflight = set()
        self.downloaded = set()
        self.retry_count = {}

        # Network response callbacks can arrive at nearly the same time. Only
        # probe one playlist candidate at once so an image/thumbnail playlist
        # cannot race a valid video playlist and overwrite it.
        self.candidate_lock = asyncio.Lock()

    @staticmethod
    def playlist_is_obviously_nonvideo(text: str, url: str) -> bool:
        upper = text.upper()
        lower_url = url.lower()

        # HLS trick-play/image playlists are not the normal A/V media stream.
        if "#EXT-X-I-FRAMES-ONLY" in upper:
            return True
        if "#EXT-X-IMAGES-ONLY" in upper or "#EXT-X-TILES" in upper:
            return True

        # Fast-path rejection for common preview/storyboard playlist names.
        suspicious_words = (
            "thumbnail", "thumbnails", "storyboard", "preview",
            "trickplay", "trick-play", "sprite",
        )
        return any(word in lower_url for word in suspicious_words)

    async def probe_media_playlist(self, frame, playlist_url: str, text: str):
        """Return parsed playlist info only if recent payloads are real MPEG-TS."""
        if self.playlist_is_obviously_nonvideo(text, playlist_url):
            print(f"[HLS] Rejecting obvious preview/image playlist: {playlist_url}")
            return None

        try:
            segments, target_duration, endlist = parse_media_playlist(text, playlist_url)
        except Exception as exc:
            print(f"[HLS] Rejecting playlist {playlist_url}: {exc}")
            return None

        if not segments:
            return None

        # Probe newest entries first: older live segments may already have
        # expired from the CDN even though they remain in the current playlist.
        candidates = list(reversed(segments[-3:]))
        for segment in candidates:
            result = await browser_fetch(
                frame,
                segment.url,
                range_start=segment.range_start,
                range_length=segment.range_length,
                timeout=min(30, SEGMENT_FETCH_TIMEOUT),
            )
            status = result.get("status", -1)
            if status not in (200, 206):
                print(
                    f"[HLS] Probe failed seq={segment.sequence} status={status} "
                    f"url={segment.url}"
                )
                continue

            b64 = result.get("b64", "")
            if not b64:
                continue

            try:
                data = base64.b64decode(b64)
            except Exception:
                continue

            image_type = looks_like_image(data)
            if image_type:
                print(
                    f"[HLS] Rejecting {playlist_url}: seq {segment.sequence} "
                    f"is {image_type}, not MPEG-TS ({segment.url})"
                )
                return None

            if looks_like_mpeg_ts(data):
                print(
                    f"[HLS] Accepted MPEG-TS playlist: {playlist_url} "
                    f"(verified seq {segment.sequence}, {len(data)} bytes)"
                )
                return segments, target_duration, endlist

            first = data[:16].hex(" ")
            ctype = result.get("contentType", "")
            print(
                f"[HLS] Probe seq={segment.sequence} was not MPEG-TS "
                f"content-type={ctype!r} first16={first} url={segment.url}"
            )

        return None

    async def inspect_response(self, response):
        if not self.accept_session_updates or self.session_event.is_set():
            return
        if ".m3u8" not in response.url.lower():
            return
        if not (200 <= response.status < 300):
            return

        try:
            text = await response.text()
        except Exception:
            return

        # Master playlists are useful to the webpage, but this downloader needs
        # a concrete media playlist containing segments.
        if "#EXT-X-STREAM-INF" in text:
            return
        if "#EXTINF" not in text and "#EXT-X-MEDIA-SEQUENCE" not in text:
            return

        try:
            frame = response.request.frame
        except Exception:
            return

        async with self.candidate_lock:
            if not self.accept_session_updates or self.session_event.is_set():
                return

            print(f"[HLS] Probing candidate playlist: {response.url}")
            verified = await self.probe_media_playlist(frame, response.url, text)
            if verified is None:
                return

            self.media_url = response.url
            self.media_frame = frame
            self.state.set_status(
                f"Verified MPEG-TS playlist: {response.url.split('/')[-1]}"
            )
            self.session_event.set()

    def response_event(self, response):
        asyncio.create_task(self.inspect_response(response))

    async def encourage_playback(self):
        for frame in self.page.frames:
            try:
                await frame.evaluate(
                    """
                    () => {
                        for (const video of document.querySelectorAll('video')) {
                            video.muted = true;
                            video.volume = 0;
                            video.play().catch(() => {});
                        }
                    }
                    """
                )
            except Exception:
                pass

    async def pause_players(self):
        for frame in self.page.frames:
            try:
                await frame.evaluate(
                    """
                    () => {
                        for (const video of document.querySelectorAll('video')) {
                            video.muted = true;
                            video.volume = 0;
                            video.pause();
                        }
                    }
                    """
                )
            except Exception:
                pass

    async def acquire_session(self, reason: str):
        async with self.refresh_lock:
            self.state.set_status(reason)
            self.accept_session_updates = True
            self.session_event.clear()

            try:
                await self.page.goto(
                    self.webpage_url,
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
            except Exception as exc:
                self.state.set_status(f"Page load warning: {exc}")

            for _ in range(10):
                await self.encourage_playback()
                if self.session_event.is_set():
                    break
                await asyncio.sleep(1)

            if not self.session_event.is_set():
                self.state.set_status("Click Play once in the Chromium window...")

            try:
                await asyncio.wait_for(
                    self.session_event.wait(),
                    timeout=INITIAL_PLAYLIST_TIMEOUT,
                )
            finally:
                self.accept_session_updates = False

            await asyncio.sleep(1)
            await self.pause_players()
            self.refresh_event.clear()
            self.state.set_status("Downloader active — website player paused")

    async def register_segments(self, segments, target_duration):
        self.state.register_segments(segments, target_duration)

        floor = None
        with self.state.lock:
            floor = self.state.retained_floor_sequence

        added = 0
        for segment in segments:
            seq = segment.sequence

            if floor is not None and seq < floor:
                continue

            if seq in self.downloaded or seq in self.queued or seq in self.inflight:
                continue

            await self.queue.put(seq)
            self.queued.add(seq)
            added += 1

        if added:
            self.update_queue_stats()

    def update_queue_stats(self):
        with self.state.lock:
            self.state.queue_size = self.queue.qsize()
            self.state.inflight_count = len(self.inflight)

    async def playlist_poller(self):
        failures = 0

        while not self.state.stop_event.is_set():
            if self.refresh_event.is_set():
                try:
                    await self.acquire_session("Refreshing browser session...")
                    failures = 0
                except Exception as exc:
                    self.state.set_status(f"Session refresh failed: {exc}")
                    await asyncio.sleep(5)
                continue

            if self.media_frame is None or self.media_url is None:
                await asyncio.sleep(1)
                continue

            result = await browser_fetch(
                self.media_frame,
                self.media_url,
                want_text=True,
                timeout=PLAYLIST_FETCH_TIMEOUT,
            )

            status = result.get("status", -1)

            if status in (401, 403, 410):
                self.refresh_event.set()
                await asyncio.sleep(1)
                continue

            if status < 0:
                failures += 1
                self.state.set_status(f"Playlist fetch: {result.get('error', '')}")
                if failures >= 3:
                    self.refresh_event.set()
                    failures = 0
                await asyncio.sleep(2)
                continue

            if not (200 <= status < 300):
                failures += 1
                if failures >= 3:
                    self.refresh_event.set()
                    failures = 0
                await asyncio.sleep(2)
                continue

            failures = 0

            try:
                segments, target_duration, endlist = parse_media_playlist(
                    result.get("text", ""),
                    self.media_url,
                )
            except Exception as exc:
                self.state.set_error(f"Playlist parse error: {exc}")
                self.state.stop_event.set()
                return

            await self.register_segments(segments, target_duration)

            if endlist:
                with self.state.condition:
                    self.state.endlist_seen = True
                    self.state.condition.notify_all()
                self.state.set_status("Livestream ended — finishing buffered data")
                return

            await asyncio.sleep(PLAYLIST_POLL_SECONDS)

    async def requeue_later(self, sequence: int, delay: int):
        await asyncio.sleep(delay)
        if self.state.stop_event.is_set():
            return

        with self.state.lock:
            floor = self.state.retained_floor_sequence

        if floor is not None and sequence < floor:
            return

        if sequence in self.downloaded or sequence in self.queued or sequence in self.inflight:
            return

        await self.queue.put(sequence)
        self.queued.add(sequence)
        self.update_queue_stats()

    async def download_segment(self, sequence: int, worker_number: int):
        with self.state.lock:
            segment = self.state.segments.get(sequence)
            floor = self.state.retained_floor_sequence

        if floor is not None and sequence < floor:
            return True

        if segment is None or self.media_frame is None:
            return False

        result = await browser_fetch(
            self.media_frame,
            segment.url,
            range_start=segment.range_start,
            range_length=segment.range_length,
            timeout=SEGMENT_FETCH_TIMEOUT,
        )

        status = result.get("status", -1)

        if status in (401, 403, 410):
            self.refresh_event.set()
            return False

        if status < 0:
            self.state.set_status(
                f"Worker {worker_number}: seq {sequence} — {result.get('error', '')}"
            )
            return False

        if status not in (200, 206):
            return False

        b64 = result.get("b64", "")
        if not b64:
            return False

        try:
            data = base64.b64decode(b64)
        except Exception:
            return False

        if segment.range_length is not None and len(data) != segment.range_length:
            return False

        image_type = looks_like_image(data)
        if image_type:
            self.state.set_status(
                f"Worker {worker_number}: seq {sequence} returned {image_type}; "
                "refreshing media session"
            )
            print(
                f"[HLS] Segment {sequence} unexpectedly returned {image_type}: "
                f"{segment.url}"
            )
            # A CDN/session can start returning a placeholder image after a
            # token expires. Force a fresh webpage/media-playlist discovery.
            self.refresh_event.set()
            return False

        if not looks_like_mpeg_ts(data):
            first = data[:16].hex(" ")
            ctype = result.get("contentType", "")
            self.state.set_status(
                f"Worker {worker_number}: seq {sequence} was not MPEG-TS"
            )
            print(
                f"[HLS] Invalid segment seq={sequence} content-type={ctype!r} "
                f"first16={first} url={segment.url}"
            )
            return False

        path = self.state.folder / f"segment_{sequence:010d}.ts"
        path.write_bytes(data)

        self.downloaded.add(sequence)
        self.retry_count.pop(sequence, None)
        self.state.mark_saved(sequence, path)
        self.state.set_status(
            f"Saved seq {sequence} ({len(data) / 1024 / 1024:.1f} MB)"
        )
        return True

    async def worker(self, worker_number: int):
        while not self.state.stop_event.is_set():
            try:
                sequence = await asyncio.wait_for(self.queue.get(), timeout=1)
            except asyncio.TimeoutError:
                continue

            self.queued.discard(sequence)

            if sequence in self.downloaded:
                self.queue.task_done()
                self.update_queue_stats()
                continue

            self.inflight.add(sequence)
            self.update_queue_stats()

            try:
                success = await self.download_segment(sequence, worker_number)
            finally:
                self.inflight.discard(sequence)
                self.queue.task_done()
                self.update_queue_stats()

            if success:
                continue

            attempts = self.retry_count.get(sequence, 0) + 1
            self.retry_count[sequence] = attempts
            delay = min(MAX_RETRY_DELAY, RETRY_DELAY * attempts)
            asyncio.create_task(self.requeue_later(sequence, delay))

    async def cleanup_task(self):
        while not self.state.stop_event.is_set():
            await asyncio.sleep(CLEANUP_CHECK_SECONDS)

            with self.state.lock:
                if not self.state.player_started:
                    continue

                threshold = (
                    self.state.playback_abs_seconds
                    - KEEP_BEHIND_SECONDS
                    - CLEANUP_SAFETY_SECONDS
                )

                if threshold <= 0:
                    continue

                deletable = []
                for seq, path in list(self.state.saved_files.items()):
                    end = self.state.segment_end_locked(seq)
                    if end is not None and end < threshold:
                        deletable.append((seq, path))

            if not deletable:
                continue

            deleted = 0
            deleted_bytes = 0
            max_deleted_seq = None

            for seq, path in deletable:
                try:
                    if path.exists():
                        deleted_bytes += path.stat().st_size
                        path.unlink()

                    with self.state.condition:
                        self.state.saved_files.pop(seq, None)
                        self.state.downloaded.discard(seq)
                        if max_deleted_seq is None or seq > max_deleted_seq:
                            max_deleted_seq = seq
                        self.state.condition.notify_all()
                    deleted += 1
                except Exception:
                    pass

            if max_deleted_seq is not None:
                with self.state.lock:
                    next_floor = max_deleted_seq + 1
                    if (
                        self.state.retained_floor_sequence is None
                        or next_floor > self.state.retained_floor_sequence
                    ):
                        self.state.retained_floor_sequence = next_floor

            if deleted:
                self.state.set_status(
                    f"Cleanup: deleted {deleted} old segments "
                    f"({deleted_bytes / 1024 / 1024:.1f} MB)"
                )

    async def run(self):
        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                str(PROFILE_DIR),
                headless=False,
                viewport=None,
                bypass_csp=True,
                args=[
                    "--autoplay-policy=no-user-gesture-required",
                    "--disable-background-timer-throttling",
                    "--disable-backgrounding-occluded-windows",
                    "--disable-renderer-backgrounding",
                    "--disable-web-security",
                    "--disable-features=IsolateOrigins,site-per-process",
                ],
            )

            self.page = context.pages[0] if context.pages else await context.new_page()
            self.page.on("response", self.response_event)

            await self.acquire_session("Opening livestream...")

            tasks = [
                asyncio.create_task(self.playlist_poller()),
                asyncio.create_task(self.cleanup_task()),
            ]

            for worker_number in range(1, DOWNLOAD_WORKERS + 1):
                tasks.append(asyncio.create_task(self.worker(worker_number)))

            try:
                while not self.state.stop_event.is_set():
                    await asyncio.sleep(0.5)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                try:
                    await context.close()
                except Exception:
                    pass


class DownloaderThread(threading.Thread):
    def __init__(self, webpage_url: str, state: SharedState):
        super().__init__(daemon=True)
        self.webpage_url = webpage_url
        self.state = state

    def run(self):
        try:
            engine = DownloaderEngine(self.webpage_url, self.state)
            asyncio.run(engine.run())
        except Exception as exc:
            self.state.set_error(f"Downloader stopped: {type(exc).__name__}: {exc}")
            self.state.stop_event.set()


# ============================================================
# LOCAL CONTINUOUS MPEG-TS SERVER
# ============================================================

class StreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path != "/stream.ts":
            self.send_error(404)
            return

        query = parse_qs(parsed.query)
        try:
            start_seq = int(query.get("start", [""])[0])
            stream_id = int(query.get("sid", [""])[0])
        except Exception:
            self.send_error(400)
            return

        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Pragma", "no-cache")
        self.end_headers()

        seq = start_seq

        try:
            while not STATE.stop_event.is_set():
                with STATE.condition:
                    if stream_id != STATE.active_stream_id:
                        return

                    path = STATE.saved_files.get(seq)
                    endlist = STATE.endlist_seen
                    latest = STATE.latest_discovered_sequence

                    while (
                        (path is None or not path.exists())
                        and not STATE.stop_event.is_set()
                        and stream_id == STATE.active_stream_id
                    ):
                        if endlist and latest is not None and seq > latest:
                            return
                        STATE.condition.wait(timeout=0.75)
                        path = STATE.saved_files.get(seq)
                        endlist = STATE.endlist_seen
                        latest = STATE.latest_discovered_sequence

                    if STATE.stop_event.is_set() or stream_id != STATE.active_stream_id:
                        return

                    if path is None or not path.exists():
                        continue

                with path.open("rb") as source:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)

                self.wfile.flush()
                seq += 1

        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return


class LocalStreamServer:
    def __init__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), StreamHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()

    def shutdown(self):
        try:
            self.server.shutdown()
        except Exception:
            pass


# ============================================================
# MODERN PLAYER UI
# ============================================================

def fmt_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


class VideoWidget(QVideoWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.on_double_click = None

    def mouseDoubleClickEvent(self, event):
        if self.on_double_click:
            self.on_double_click()
        super().mouseDoubleClickEvent(event)


class DVRPlayer(QMainWindow):
    def __init__(self, state: SharedState, server: LocalStreamServer):
        super().__init__()
        self.state = state
        self.server = server

        self.stream_base_seconds = 0.0
        self.position_origin_ms = None
        self.current_abs_seconds = 0.0
        self.user_dragging_slider = False
        self.started_once = False
        self.restart_pending = False

        # Fullscreen UI behavior. In fullscreen the controls appear when the
        # mouse moves (or sits near the bottom edge), then auto-hide after a
        # short idle period.
        self.fullscreen_controls_delay = 1.8
        self.fullscreen_reveal_zone_px = 120
        self._fullscreen_last_activity = time.monotonic()
        self._last_mouse_global_pos = None

        self.setWindowTitle("Buffered DVR Player")
        self.resize(1280, 800)
        self.setMinimumSize(850, 520)

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(0.8)
        self.player.setAudioOutput(self.audio)

        self.video = VideoWidget(self)
        self.video.on_double_click = self.toggle_fullscreen
        self.video.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
        self.player.setVideoOutput(self.video)

        self.build_ui()
        self.install_shortcuts()

        self.player.positionChanged.connect(self.on_position_changed)
        self.player.playbackStateChanged.connect(self.on_playback_state)
        self.player.mediaStatusChanged.connect(self.on_media_status)
        self.player.errorOccurred.connect(self.on_player_error)

        self.ui_timer = QTimer(self)
        self.ui_timer.timeout.connect(self.refresh_ui)
        self.ui_timer.start(250)

        # A lightweight timer is more reliable than depending on mouse-move
        # events from QVideoWidget (which can consume them internally).
        self.fullscreen_ui_timer = QTimer(self)
        self.fullscreen_ui_timer.timeout.connect(self.update_fullscreen_ui)
        self.fullscreen_ui_timer.start(120)

    def build_ui(self):
        root = QWidget(self)
        self.setCentralWidget(root)

        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self.video, 1)

        panel = QFrame()
        panel.setObjectName("controlsPanel")
        self.controls_panel = panel
        controls = QVBoxLayout(panel)
        controls.setContentsMargins(14, 10, 14, 12)
        controls.setSpacing(8)

        status_row = QHBoxLayout()
        self.status_label = QLabel("Building buffer...")
        self.status_label.setObjectName("status")
        self.metrics_label = QLabel("Buffer: 0s | Behind live: --")
        self.metrics_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        status_row.addWidget(self.status_label, 1)
        status_row.addWidget(self.metrics_label)
        controls.addLayout(status_row)

        self.timeline = QSlider(Qt.Orientation.Horizontal)
        self.timeline.setRange(0, 1000)
        self.timeline.setValue(0)
        self.timeline.sliderPressed.connect(self.on_slider_pressed)
        self.timeline.sliderReleased.connect(self.on_slider_released)
        controls.addWidget(self.timeline)

        row = QHBoxLayout()
        row.setSpacing(8)

        self.play_btn = QPushButton("▶")
        self.play_btn.setFixedWidth(44)
        self.play_btn.clicked.connect(self.toggle_play_pause)

        self.back_btn = QPushButton("↶ 10s")
        self.back_btn.clicked.connect(lambda: self.jump_relative(-10))

        self.forward_btn = QPushButton("10s ↷")
        self.forward_btn.clicked.connect(lambda: self.jump_relative(10))

        self.live_btn = QPushButton("LIVE")
        self.live_btn.setObjectName("liveButton")
        self.live_btn.clicked.connect(self.go_live)

        self.time_label = QLabel("00:00 / 00:00")
        self.time_label.setMinimumWidth(150)

        self.mute_btn = QPushButton("🔊")
        self.mute_btn.setFixedWidth(44)
        self.mute_btn.clicked.connect(self.toggle_mute)

        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(80)
        self.volume.setFixedWidth(120)
        self.volume.valueChanged.connect(lambda value: self.audio.setVolume(value / 100.0))

        self.speed = QComboBox()
        for label, value in [
            ("0.5×", 0.5),
            ("0.75×", 0.75),
            ("1×", 1.0),
            ("1.25×", 1.25),
            ("1.5×", 1.5),
            ("2×", 2.0),
        ]:
            self.speed.addItem(label, value)
        self.speed.setCurrentText("1×")
        self.speed.currentIndexChanged.connect(self.change_speed)

        self.shot_btn = QPushButton("📷")
        self.shot_btn.setToolTip("Screenshot")
        self.shot_btn.clicked.connect(self.take_screenshot)

        self.pin_btn = QPushButton("📌")
        self.pin_btn.setCheckable(True)
        self.pin_btn.setToolTip("Always on top")
        self.pin_btn.toggled.connect(self.toggle_pin)

        self.fullscreen_btn = QPushButton("⛶")
        self.fullscreen_btn.setToolTip("Fullscreen")
        self.fullscreen_btn.clicked.connect(self.toggle_fullscreen)

        row.addWidget(self.play_btn)
        row.addWidget(self.back_btn)
        row.addWidget(self.forward_btn)
        row.addWidget(self.live_btn)
        row.addWidget(self.time_label)
        row.addStretch(1)
        row.addWidget(self.mute_btn)
        row.addWidget(self.volume)
        row.addWidget(self.speed)
        row.addWidget(self.shot_btn)
        row.addWidget(self.pin_btn)
        row.addWidget(self.fullscreen_btn)

        controls.addLayout(row)
        outer.addWidget(panel)

        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background: #0d0f12;
                color: #f2f3f5;
                font-family: Segoe UI, Arial;
                font-size: 13px;
            }
            QFrame#controlsPanel {
                background: #15181d;
                border-top: 1px solid #262b33;
            }
            QLabel#status {
                color: #b7bec8;
            }
            QPushButton {
                background: #232830;
                border: 1px solid #343b45;
                border-radius: 7px;
                padding: 7px 10px;
            }
            QPushButton:hover {
                background: #2d333d;
            }
            QPushButton:pressed {
                background: #1d2229;
            }
            QPushButton#liveButton {
                background: #b51d2a;
                border-color: #d12b39;
                font-weight: 600;
            }
            QPushButton#liveButton:hover {
                background: #d12b39;
            }
            QComboBox {
                background: #232830;
                border: 1px solid #343b45;
                border-radius: 7px;
                padding: 6px 9px;
                min-width: 68px;
            }
            QSlider::groove:horizontal {
                height: 5px;
                background: #323844;
                border-radius: 2px;
            }
            QSlider::sub-page:horizontal {
                background: #e0e3e8;
                border-radius: 2px;
            }
            QSlider::handle:horizontal {
                background: white;
                width: 14px;
                margin: -5px 0;
                border-radius: 7px;
            }
            """
        )

    def install_shortcuts(self):
        shortcuts = [
            ("Space", self.toggle_play_pause),
            ("Left", lambda: self.jump_relative(-10)),
            ("Right", lambda: self.jump_relative(10)),
            ("J", lambda: self.jump_relative(-30)),
            ("L", lambda: self.jump_relative(30)),
            ("F", self.toggle_fullscreen),
            ("M", self.toggle_mute),
            ("Ctrl+S", self.take_screenshot),
            ("Escape", self.exit_fullscreen),
        ]
        for key, fn in shortcuts:
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(fn)

    def current_absolute_position(self) -> float:
        return self.current_abs_seconds

    def start_from_time(self, target_seconds: float):
        with self.state.condition:
            floor = self.state.retained_floor_time_locked()
            end = self.state.contiguous_download_end_locked()

            if end <= floor:
                return

            target_seconds = max(floor, min(target_seconds, max(floor, end - 0.1)))
            seq = self.state.sequence_for_time_locked(target_seconds)
            if seq is None:
                return

            base = self.state.start_times.get(seq, target_seconds)
            self.state.active_stream_id += 1
            sid = self.state.active_stream_id
            self.state.player_started = True
            self.state.playback_abs_seconds = base
            self.state.condition.notify_all()

        self.stream_base_seconds = base
        self.current_abs_seconds = base
        self.position_origin_ms = None

        url = QUrl(
            f"http://127.0.0.1:{self.server.port}/stream.ts?start={seq}&sid={sid}"
        )

        self.player.stop()
        self.player.setSource(url)
        self.player.setPlaybackRate(float(self.speed.currentData() or 1.0))
        self.player.play()
        self.started_once = True

    def maybe_autostart(self, snap):
        if self.started_once:
            return
        if snap["initial_buffer"] < INITIAL_BUFFER_SECONDS:
            return

        with self.state.lock:
            if self.state.origin_sequence is None:
                return
            start = self.state.start_times.get(self.state.origin_sequence, 0.0)

        self.start_from_time(start)

    def on_position_changed(self, position_ms: int):
        if not self.started_once:
            return

        if self.position_origin_ms is None:
            self.position_origin_ms = position_ms

        relative_ms = max(0, position_ms - self.position_origin_ms)
        current = self.stream_base_seconds + relative_ms / 1000.0
        self.current_abs_seconds = current

        with self.state.lock:
            self.state.playback_abs_seconds = current

    def on_playback_state(self, state):
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self.play_btn.setText("⏸")
        else:
            self.play_btn.setText("▶")

    def on_media_status(self, status):
        if status == QMediaPlayer.MediaStatus.StalledMedia:
            self.status_label.setText("Waiting for the next queued segment…")
        elif status == QMediaPlayer.MediaStatus.BufferingMedia:
            self.status_label.setText("Buffering local DVR data…")
        elif status == QMediaPlayer.MediaStatus.EndOfMedia:
            snap = self.state.snapshot()
            if not snap["endlist"] and self.started_once and not self.restart_pending:
                self.restart_pending = True
                QTimer.singleShot(1200, self.restart_after_unexpected_end)

    def restart_after_unexpected_end(self):
        self.restart_pending = False
        if self.state.stop_event.is_set():
            return
        self.start_from_time(self.current_abs_seconds)

    def on_player_error(self, error, error_string):
        if error_string:
            self.status_label.setText(f"Player: {error_string}")

    def toggle_play_pause(self):
        if not self.started_once:
            snap = self.state.snapshot()
            if snap["initial_buffer"] >= INITIAL_BUFFER_SECONDS:
                self.maybe_autostart(snap)
            return

        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def jump_relative(self, seconds: float):
        if not self.started_once:
            return
        self.start_from_time(self.current_abs_seconds + seconds)

    def go_live(self):
        snap = self.state.snapshot()
        target = max(snap["floor_time"], snap["downloaded_end"] - LIVE_SAFETY_SECONDS)
        self.start_from_time(target)

    def on_slider_pressed(self):
        self.user_dragging_slider = True

    def on_slider_released(self):
        self.user_dragging_slider = False
        target = self.timeline.value() / 1000.0
        self.start_from_time(target)

    def change_speed(self):
        value = float(self.speed.currentData() or 1.0)
        self.player.setPlaybackRate(value)

    def toggle_mute(self):
        muted = not self.audio.isMuted()
        self.audio.setMuted(muted)
        self.mute_btn.setText("🔇" if muted else "🔊")

    def toggle_pin(self, checked: bool):
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, checked)
        self.show()

    def show_fullscreen_controls(self):
        if not self.controls_panel.isVisible():
            self.controls_panel.show()
        self._fullscreen_last_activity = time.monotonic()

    def hide_fullscreen_controls(self):
        if self.isFullScreen() and self.started_once and not self.user_dragging_slider:
            self.controls_panel.hide()

    def update_fullscreen_ui(self):
        # Windowed mode always keeps the controls visible and preserves the
        # complete video frame.
        if not self.isFullScreen():
            if not self.controls_panel.isVisible():
                self.controls_panel.show()
            return

        # Keep status/controls visible while the initial DVR buffer is still
        # being built. Auto-hide begins once playback has actually started.
        if not self.started_once:
            self.show_fullscreen_controls()
            return

        now = time.monotonic()
        global_pos = QCursor.pos()
        local_pos = self.mapFromGlobal(global_pos)
        inside_window = self.rect().contains(local_pos)

        moved = (
            self._last_mouse_global_pos is None
            or global_pos != self._last_mouse_global_pos
        )
        self._last_mouse_global_pos = global_pos

        if moved and inside_window:
            self.show_fullscreen_controls()

        # Even if the controls are hidden, moving the pointer to the bottom
        # edge reveals them, like VLC/YouTube style fullscreen controls.
        if inside_window and local_pos.y() >= self.height() - self.fullscreen_reveal_zone_px:
            self.show_fullscreen_controls()

        # Do not hide while the pointer is actually over the panel or while
        # the timeline thumb is being dragged.
        if self.controls_panel.isVisible():
            panel_pos = self.controls_panel.mapFromGlobal(global_pos)
            over_panel = self.controls_panel.rect().contains(panel_pos)
            if over_panel or self.user_dragging_slider:
                self._fullscreen_last_activity = now
                return

            if now - self._fullscreen_last_activity >= self.fullscreen_controls_delay:
                self.hide_fullscreen_controls()

    def enter_fullscreen(self):
        # Fill the available fullscreen video area. This removes pillar-bars
        # caused by the controls reducing the video's vertical space. The
        # tradeoff is a small crop while the controls are visible.
        self.video.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatioByExpanding)
        self.show_fullscreen_controls()
        self._last_mouse_global_pos = QCursor.pos()
        self.showFullScreen()

    def leave_fullscreen(self):
        self.video.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
        self.controls_panel.show()
        self.showNormal()

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.leave_fullscreen()
        else:
            self.enter_fullscreen()

    def exit_fullscreen(self):
        if self.isFullScreen():
            self.leave_fullscreen()

    def take_screenshot(self):
        try:
            frame = self.video.videoSink().videoFrame()
            image = frame.toImage()
            if image.isNull():
                raise RuntimeError("No video frame available yet")

            SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            path = SCREENSHOT_DIR / f"screenshot_{stamp}.png"
            image.save(str(path))
            self.status_label.setText(f"Screenshot saved: {path.name}")
        except Exception as exc:
            self.status_label.setText(f"Screenshot failed: {exc}")

    def refresh_ui(self):
        snap = self.state.snapshot()
        self.maybe_autostart(snap)

        if snap["error"]:
            self.status_label.setText(snap["error"])
        elif not self.started_once:
            self.status_label.setText(
                f"Building 3-minute buffer: {snap['initial_buffer']:.0f}/{INITIAL_BUFFER_SECONDS}s"
            )
        elif self.player.mediaStatus() not in (
            QMediaPlayer.MediaStatus.StalledMedia,
            QMediaPlayer.MediaStatus.BufferingMedia,
        ):
            self.status_label.setText(snap["status"])

        self.metrics_label.setText(
            f"Ahead {fmt_time(snap['ahead'])}   •   "
            f"Behind live {fmt_time(snap['behind_live'])}   •   "
            f"Queue {snap['queue']}"
        )

        floor = snap["floor_time"]
        end = snap["downloaded_end"]
        current = self.current_abs_seconds if self.started_once else floor

        if end > floor:
            minimum = int(floor * 1000)
            maximum = int(end * 1000)
            self.timeline.setRange(minimum, max(minimum + 1, maximum))

            if not self.user_dragging_slider:
                self.timeline.setValue(int(max(floor, min(current, end)) * 1000))

        self.time_label.setText(
            f"{fmt_time(max(0, current - floor))} / {fmt_time(max(0, end - floor))}"
        )

        # LIVE glows less when already near the safe edge.
        if snap["behind_live"] <= 15:
            self.live_btn.setText("● LIVE")
        else:
            self.live_btn.setText("LIVE")

    def closeEvent(self, event):
        self.state.stop_event.set()
        with self.state.condition:
            self.state.active_stream_id += 1
            self.state.condition.notify_all()

        self.player.stop()
        self.server.shutdown()
        event.accept()


# ============================================================
# MAIN
# ============================================================

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Buffered DVR Player")

    url = sys.argv[1] if len(sys.argv) > 1 else ""
    if not url:
        url, ok = QInputDialog.getText(
            None,
            "Livestream URL",
            "Paste the NORMAL livestream webpage URL:",
        )
        if not ok or not url.strip():
            return 0
        url = url.strip()

    STATE.folder = make_session_folder()

    server = LocalStreamServer()
    server.start()

    downloader = DownloaderThread(url, STATE)
    downloader.start()

    window = DVRPlayer(STATE, server)
    window.show()

    result = app.exec()

    STATE.stop_event.set()
    with STATE.condition:
        STATE.active_stream_id += 1
        STATE.condition.notify_all()

    server.shutdown()

    if DELETE_SESSION_ON_EXIT and STATE.folder and STATE.folder.exists():
        try:
            shutil.rmtree(STATE.folder)
        except Exception:
            pass

    return result


if __name__ == "__main__":
    raise SystemExit(main())
