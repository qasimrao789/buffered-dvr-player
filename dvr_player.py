import os
os.environ.setdefault("QT_MEDIA_BACKEND", "ffmpeg")

import asyncio
import base64
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urljoin, urlparse
from urllib.request import Request, urlopen


def resource_root() -> Path:
    """Return the folder containing bundled runtime resources."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


RESOURCE_ROOT = resource_root()
BUNDLED_PLAYWRIGHT_DIR = RESOURCE_ROOT / "vendor" / "playwright"
if BUNDLED_PLAYWRIGHT_DIR.exists():
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(BUNDLED_PLAYWRIGHT_DIR)

from playwright.async_api import async_playwright

from PySide6.QtCore import QTimer, Qt, QUrl
from PySide6.QtGui import QCursor, QKeySequence, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)


# ============================================================
# APPLICATION / RELEASE SETTINGS
# ============================================================

APP_NAME = "StreamShift DVR"
APP_VERSION = "1.0.0"
GITHUB_REPOSITORY = "qasimrao789/buffered-dvr-player"
GITHUB_RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest"
UPDATE_ASSET_NAME = "StreamShift-DVR-Setup.exe"


def app_config_dir() -> Path:
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / APP_NAME
    return Path.home() / f".{APP_NAME.lower().replace(' ', '-')}"


SETTINGS_PATH = app_config_dir() / "settings.json"
DEFAULT_APP_SETTINGS = {
    "preferred_source": "",
    "check_updates": True,
}


def load_app_settings() -> dict:
    settings = dict(DEFAULT_APP_SETTINGS)
    try:
        if SETTINGS_PATH.exists():
            loaded = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                settings.update(loaded)
    except Exception as exc:
        print(f"[SETTINGS] Could not read {SETTINGS_PATH}: {exc}")
    return settings


def save_app_settings(settings: dict):
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(
            json.dumps(settings, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except Exception as exc:
        print(f"[SETTINGS] Could not save {SETTINGS_PATH}: {exc}")


def version_key(text: str) -> tuple[int, int, int, int]:
    clean = str(text or "").strip().lower().lstrip("v")
    main, _, suffix = clean.partition("-")
    nums = []
    for part in main.split(".")[:3]:
        match = re.match(r"(\d+)", part)
        nums.append(int(match.group(1)) if match else 0)
    while len(nums) < 3:
        nums.append(0)
    # Stable releases sort above prereleases with the same numeric version.
    return (nums[0], nums[1], nums[2], 1 if not suffix else 0)


def fetch_latest_release() -> dict | None:
    request = Request(
        GITHUB_RELEASES_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"StreamShift-DVR/{APP_VERSION}",
        },
    )
    try:
        with urlopen(request, timeout=12) as response:
            release = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        # 404 is normal before the first GitHub Release exists.
        if exc.code == 404:
            return None
        raise RuntimeError(f"GitHub update check failed: HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"GitHub update check failed: {exc.reason}") from exc

    if not isinstance(release, dict) or release.get("draft"):
        return None
    tag = str(release.get("tag_name") or "").strip()
    if not tag or version_key(tag) <= version_key(APP_VERSION):
        return None

    asset_url = ""
    asset_name = ""
    for asset in release.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "")
        url = str(asset.get("browser_download_url") or "")
        if name.lower() == UPDATE_ASSET_NAME.lower() and url:
            asset_name, asset_url = name, url
            break
        if not asset_url and name.lower().endswith(".exe") and "streamshift" in name.lower() and url:
            asset_name, asset_url = name, url

    return {
        "version": tag.lstrip("v"),
        "tag": tag,
        "asset_name": asset_name,
        "asset_url": asset_url,
        "html_url": str(release.get("html_url") or ""),
        "notes": str(release.get("body") or "").strip(),
    }

# ============================================================
# USER SETTINGS
# ============================================================

INITIAL_BUFFER_SECONDS = 180
KEEP_BEHIND_SECONDS = 120
CLEANUP_SAFETY_SECONDS = 30
CLEANUP_CHECK_SECONDS = 5

# When LIVE is pressed, stay a little behind the newest completely
# downloaded data instead of sitting on the exact edge.
LIVE_SAFETY_SECONDS = 8

# 1080p is the target. If no source reaches it, the highest verified source
# found across the event's embeds is used.
MIN_DESIRED_HEIGHT = 1080

# Streamed API / multi-source quality selection. The application opens on the
# official live-events endpoint, then opens every HD embed directly in Chromium.
# Chromium is allowed to choose/render its highest quality, while we capture the
# actual upstream manifests/URLs. ffprobe verifies the REAL video dimensions and
# FFmpeg remuxes that exact source into our local MPEG-TS DVR buffer.
STREAMED_API_BASE = "https://streamed.pk"
SOURCE_PROBE_SECONDS = 10
QUALITY_MENU_RETRY_SECONDS = 3
API_TIMEOUT_SECONDS = 20
MAX_EMBEDS_TO_PROBE = 10
MAX_MEDIA_CANDIDATES_PER_SOURCE = 20
MAX_TEXT_RESPONSE_BYTES = 2_000_000
FFPROBE_TIMEOUT_SECONDS = 18
FFMPEG_RESTART_DELAY_SECONDS = 3
FFMPEG_PLAYLIST_POLL_SECONDS = 0.35

# We never intentionally downgrade a verified 1080p browser source merely
# because the old Python downloader only understands MPEG-TS. FFmpeg handles
# HLS TS, HLS CMAF/fMP4, DASH, FLV, MP4 and other formats that FFmpeg supports.
REJECT_IMAGE_VIDEO_CODECS = {"webp", "mjpeg", "mjpegb", "png", "gif"}

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


@dataclass
class HLSVariant:
    url: str
    bandwidth: int = 0
    average_bandwidth: int = 0
    width: int = 0
    height: int = 0
    frame_rate: float = 0.0
    codecs: str = ""
    audio_group: str | None = None
    name: str = ""

    @property
    def effective_bandwidth(self) -> int:
        return self.average_bandwidth or self.bandwidth

    @property
    def quality_label(self) -> str:
        if self.height > 0:
            fps = ""
            if self.frame_rate >= 50:
                fps = f"{int(round(self.frame_rate))}"
            return f"{self.height}p{fps}"
        if self.effective_bandwidth > 0:
            return f"{self.effective_bandwidth / 1_000_000:.1f} Mbps"
        return "unknown quality"


@dataclass
class StreamOption:
    source: str
    source_id: str
    stream_no: int
    language: str
    hd: bool
    embed_url: str

    @property
    def label(self) -> str:
        quality = "HD" if self.hd else "SD"
        language = self.language or "Unknown language"
        return f"{self.source} #{self.stream_no} • {language} • {quality}"


@dataclass
class VerifiedHLSCandidate:
    frame: object
    playlist_url: str
    verified: tuple
    variant: HLSVariant | None = None
    source: str = "direct"

    @property
    def quality_label(self) -> str:
        if self.variant is not None:
            return self.variant.quality_label
        return "unknown/direct"

    @property
    def height(self) -> int:
        return self.variant.height if self.variant is not None else 0

    @property
    def bandwidth(self) -> int:
        return self.variant.effective_bandwidth if self.variant is not None else 0

    @property
    def score(self) -> tuple:
        if self.variant is not None:
            return variant_preference_key(self.variant)
        return (0, 0, 0, 0, 0.0, 0)


@dataclass
class MediaInputCandidate:
    url: str
    kind: str
    option: StreamOption
    page: object | None = None
    frame: object | None = None
    headers: dict[str, str] | None = None
    known_width: int = 0
    known_height: int = 0
    known_bandwidth: int = 0
    source: str = "network"

    @property
    def label(self) -> str:
        if self.known_height:
            return f"{self.known_height}p {self.kind}"
        return self.kind


@dataclass
class ProbedMediaInput:
    candidate: MediaInputCandidate
    width: int
    height: int
    video_index: int
    audio_index: int | None
    video_codec: str
    audio_codec: str
    bitrate: int = 0
    fps: float = 0.0
    format_name: str = ""
    has_audio: bool = False

    @property
    def quality_label(self) -> str:
        fps = f"{int(round(self.fps))}" if self.fps >= 50 else ""
        return f"{self.height}p{fps}" if self.height else "unknown"

    @property
    def score(self) -> tuple:
        # Actual decoded/probed dimensions dominate. Audio presence and bitrate
        # only break ties at the same video resolution.
        return (
            self.height,
            self.width,
            1 if self.has_audio else 0,
            self.bitrate,
            self.fps,
        )


# ============================================================
# STREAMED API
# ============================================================

def streamed_api_get_json(path: str, timeout: int = API_TIMEOUT_SECONDS):
    """Fetch one JSON endpoint from the documented Streamed API."""
    url = path if path.startswith("http") else f"{STREAMED_API_BASE}{path}"
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "StreamShift-DVR/1.0",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except HTTPError as exc:
        raise RuntimeError(f"Streamed API HTTP {exc.code}: {url}") from exc
    except URLError as exc:
        raise RuntimeError(f"Streamed API connection failed: {exc.reason}") from exc

    try:
        return json.loads(payload.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Streamed API returned invalid JSON: {url}") from exc


def fetch_live_events() -> list[dict]:
    data = streamed_api_get_json("/api/matches/live")
    if not isinstance(data, list):
        raise RuntimeError("Unexpected /api/matches/live response")

    events = [
        event for event in data
        if isinstance(event, dict) and event.get("title") and event.get("sources")
    ]
    events.sort(
        key=lambda event: (
            0 if event.get("popular") else 1,
            str(event.get("category") or "").lower(),
            str(event.get("title") or "").lower(),
        )
    )
    return events


def fetch_stream_options(event: dict) -> list[StreamOption]:
    options: list[StreamOption] = []
    seen_embed_urls: set[str] = set()

    for source_ref in event.get("sources") or []:
        if not isinstance(source_ref, dict):
            continue
        source = str(source_ref.get("source") or "").strip()
        source_id = str(source_ref.get("id") or "").strip()
        if not source or not source_id:
            continue

        endpoint = f"/api/stream/{quote(source, safe='')}/{quote(source_id, safe='')}"
        try:
            streams = streamed_api_get_json(endpoint)
        except Exception as exc:
            print(f"[API] {source}/{source_id}: {exc}")
            continue
        if not isinstance(streams, list):
            continue

        for stream in streams:
            if not isinstance(stream, dict):
                continue
            embed_url = str(stream.get("embedUrl") or "").strip()
            if not embed_url or embed_url in seen_embed_urls:
                continue
            seen_embed_urls.add(embed_url)
            try:
                stream_no = int(stream.get("streamNo") or 0)
            except Exception:
                stream_no = 0
            options.append(
                StreamOption(
                    source=str(stream.get("source") or source),
                    source_id=source_id,
                    stream_no=stream_no,
                    language=str(stream.get("language") or ""),
                    hd=bool(stream.get("hd")),
                    embed_url=embed_url,
                )
            )

    def option_key(option: StreamOption):
        language = option.language.lower()
        englishish = 1 if ("english" in language or language == "main") else 0
        return (1 if option.hd else 0, englishish, -option.stream_no)

    options.sort(key=option_key, reverse=True)
    return options


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
        self.selected_quality = "Detecting..."
        self.selected_variant_url = ""
        self.available_qualities: list[str] = []

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

    def set_selected_quality(self, label: str, url: str = ""):
        with self.lock:
            self.selected_quality = label
            self.selected_variant_url = url

    def set_available_qualities(self, labels: list[str]):
        with self.lock:
            self.available_qualities = list(labels)

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
                "quality": self.selected_quality,
                "variant_url": self.selected_variant_url,
                "available_qualities": list(self.available_qualities),
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


def looks_like_iso_bmff(data: bytes) -> bool:
    """Detect common ISO-BMFF/CMAF/fMP4 box signatures."""
    if len(data) < 8:
        return False
    box_type = data[4:8]
    return box_type in {b"ftyp", b"styp", b"moof", b"sidx"}


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

def parse_hls_attribute_list(value: str) -> dict[str, str]:
    """Parse an HLS comma-separated attribute list without splitting quoted commas."""
    parts: list[str] = []
    current: list[str] = []
    in_quotes = False

    for char in value:
        if char == '"':
            in_quotes = not in_quotes
            current.append(char)
        elif char == "," and not in_quotes:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)

    if current:
        parts.append("".join(current).strip())

    attrs: dict[str, str] = {}
    for part in parts:
        if "=" not in part:
            continue
        key, raw = part.split("=", 1)
        key = key.strip().upper()
        raw = raw.strip()
        if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
            raw = raw[1:-1]
        attrs[key] = raw

    return attrs


def _safe_int(value: str | None, default: int = 0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def _safe_float(value: str | None, default: float = 0.0) -> float:
    try:
        return float(value or default)
    except (TypeError, ValueError):
        return default


def parse_master_playlist(text: str, playlist_url: str) -> list[HLSVariant]:
    """Parse #EXT-X-STREAM-INF variants from an HLS master playlist."""
    lines = [line.strip() for line in text.splitlines()]
    variants: list[HLSVariant] = []
    pending_attrs: dict[str, str] | None = None

    for line in lines:
        if not line:
            continue

        if line.upper().startswith("#EXT-X-STREAM-INF:"):
            pending_attrs = parse_hls_attribute_list(line.split(":", 1)[1])
            continue

        if pending_attrs is None:
            continue

        # The URI normally follows immediately. Tolerate comments in between.
        if line.startswith("#"):
            continue

        width = 0
        height = 0
        resolution = pending_attrs.get("RESOLUTION", "")
        if "x" in resolution.lower():
            try:
                width_text, height_text = resolution.lower().split("x", 1)
                width = int(width_text)
                height = int(height_text)
            except (TypeError, ValueError):
                width = 0
                height = 0

        variants.append(
            HLSVariant(
                url=urljoin(playlist_url, line),
                bandwidth=_safe_int(pending_attrs.get("BANDWIDTH")),
                average_bandwidth=_safe_int(pending_attrs.get("AVERAGE-BANDWIDTH")),
                width=width,
                height=height,
                frame_rate=_safe_float(pending_attrs.get("FRAME-RATE")),
                codecs=pending_attrs.get("CODECS", ""),
                audio_group=pending_attrs.get("AUDIO") or None,
                name=pending_attrs.get("NAME", ""),
            )
        )
        pending_attrs = None

    return variants


def variant_preference_key(variant: HLSVariant) -> tuple:
    """Rank variants by actual quality: resolution first, then bitrate/FPS.

    Codec preference only breaks ties at the same resolution so a real 1440p
    stream is not demoted below 1080p merely because the 1080p entry says AVC.
    """
    codecs = variant.codecs.lower()
    if "avc1" in codecs or "h264" in codecs:
        codec_score = 3
    elif not codecs:
        codec_score = 2
    elif "hvc1" in codecs or "hev1" in codecs or "hevc" in codecs:
        codec_score = 1
    else:
        codec_score = 0

    # Unknown-resolution variants remain usable as a last-resort fallback.
    return (
        1 if variant.height > 0 else 0,
        variant.height,
        variant.width,
        variant.effective_bandwidth,
        variant.frame_rate,
        codec_score,
    )


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
# FFmpeg / SOURCE DISCOVERY HELPERS
# ============================================================

def parse_rate(value: str | None) -> float:
    if not value or value in {"0/0", "N/A"}:
        return 0.0
    try:
        if "/" in value:
            a, b = value.split("/", 1)
            denom = float(b)
            return float(a) / denom if denom else 0.0
        return float(value)
    except Exception:
        return 0.0


def find_ffmpeg_tools() -> tuple[str, str]:
    """Return bundled FFmpeg tools first, then fall back to PATH for development."""
    exe = ".exe" if os.name == "nt" else ""
    bundled_bin = RESOURCE_ROOT / "vendor" / "ffmpeg" / "bin"
    bundled_ffmpeg = bundled_bin / f"ffmpeg{exe}"
    bundled_ffprobe = bundled_bin / f"ffprobe{exe}"

    if bundled_ffmpeg.exists() and bundled_ffprobe.exists():
        return str(bundled_ffmpeg), str(bundled_ffprobe)

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")

    if ffmpeg and not ffprobe:
        sibling = Path(ffmpeg).with_name(f"ffprobe{exe}")
        if sibling.exists():
            ffprobe = str(sibling)

    if not ffmpeg or not ffprobe:
        raise RuntimeError(
            "FFmpeg + ffprobe were not found. Official StreamShift installers bundle "
            "both tools automatically. If you are running from source, install FFmpeg "
            "and make sure ffmpeg and ffprobe are in PATH."
        )
    return ffmpeg, ffprobe


def media_kind_from_url(url: str, content_type: str = "") -> str | None:
    lower = url.lower()
    ctype = (content_type or "").lower()
    path = urlparse(url).path.lower()

    if ".m3u8" in lower or "mpegurl" in ctype:
        return "hls"
    if path.endswith(".mpd") or "dash+xml" in ctype:
        return "dash"
    if path.endswith(".flv") or "video/x-flv" in ctype or "video/flv" in ctype:
        return "flv"
    if path.endswith(".mp4") or "video/mp4" in ctype:
        return "mp4"
    if path.endswith(".webm") or "video/webm" in ctype:
        return "webm"
    if path.endswith(".mkv") or "video/x-matroska" in ctype:
        return "mkv"
    return None


def looks_like_ad_url(url: str) -> bool:
    lower = url.lower()
    ad_tokens = (
        "doubleclick", "googlesyndication", "googleadservices", "adservice",
        "amazon-adsystem", "adnxs", "taboola", "outbrain", "imasdk",
        "/vast", "vpaid", "prebid", "adserver", "adsystem", "advert",
    )
    return any(token in lower for token in ad_tokens)



def candidate_kind_priority(kind: str) -> int:
    # Manifests/direct streams are useful inputs; individual .ts/.m4s segments
    # are deliberately not candidates because they are not complete live inputs.
    return {
        "hls": 6,
        "dash": 5,
        "flv": 4,
        "mp4": 3,
        "webm": 2,
        "mkv": 2,
        "direct": 1,
    }.get(kind, 0)


def extract_media_urls_from_text(text: str, base_url: str) -> list[tuple[str, str]]:
    """Best-effort extraction of explicit and extensionless media URLs.

    Some players return the 1080 source from opaque API routes with no .m3u8,
    .mpd or .flv suffix. Nearby JSON field names such as playUrl/origin/stream
    are therefore used as hints, then ffprobe decides whether the URL is real
    video. False positives are harmless because they are verified before use.
    """
    if not text:
        return []

    cleaned = (
        text.replace(r"\/", "/")
        .replace("&amp;", "&")
        .replace(r"\u0026", "&")
        .replace(r"\u002F", "/")
        .replace(r"\u003A", ":")
        .replace(r"\u003F", "?")
        .replace(r"\u003D", "=")
    )

    found: list[tuple[str, str]] = []
    seen: set[str] = set()

    def hint_score(url: str, context: str) -> int:
        hay = f"{url} {context}".lower()
        score = 0
        if any(x in hay for x in (".m3u8", ".mpd", ".flv", ".mp4", ".webm", ".mkv")):
            score += 20
        if any(x in hay for x in (
            "playlist", "manifest", "stream", "playurl", "play_url", "play-url",
            "pull_url", "pullurl", "live_url", "liveurl", "origin", "source",
            "transcode", "bitrate", "resolution", "hls", "dash"
        )):
            score += 6
        if any(x in hay for x in (
            "2160", "1440", "1080", "1920x1080", "2560x1440", "3840x2160",
            "fhd", "uhd", "high"
        )):
            score += 8
        if any(x in hay for x in (
            "thumbnail", "storyboard", "sprite", ".jpg", ".jpeg", ".png", ".webp",
            ".gif", "origin.image", "avatar", "poster"
        )):
            score -= 25
        # Individual media fragments are useful evidence but not stable FFmpeg
        # live inputs, so don't promote them as source URLs.
        if any(x in url.lower() for x in (".m4s", ".ts", "segment", "chunk")):
            score -= 18
        return score

    for match in re.finditer(r"https?://[^\s\"'<>\\]+", cleaned, re.I):
        url = match.group(0).rstrip(").,;]}>")
        start = max(0, match.start() - 180)
        end = min(len(cleaned), match.end() + 180)
        context = cleaned[start:end]
        kind = media_kind_from_url(url)
        if kind is None and hint_score(url, context) >= 12:
            kind = "direct"
        if kind and url not in seen:
            seen.add(url)
            found.append((url, kind))

    # Relative media paths inside JSON/HTML are also common.
    for match in re.finditer(
        r"(?P<q>[\"'])(?P<url>[^\"']+\.(?:m3u8|mpd|flv|mp4|webm|mkv)(?:\?[^\"']*)?)(?P=q)",
        cleaned,
        re.I,
    ):
        raw = match.group("url")
        url = urljoin(base_url, raw)
        kind = media_kind_from_url(url)
        if kind and url not in seen:
            seen.add(url)
            found.append((url, kind))

    return found


def safe_header_subset(headers: dict[str, str] | None) -> dict[str, str]:
    if not headers:
        return {}
    keep = {
        "user-agent",
        "referer",
        "origin",
        "cookie",
        "authorization",
        "accept",
        "accept-language",
    }
    return {str(k).lower(): str(v) for k, v in headers.items() if str(k).lower() in keep}


def ffmpeg_header_blob(headers: dict[str, str]) -> str:
    # User-Agent and Referer have dedicated FFmpeg options, but sending them in
    # the custom-header block too is harmless on some protocols and harmful on
    # others. Keep only headers without dedicated options here.
    skip = {"user-agent", "referer"}
    lines = []
    for key, value in headers.items():
        if key.lower() in skip or not value:
            continue
        canonical = "-".join(part.capitalize() for part in key.split("-"))
        lines.append(f"{canonical}: {value}")
    return "\r\n".join(lines) + ("\r\n" if lines else "")


# ============================================================
# DOWNLOADER ENGINE
# ============================================================

class DownloaderEngine:
    """Discover the browser's real highest-quality source and ingest it with FFmpeg.

    The old engine only accepted MPEG-TS HLS segments. That meant a browser could
    genuinely render 1080p through CMAF/DASH/FLV while the DVR silently fell back
    to a 540p MPEG-TS rendition. This engine removes that restriction:

      Streamed API embed -> Chromium highest-quality playback -> network capture
      -> ffprobe actual dimensions -> FFmpeg remux -> local MPEG-TS DVR segments.
    """

    def __init__(self, stream_options: list[StreamOption], state: SharedState, event_title: str):
        self.stream_options = list(stream_options)
        self.event_title = event_title
        self.state = state

        self.context = None
        self.page = None
        self.current_option: StreamOption | None = None
        self.page_options: dict[int, StreamOption] = {}
        self.opened_pages: list[object] = []

        self.candidates: dict[str, MediaInputCandidate] = {}
        self.variant_hints: dict[str, HLSVariant] = {}
        self.response_tasks: set[asyncio.Task] = set()
        self.option_browser_size: dict[str, tuple[int, int]] = {}
        self.selected_input: ProbedMediaInput | None = None
        self.selected_option: StreamOption | None = None

        self.ffmpeg_path, self.ffprobe_path = find_ffmpeg_tools()
        if self.state.folder is None:
            raise RuntimeError("Session folder was not created")
        self.local_playlist = self.state.folder / "ffmpeg_live.m3u8"
        self.segment_pattern = self.state.folder / "segment_%010d.ts"
        self.ffmpeg_process: subprocess.Popen | None = None
        self.ffmpeg_log_thread: threading.Thread | None = None
        self.ffmpeg_transcoding_video = False

    # --------------------------------------------------------
    # Browser / candidate capture
    # --------------------------------------------------------

    @staticmethod
    def option_key(option: StreamOption) -> str:
        return option.embed_url

    @staticmethod
    def stream_option_priority(option: StreamOption) -> tuple:
        language = option.language.lower()
        englishish = 1 if ("english" in language or language == "main") else 0
        return (1 if option.hd else 0, englishish, -option.stream_no)

    def register_page(self, page, option: StreamOption | None = None):
        if option is None:
            option = self.current_option
        if option is not None:
            self.page_options[id(page)] = option
        if page not in self.opened_pages:
            self.opened_pages.append(page)

    def on_new_page(self, page):
        self.register_page(page, self.current_option)

    def _track_task(self, task: asyncio.Task):
        self.response_tasks.add(task)
        task.add_done_callback(lambda done: self.response_tasks.discard(done))

    def response_event(self, response):
        task = asyncio.create_task(self.inspect_response(response))
        self._track_task(task)

    def option_for_response(self, response) -> StreamOption | None:
        try:
            frame = response.request.frame
            if frame is not None:
                option = self.page_options.get(id(frame.page))
                if option is not None:
                    return option
        except Exception:
            pass
        return self.current_option

    def add_candidate(
        self,
        *,
        url: str,
        kind: str,
        option: StreamOption,
        page=None,
        frame=None,
        headers: dict[str, str] | None = None,
        known_width: int = 0,
        known_height: int = 0,
        known_bandwidth: int = 0,
        source: str = "network",
    ):
        if not url or url.startswith(("blob:", "data:")):
            return
        if looks_like_ad_url(url):
            return
        if kind not in {"hls", "dash", "flv", "mp4", "webm", "mkv", "direct"}:
            return
        if kind in {"mp4", "webm", "mkv"} and source == "network response":
            lower = url.lower()
            if not any(token in lower for token in ("live", "stream", "origin", "playlist", "manifest")):
                # Random MP4/WebM responses on embed pages are frequently ads.
                # Direct currentSrc/API-discovered files are still allowed below.
                return

        existing = self.candidates.get(url)
        if existing is not None:
            if known_height > existing.known_height:
                existing.known_height = known_height
                existing.known_width = known_width
            existing.known_bandwidth = max(existing.known_bandwidth, known_bandwidth)
            if headers:
                existing.headers = safe_header_subset(headers)
            if page is not None:
                existing.page = page
            if frame is not None:
                existing.frame = frame
            return

        candidate = MediaInputCandidate(
            url=url,
            kind=kind,
            option=option,
            page=page,
            frame=frame,
            headers=safe_header_subset(headers),
            known_width=known_width,
            known_height=known_height,
            known_bandwidth=known_bandwidth,
            source=source,
        )
        self.candidates[url] = candidate
        quality = f" {known_height}p" if known_height else ""
        print(f"[CAPTURE] {option.source} #{option.stream_no}{quality} {kind}: {url}")

    def add_hls_manifest(
        self,
        *,
        url: str,
        text: str,
        option: StreamOption,
        page=None,
        frame=None,
        headers: dict[str, str] | None = None,
        source: str = "HLS response",
    ):
        upper = text.upper()
        if (
            "#EXT-X-IMAGES-ONLY" in upper
            or "#EXT-X-TILES" in upper
            or "#EXT-X-I-FRAMES-ONLY" in upper
        ):
            print(f"[CAPTURE] Ignoring image/trick-play HLS: {url}")
            return

        if "#EXT-X-STREAM-INF" in upper:
            variants = parse_master_playlist(text, url)
            if variants:
                max_variant = max(variants, key=variant_preference_key)
                self.add_candidate(
                    url=url,
                    kind="hls",
                    option=option,
                    page=page,
                    frame=frame,
                    headers=headers,
                    known_width=max_variant.width,
                    known_height=max_variant.height,
                    known_bandwidth=max_variant.effective_bandwidth,
                    source=f"{source} master",
                )
                for variant in variants:
                    self.variant_hints[variant.url] = variant
                    self.add_candidate(
                        url=variant.url,
                        kind="hls",
                        option=option,
                        page=page,
                        frame=frame,
                        headers=headers,
                        known_width=variant.width,
                        known_height=variant.height,
                        known_bandwidth=variant.effective_bandwidth,
                        source=f"{source} variant {variant.quality_label}",
                    )
                return

        hint = self.variant_hints.get(url)
        self.add_candidate(
            url=url,
            kind="hls",
            option=option,
            page=page,
            frame=frame,
            headers=headers,
            known_width=hint.width if hint else 0,
            known_height=hint.height if hint else 0,
            known_bandwidth=hint.effective_bandwidth if hint else 0,
            source=source,
        )

    async def inspect_response(self, response):
        option = self.option_for_response(response)
        if option is None:
            return

        url = response.url
        try:
            response_headers = await response.all_headers()
        except Exception:
            try:
                response_headers = response.headers
            except Exception:
                response_headers = {}
        content_type = str(response_headers.get("content-type") or "").lower()

        try:
            request_headers = await response.request.all_headers()
        except Exception:
            try:
                request_headers = response.request.headers
            except Exception:
                request_headers = {}

        try:
            frame = response.request.frame
            page = frame.page if frame is not None else None
            if page is not None:
                self.register_page(page, option)
        except Exception:
            frame = None
            page = None

        kind = media_kind_from_url(url, content_type)
        if kind is not None:
            self.add_candidate(
                url=url,
                kind=kind,
                option=option,
                page=page,
                frame=frame,
                headers=request_headers,
                source="network response",
            )

        # Read manifests and small API/XHR bodies even when the URL has no file
        # extension. This is important for MSE players whose actual 1080 manifest
        # is returned by an opaque API route rather than *.m3u8 or *.mpd.
        try:
            resource_type = response.request.resource_type
        except Exception:
            resource_type = ""

        content_length = 0
        try:
            content_length = int(response_headers.get("content-length") or 0)
        except Exception:
            content_length = 0

        textual_type = any(
            token in content_type
            for token in (
                "mpegurl",
                "dash+xml",
                "json",
                "text/",
                "xml",
                "javascript",
                "application/octet-stream",
            )
        )
        should_read = (
            kind in {"hls", "dash"}
            or textual_type
            or resource_type in {"xhr", "fetch", "document"}
        ) and (not content_length or content_length <= MAX_TEXT_RESPONSE_BYTES)

        if not should_read or not (200 <= response.status < 300):
            return

        try:
            text = await response.text()
        except Exception:
            return
        if len(text) > MAX_TEXT_RESPONSE_BYTES:
            return

        stripped = text.lstrip()
        upper = stripped[:2000].upper()
        if stripped.startswith("#EXTM3U") or "#EXT-X-STREAM-INF" in upper:
            self.add_hls_manifest(
                url=url,
                text=text,
                option=option,
                page=page,
                frame=frame,
                headers=request_headers,
                source="manifest body",
            )
        elif "<MPD" in stripped[:5000] or "<MPD" in text[:5000]:
            self.add_candidate(
                url=url,
                kind="dash",
                option=option,
                page=page,
                frame=frame,
                headers=request_headers,
                source="MPD body",
            )

        for hidden_url, hidden_kind in extract_media_urls_from_text(text, url):
            self.add_candidate(
                url=hidden_url,
                kind=hidden_kind,
                option=option,
                page=page,
                frame=frame,
                headers=request_headers,
                source="API/HTML metadata",
            )

    async def wait_for_response_tasks(self, timeout: float = 2.0):
        if not self.response_tasks:
            return
        deadline = time.monotonic() + timeout
        while self.response_tasks and time.monotonic() < deadline:
            await asyncio.sleep(0.05)

    async def encourage_playback(self, page):
        for frame in list(page.frames):
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

    async def pause_page(self, page):
        for frame in list(page.frames):
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

    async def try_click_play_control(self, page) -> bool:
        """Best-effort click for custom player overlays that gate source loading."""
        for frame in list(page.frames):
            try:
                clicked = await frame.evaluate(
                    r"""
                    () => {
                        const visible = (el) => {
                            const r = el.getBoundingClientRect();
                            if (r.width < 8 || r.height < 8) return false;
                            const s = getComputedStyle(el);
                            return s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
                        };
                        const candidates = [];
                        for (const el of document.querySelectorAll('button,[role="button"],a')) {
                            if (!visible(el)) continue;
                            const text = [
                                el.getAttribute('aria-label') || '',
                                el.getAttribute('title') || '',
                                el.innerText || ''
                            ].join(' ').trim().toLowerCase();
                            if (!text) continue;
                            let score = 0;
                            if (/^play$/.test(text)) score += 20;
                            if (text.includes('play video')) score += 18;
                            if (text.includes('start video')) score += 15;
                            if (text.includes('watch stream')) score += 12;
                            if (text.includes('play')) score += 5;
                            if (text.includes('replay') || text.includes('display') || text.includes('playlist')) score -= 8;
                            if (score > 0) candidates.push({el, score});
                        }
                        candidates.sort((a,b) => b.score - a.score);
                        if (!candidates.length) return false;
                        candidates[0].el.click();
                        return true;
                    }
                    """
                )
                if clicked:
                    print("[PLAYER] Clicked a visible Play control")
                    return True
            except Exception:
                pass
        return False

    async def try_click_highest_quality(self, page) -> int:
        """Best-effort generic click of the highest visible quality option."""
        try:
            await page.bring_to_front()
        except Exception:
            pass

        async def click_visible_quality() -> int:
            best_height = 0
            for frame in list(page.frames):
                try:
                    result = await frame.evaluate(
                        r"""
                        () => {
                            const visible = (el) => {
                                const r = el.getBoundingClientRect();
                                if (r.width <= 1 || r.height <= 1) return false;
                                const s = getComputedStyle(el);
                                return s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
                            };
                            const items = [];
                            for (const el of document.querySelectorAll('button,[role="menuitem"],[role="option"],li,a,div,span')) {
                                if (!visible(el)) continue;
                                const text = (el.innerText || el.textContent || '').trim();
                                const exact = text.match(/^\s*(2160|1440|1080|900|720|576|540|480|360)p(?:\s.*)?$/i);
                                if (!exact) continue;
                                // Prefer compact leaf-like option elements, not a parent containing the whole menu.
                                if (text.length > 45) continue;
                                items.push({el, height: Number(exact[1]), text});
                            }
                            items.sort((a,b) => b.height - a.height);
                            if (!items.length) return null;
                            items[0].el.click();
                            return {height: items[0].height, text: items[0].text};
                        }
                        """
                    )
                    if result and int(result.get("height") or 0) > best_height:
                        best_height = int(result.get("height") or 0)
                except Exception:
                    pass
            return best_height

        height = await click_visible_quality()
        if height:
            print(f"[PLAYER] Clicked highest visible quality: {height}p")
            return height

        # Try opening a settings/quality control. Sites differ wildly, so use
        # accessibility labels/title/text rather than provider-specific classes.
        for frame in list(page.frames):
            try:
                clicked = await frame.evaluate(
                    """
                    () => {
                        const visible = (el) => {
                            const r = el.getBoundingClientRect();
                            if (r.width <= 1 || r.height <= 1) return false;
                            const s = getComputedStyle(el);
                            return s.display !== 'none' && s.visibility !== 'hidden';
                        };
                        const candidates = [];
                        for (const el of document.querySelectorAll('button,[role="button"],a')) {
                            if (!visible(el)) continue;
                            const blob = [
                                el.getAttribute('aria-label') || '',
                                el.getAttribute('title') || '',
                                el.innerText || '',
                                el.className || ''
                            ].join(' ').toLowerCase();
                            let score = 0;
                            if (blob.includes('quality')) score += 10;
                            if (blob.includes('setting')) score += 8;
                            if (blob.includes('gear')) score += 5;
                            if (blob.includes('cog')) score += 5;
                            if (score) candidates.push({el, score});
                        }
                        candidates.sort((a,b) => b.score - a.score);
                        if (!candidates.length) return false;
                        candidates[0].el.click();
                        return true;
                    }
                    """
                )
                if clicked:
                    break
            except Exception:
                pass

        await asyncio.sleep(0.5)

        # Some players expose Settings -> Quality as a second menu level.
        for frame in list(page.frames):
            try:
                clicked = await frame.evaluate(
                    r"""
                    () => {
                        const visible = (el) => {
                            const r = el.getBoundingClientRect();
                            return r.width > 1 && r.height > 1 && getComputedStyle(el).visibility !== 'hidden';
                        };
                        const els = [...document.querySelectorAll('button,[role="menuitem"],li,div,span')]
                            .filter(visible)
                            .filter(el => /^\s*quality\s*$/i.test((el.innerText || el.textContent || '').trim()));
                        if (!els.length) return false;
                        els[0].click();
                        return true;
                    }
                    """
                )
                if clicked:
                    break
            except Exception:
                pass

        await asyncio.sleep(0.5)
        height = await click_visible_quality()
        if height:
            print(f"[PLAYER] Selected highest quality from menu: {height}p")
        return height

    async def page_video_info(self, page) -> dict:
        best = {"width": 0, "height": 0, "src": "", "ready": 0}
        for frame in list(page.frames):
            try:
                info = await frame.evaluate(
                    """
                    () => {
                        let best = null;
                        for (const video of document.querySelectorAll('video')) {
                            const item = {
                                width: Number(video.videoWidth || 0),
                                height: Number(video.videoHeight || 0),
                                src: String(video.currentSrc || video.src || ''),
                                ready: Number(video.readyState || 0),
                            };
                            if (!best || item.width * item.height > best.width * best.height) best = item;
                        }
                        return best;
                    }
                    """
                )
            except Exception:
                info = None
            if info and int(info.get("width") or 0) * int(info.get("height") or 0) > best["width"] * best["height"]:
                best = {
                    "width": int(info.get("width") or 0),
                    "height": int(info.get("height") or 0),
                    "src": str(info.get("src") or ""),
                    "ready": int(info.get("ready") or 0),
                }
        return best

    async def collect_runtime_urls(self, page, option: StreamOption):
        """Collect currentSrc, performance resources, and our fetch/XHR hook."""
        for frame in list(page.frames):
            try:
                payload = await frame.evaluate(
                    """
                    () => ({
                        current: [...document.querySelectorAll('video')].map(v => v.currentSrc || v.src || '').filter(Boolean),
                        resources: performance.getEntriesByType('resource').map(e => e.name).filter(Boolean),
                        hooked: Array.isArray(window.__streamshiftSeenUrls) ? window.__streamshiftSeenUrls.slice() : [],
                        mseTypes: Array.isArray(window.__streamshiftMseTypes) ? window.__streamshiftMseTypes.slice() : [],
                    })
                    """
                )
            except Exception:
                continue

            for mse_type in payload.get("mseTypes") or []:
                print(f"[MSE] {option.label}: {mse_type}")

            for url in (payload.get("current") or []) + (payload.get("resources") or []) + (payload.get("hooked") or []):
                url = str(url)
                kind = media_kind_from_url(url)
                if kind is None:
                    lower = url.lower()
                    looks_like_endpoint = any(token in lower for token in (
                        "manifest", "playlist", "playurl", "play_url", "pull_url",
                        "live_url", "liveurl", "origin", "master", "stream"
                    ))
                    looks_like_piece = any(token in lower for token in (
                        "segment", "chunk", ".m4s", ".ts", "thumbnail", "sprite",
                        ".jpg", ".jpeg", ".png", ".webp", ".gif"
                    ))
                    if looks_like_endpoint and not looks_like_piece and url.startswith(("http://", "https://")):
                        kind = "direct"
                if kind:
                    self.add_candidate(
                        url=url,
                        kind=kind,
                        option=option,
                        page=page,
                        frame=frame,
                        source="browser runtime",
                    )

    async def scan_option(self, option: StreamOption, page, *, extended: bool = False) -> tuple[int, int]:
        seconds = 8 if extended else SOURCE_PROBE_SECONDS
        quality_clicked = 0
        max_width = 0
        max_height = 0
        next_quality_attempt = 1.0
        start = time.monotonic()

        while time.monotonic() - start < seconds and not self.state.stop_event.is_set():
            await self.encourage_playback(page)
            elapsed = time.monotonic() - start
            if elapsed >= 1.5 and max_height == 0 and int(elapsed * 10) % 30 < 8:
                await self.try_click_play_control(page)
            if elapsed >= next_quality_attempt:
                clicked = await self.try_click_highest_quality(page)
                quality_clicked = max(quality_clicked, clicked)
                next_quality_attempt += QUALITY_MENU_RETRY_SECONDS

            info = await self.page_video_info(page)
            if info["height"] > max_height or (
                info["height"] == max_height and info["width"] > max_width
            ):
                max_width = info["width"]
                max_height = info["height"]
                if max_height:
                    print(
                        f"[PLAYER] {option.label} actual decoded video: "
                        f"{max_width}x{max_height} currentSrc={info['src'] or '(blob/MSE or unavailable)'}"
                    )
                    if info["src"].startswith("http"):
                        kind = media_kind_from_url(info["src"]) or "direct"
                        self.add_candidate(
                            url=info["src"],
                            kind=kind,
                            option=option,
                            page=page,
                            source="video.currentSrc",
                            known_width=max_width,
                            known_height=max_height,
                        )
            await asyncio.sleep(0.75)

        await self.collect_runtime_urls(page, option)
        await self.wait_for_response_tasks(2.0)
        key = self.option_key(option)
        old = self.option_browser_size.get(key, (0, 0))
        if max_height > old[1] or (max_height == old[1] and max_width > old[0]):
            self.option_browser_size[key] = (max_width, max_height)
        return max_width, max_height

    # --------------------------------------------------------
    # ffprobe verification
    # --------------------------------------------------------

    async def headers_for_candidate(self, candidate: MediaInputCandidate) -> dict[str, str]:
        headers = dict(candidate.headers or {})
        try:
            cookies = await self.context.cookies(candidate.url)
        except Exception:
            cookies = []
        if cookies:
            cookie_text = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
            if cookie_text:
                headers["cookie"] = cookie_text
        if "referer" not in headers:
            headers["referer"] = candidate.option.embed_url
        if "user-agent" not in headers and candidate.page is not None:
            try:
                headers["user-agent"] = await candidate.page.evaluate("() => navigator.userAgent")
            except Exception:
                pass
        return safe_header_subset(headers)

    def ff_http_args(self, headers: dict[str, str]) -> list[str]:
        args: list[str] = []
        user_agent = headers.get("user-agent")
        referer = headers.get("referer")
        if user_agent:
            args += ["-user_agent", user_agent]
        if referer:
            args += ["-referer", referer]
        blob = ffmpeg_header_blob(headers)
        if blob:
            args += ["-headers", blob]
        return args

    async def probe_candidate(self, candidate: MediaInputCandidate) -> ProbedMediaInput | None:
        headers = await self.headers_for_candidate(candidate)
        cmd = [
            self.ffprobe_path,
            "-v", "error",
            "-rw_timeout", "12000000",
            "-analyzeduration", "7000000",
            "-probesize", "12000000",
        ]
        if candidate.kind == "hls":
            cmd += ["-live_start_index", "0"]
        cmd += self.ff_http_args(headers)
        cmd += [
            "-show_streams",
            "-show_programs",
            "-show_format",
            "-of", "json",
            candidate.url,
        ]

        def run_probe():
            return subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=FFPROBE_TIMEOUT_SECONDS,
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
            )

        try:
            completed = await asyncio.to_thread(run_probe)
        except subprocess.TimeoutExpired:
            print(f"[FFPROBE] Timeout: {candidate.url}")
            return None
        except Exception as exc:
            print(f"[FFPROBE] Failed to run: {exc}")
            return None

        if completed.returncode != 0:
            tail = " ".join(completed.stderr.strip().splitlines()[-2:])
            print(f"[FFPROBE] Rejected {candidate.label}: {tail[:280]}")
            return None

        try:
            payload = json.loads(completed.stdout or "{}")
        except Exception:
            return None

        streams = payload.get("streams") or []
        videos = []
        audios = []
        for stream in streams:
            codec_type = str(stream.get("codec_type") or "")
            codec_name = str(stream.get("codec_name") or "").lower()
            if codec_type == "video":
                width = int(stream.get("width") or 0)
                height = int(stream.get("height") or 0)
                if codec_name in REJECT_IMAGE_VIDEO_CODECS:
                    continue
                if width <= 0 or height <= 0:
                    continue
                bitrate = 0
                try:
                    bitrate = int(stream.get("bit_rate") or 0)
                except Exception:
                    pass
                videos.append((height, width, bitrate, stream))
            elif codec_type == "audio":
                audios.append(stream)

        if not videos:
            print(f"[FFPROBE] No real video stream in {candidate.url}")
            return None

        videos.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        height, width, stream_bitrate, video = videos[0]
        video_index = int(video.get("index") or 0)
        video_codec = str(video.get("codec_name") or "")

        # Prefer an audio stream from the same program as the chosen video.
        audio = None
        for program in payload.get("programs") or []:
            pstreams = program.get("streams") or []
            indexes = {
                int(s.get("index"))
                for s in pstreams
                if s.get("index") is not None
            }
            if video_index not in indexes:
                continue
            audio_candidates = [s for s in pstreams if str(s.get("codec_type") or "") == "audio"]
            if audio_candidates:
                audio = audio_candidates[0]
                break
        if audio is None and audios:
            audio = audios[0]

        audio_index = int(audio.get("index")) if audio and audio.get("index") is not None else None
        audio_codec = str(audio.get("codec_name") or "") if audio else ""

        bitrate = stream_bitrate or candidate.known_bandwidth
        if not bitrate:
            try:
                bitrate = int((payload.get("format") or {}).get("bit_rate") or 0)
            except Exception:
                bitrate = 0
        fps = parse_rate(video.get("avg_frame_rate") or video.get("r_frame_rate"))
        format_name = str((payload.get("format") or {}).get("format_name") or "")

        result = ProbedMediaInput(
            candidate=candidate,
            width=width,
            height=height,
            video_index=video_index,
            audio_index=audio_index,
            video_codec=video_codec,
            audio_codec=audio_codec,
            bitrate=bitrate,
            fps=fps,
            format_name=format_name,
            has_audio=audio_index is not None,
        )
        bitrate_text = f" {bitrate / 1_000_000:.2f}Mbps" if bitrate else ""
        audio_text = f" + {audio_codec}" if audio_codec else " (video only)"
        print(
            f"[FFPROBE] VERIFIED {result.quality_label} {video_codec}{audio_text}{bitrate_text} "
            f"[{candidate.kind}] -> {candidate.url}"
        )
        return result

    async def probe_option_candidates(
        self,
        option: StreamOption,
        browser_width: int,
        browser_height: int,
        already_probed: set[str] | None = None,
    ) -> tuple[ProbedMediaInput | None, set[str]]:
        if already_probed is None:
            already_probed = set()

        candidates = [
            c for c in self.candidates.values()
            if c.option.embed_url == option.embed_url and c.url not in already_probed
        ]
        candidates.sort(
            key=lambda c: (
                c.known_height,
                c.known_width,
                c.known_bandwidth,
                candidate_kind_priority(c.kind),
            ),
            reverse=True,
        )
        candidates = candidates[:MAX_MEDIA_CANDIDATES_PER_SOURCE]

        best: ProbedMediaInput | None = None
        target_height = browser_height or MIN_DESIRED_HEIGHT

        for candidate in candidates:
            if self.state.stop_event.is_set():
                break
            already_probed.add(candidate.url)

            # Once we have actually verified the same resolution Chromium is
            # decoding, lower advertised variants cannot improve this source.
            if (
                best is not None
                and browser_height > 0
                and best.height >= browser_height
                and candidate.known_height > 0
                and candidate.known_height < best.height
            ):
                continue

            result = await self.probe_candidate(candidate)
            if result is None:
                continue
            if best is None or result.score > best.score:
                best = result

            # For our goal, a verified 1080p+ source is enough. If the browser
            # itself only decodes 1080p, no lower/unknown candidate can beat it.
            if best.height >= max(MIN_DESIRED_HEIGHT, target_height):
                break

        return best, already_probed

    # --------------------------------------------------------
    # Highest-quality source acquisition
    # --------------------------------------------------------

    async def acquire_best_source(self, reason: str) -> ProbedMediaInput:
        self.state.set_status(reason)
        self.state.set_selected_quality("Finding actual highest quality...")
        self.candidates.clear()
        self.variant_hints.clear()
        self.option_browser_size.clear()
        self.selected_option = None

        # Close stale probe pages from a previous token/session scan.
        for old in list(self.opened_pages):
            try:
                if not old.is_closed():
                    await old.close()
            except Exception:
                pass
        self.opened_pages.clear()
        self.page_options.clear()

        ordered = sorted(self.stream_options, key=self.stream_option_priority, reverse=True)
        hd_options = [o for o in ordered if o.hd]
        sd_options = [o for o in ordered if not o.hd]
        phases = [hd_options, sd_options]

        global_best: ProbedMediaInput | None = None
        best_page = None

        print("=" * 78)
        print(f"[EVENT] {self.event_title}")
        print(f"[API] {len(hd_options)} HD embeds, {len(sd_options)} SD embeds")
        print("[QUALITY] Browser selects highest -> ffprobe verifies real pixels -> FFmpeg ingests it")
        print("=" * 78)

        for phase_index, options in enumerate(phases):
            if phase_index == 1 and global_best is not None:
                break

            for option in options[:MAX_EMBEDS_TO_PROBE]:
                if self.state.stop_event.is_set():
                    raise RuntimeError("Stopped")

                self.current_option = option
                page = await self.context.new_page()
                self.register_page(page, option)
                self.page = page

                print(f"\n[SOURCE] Testing {option.label}: {option.embed_url}")
                self.state.set_status(
                    f"Testing {option.source} #{option.stream_no} — {option.language or 'unknown'}"
                )
                try:
                    await page.goto(option.embed_url, wait_until="domcontentloaded", timeout=60000)
                except Exception as exc:
                    print(f"[SOURCE] Page load warning: {exc}")

                browser_width, browser_height = await self.scan_option(option, page)
                probed_urls: set[str] = set()
                option_best, probed_urls = await self.probe_option_candidates(
                    option, browser_width, browser_height, probed_urls
                )

                # If Chromium visibly achieved 1080p but we have not captured a
                # 1080-capable input yet, keep the page running longer. This is
                # specifically designed for MSE players that fetch the real
                # high-quality manifest only after the quality switch settles.
                if browser_height >= MIN_DESIRED_HEIGHT and (
                    option_best is None or option_best.height < browser_height
                ):
                    print(
                        f"[QUALITY] Chromium is actually decoding {browser_width}x{browser_height}, "
                        "but the ingest URL is not captured yet. Deepening capture..."
                    )
                    browser_width, browser_height = await self.scan_option(option, page, extended=True)
                    extra_best, probed_urls = await self.probe_option_candidates(
                        option, browser_width, browser_height, probed_urls
                    )
                    if extra_best is not None and (
                        option_best is None or extra_best.score > option_best.score
                    ):
                        option_best = extra_best

                if option_best is not None:
                    if global_best is None or option_best.score > global_best.score:
                        global_best = option_best
                        best_page = page
                        self.selected_option = option
                        self.state.set_selected_quality(
                            f"Found {global_best.quality_label}", global_best.candidate.url
                        )
                        print(
                            f"[QUALITY] NEW BEST: {global_best.quality_label} "
                            f"from {option.label}"
                        )

                    # The stated goal is 1080p when available. Stop immediately
                    # once a real 1080p+ input is verified instead of wasting a
                    # minute probing equivalent embeds.
                    if global_best.height >= MIN_DESIRED_HEIGHT:
                        print("[QUALITY] Verified 1080p+ source found; stopping source search.")
                        break

                await self.pause_page(page)

            if global_best is not None and global_best.height >= MIN_DESIRED_HEIGHT:
                break

        self.current_option = None

        if global_best is None:
            raise RuntimeError(
                "No FFmpeg-readable video source was found for this event. "
                "The browser embeds opened, but no usable HLS/DASH/FLV/MP4 input could be verified."
            )

        max_browser_width = 0
        max_browser_height = 0
        for width, height in self.option_browser_size.values():
            if height > max_browser_height or (height == max_browser_height and width > max_browser_width):
                max_browser_width, max_browser_height = width, height

        # Never repeat the old failure mode: if Chromium has PROVEN that this
        # event is actually decoding at 1080p+, do not silently hand the user a
        # 540p DVR merely because that lower transport is easier to ingest.
        if max_browser_height >= MIN_DESIRED_HEIGHT and global_best.height < MIN_DESIRED_HEIGHT:
            raise RuntimeError(
                f"Chromium actually decoded {max_browser_width}x{max_browser_height}, but the "
                f"highest FFmpeg-readable URL captured was only {global_best.width}x{global_best.height}. "
                "Refusing to downgrade to the low-quality stream."
            )

        # If the browser proved a higher resolution on the selected embed than
        # ffprobe could ingest, don't lie to the user about quality.
        selected_browser_size = self.option_browser_size.get(
            self.option_key(global_best.candidate.option), (0, 0)
        )
        if selected_browser_size[1] > global_best.height:
            print(
                f"[WARNING] Browser reached {selected_browser_size[0]}x{selected_browser_size[1]} "
                f"but the best FFmpeg-readable source is {global_best.width}x{global_best.height}."
            )

        # Keep the winning embed alive (paused) in case its session/cookies are
        # needed; close all other pages to stop wasting bandwidth.
        for page in list(self.opened_pages):
            if page is best_page:
                continue
            try:
                if not page.is_closed():
                    await page.close()
            except Exception:
                pass
        self.opened_pages = [best_page] if best_page is not None else []
        self.page = best_page
        if best_page is not None:
            await self.pause_page(best_page)

        self.selected_input = global_best
        bitrate_text = (
            f" @ {global_best.bitrate / 1_000_000:.2f} Mbps" if global_best.bitrate else ""
        )
        print("=" * 78)
        print(f"[QUALITY] FINAL VERIFIED INPUT: {global_best.width}x{global_best.height}{bitrate_text}")
        print(f"[QUALITY] VIDEO CODEC: {global_best.video_codec}")
        print(f"[QUALITY] TRANSPORT: {global_best.candidate.kind} / {global_best.format_name}")
        print(f"[QUALITY] SOURCE: {global_best.candidate.option.label}")
        print(f"[QUALITY] URL: {global_best.candidate.url}")
        print("=" * 78)
        self.state.set_selected_quality(
            global_best.quality_label, global_best.candidate.url
        )
        self.state.set_status(
            f"Selected {global_best.quality_label} — starting FFmpeg DVR ingest"
        )
        return global_best

    # --------------------------------------------------------
    # FFmpeg -> local HLS/TS DVR bridge
    # --------------------------------------------------------

    def next_output_sequence(self) -> int:
        with self.state.lock:
            if self.state.latest_discovered_sequence is None:
                return 0
            return self.state.latest_discovered_sequence + 1

    def _drain_ffmpeg_stderr(self, process: subprocess.Popen):
        try:
            if process.stderr is None:
                return
            for line in iter(process.stderr.readline, ""):
                if not line:
                    break
                text = line.strip()
                if not text:
                    continue
                lower = text.lower()
                # Keep useful failures/quality messages without flooding the console.
                if any(token in lower for token in (
                    "error", "failed", "403", "401", "404", "invalid", "codec", "non-monotonous"
                )):
                    print(f"[FFMPEG] {text}")
        except Exception:
            pass

    async def start_ffmpeg_process(
        self,
        selected: ProbedMediaInput,
        start_number: int,
        *,
        transcode_video: bool = False,
    ) -> subprocess.Popen:
        try:
            if self.local_playlist.exists():
                self.local_playlist.unlink()
        except Exception:
            pass

        headers = await self.headers_for_candidate(selected.candidate)
        cmd = [
            self.ffmpeg_path,
            "-hide_banner",
            "-loglevel", "warning",
            "-nostdin",
            "-rw_timeout", "15000000",
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_on_network_error", "1",
            "-reconnect_on_http_error", "401,403,404,408,429,5xx",
            "-reconnect_delay_max", "5",
        ]
        if selected.candidate.kind == "hls":
            cmd += ["-live_start_index", "0"]
        cmd += self.ff_http_args(headers)
        cmd += ["-i", selected.candidate.url]

        cmd += ["-map", f"0:{selected.video_index}"]
        if selected.audio_index is not None:
            cmd += ["-map", f"0:{selected.audio_index}"]
        else:
            cmd += ["-map", "0:a:0?"]

        if transcode_video:
            # Last-resort compatibility path. Resolution is preserved; CRF 17 is
            # visually transparent for this use and avoids ever falling back to
            # a lower upstream rendition just because its codec won't mux to TS.
            cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "17"]
        else:
            cmd += ["-c:v", "copy"]

        # Re-encoding audio to AAC avoids TS incompatibilities (e.g. Opus) while
        # leaving the video completely untouched in the normal path.
        cmd += [
            "-c:a", "aac",
            "-b:a", "192k",
            "-max_muxing_queue_size", "4096",
            "-avoid_negative_ts", "make_zero",
            "-f", "hls",
            "-hls_segment_type", "mpegts",
            "-hls_time", "4",
            "-hls_list_size", "0",
            "-hls_flags", "temp_file+independent_segments",
            "-start_number", str(start_number),
            "-hls_segment_filename", str(self.segment_pattern),
            str(self.local_playlist),
        ]

        print(
            f"[FFMPEG] Starting {'1080-preserving transcode' if transcode_video else 'video-copy remux'} "
            f"at local sequence {start_number}"
        )
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
        self.ffmpeg_log_thread = threading.Thread(
            target=self._drain_ffmpeg_stderr,
            args=(process,),
            daemon=True,
        )
        self.ffmpeg_log_thread.start()
        self.ffmpeg_process = process
        self.ffmpeg_transcoding_video = transcode_video
        return process

    def parse_local_playlist(self) -> tuple[list[tuple[int, float, Path]], bool]:
        if not self.local_playlist.exists():
            return [], False
        try:
            text = self.local_playlist.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return [], False

        media_sequence = 0
        duration = 0.0
        index = 0
        items: list[tuple[int, float, Path]] = []
        endlist = False

        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                try:
                    media_sequence = int(line.split(":", 1)[1])
                except Exception:
                    pass
            elif line.startswith("#EXTINF:"):
                try:
                    duration = float(line.split(":", 1)[1].split(",", 1)[0])
                except Exception:
                    duration = 0.0
            elif line.startswith("#EXT-X-ENDLIST"):
                endlist = True
            elif not line.startswith("#"):
                path = (self.local_playlist.parent / line).resolve()
                items.append((media_sequence + index, duration or 4.0, path))
                index += 1
                duration = 0.0

        return items, endlist

    def register_local_segments(self) -> int:
        items, _endlist = self.parse_local_playlist()
        if not items:
            return 0

        segments = [
            Segment(sequence=seq, url=path.as_uri(), duration=duration)
            for seq, duration, path in items
        ]
        target = max((duration for _seq, duration, _path in items), default=4.0)
        self.state.register_segments(segments, target)

        newly_saved = 0
        for seq, _duration, path in items:
            if not path.exists():
                continue
            with self.state.lock:
                already = seq in self.state.downloaded and self.state.saved_files.get(seq) == path
            if already:
                continue
            self.state.mark_saved(seq, path)
            newly_saved += 1

        return newly_saved

    async def monitor_ffmpeg(self, process: subprocess.Popen) -> int:
        last_count = -1
        while not self.state.stop_event.is_set():
            self.register_local_segments()
            snap = self.state.snapshot()
            current_count = snap["saved"]
            if current_count != last_count:
                last_count = current_count
                self.state.set_status(
                    f"FFmpeg ingest {self.selected_input.quality_label if self.selected_input else ''} — "
                    f"buffer {snap['initial_buffer']:.0f}s"
                )

            code = process.poll()
            if code is not None:
                # One final playlist read catches the last complete segment.
                self.register_local_segments()
                return int(code)
            await asyncio.sleep(FFMPEG_PLAYLIST_POLL_SECONDS)

        try:
            process.terminate()
        except Exception:
            pass
        return 0

    async def ingest_selected_source(self, selected: ProbedMediaInput):
        before_saved = self.state.snapshot()["saved"]
        start_number = self.next_output_sequence()
        process = await self.start_ffmpeg_process(selected, start_number, transcode_video=False)

        # Catch immediate mux/codec failures quickly.
        await asyncio.sleep(3.0)
        if process.poll() is not None and not self.state.stop_event.is_set():
            print(
                "[FFMPEG] Video-copy path exited immediately. Retrying with H.264 at the SAME resolution; "
                "no upstream quality downgrade will be used."
            )
            start_number = self.next_output_sequence()
            process = await self.start_ffmpeg_process(selected, start_number, transcode_video=True)
            return await self.monitor_ffmpeg(process)

        code = await self.monitor_ffmpeg(process)
        after_saved = self.state.snapshot()["saved"]

        # A copy-mode process can sometimes survive startup and then fail before
        # producing the first TS segment. Retry that exact high-resolution input
        # with H.264 rather than rediscovering/falling back to a lower source.
        if (
            code != 0
            and after_saved <= before_saved
            and not self.state.stop_event.is_set()
        ):
            print(
                "[FFMPEG] High-quality input was readable but stream-copy produced no DVR segments. "
                "Retrying at the SAME resolution with H.264."
            )
            start_number = self.next_output_sequence()
            process = await self.start_ffmpeg_process(selected, start_number, transcode_video=True)
            return await self.monitor_ffmpeg(process)

        return code

    # --------------------------------------------------------
    # Disk cleanup (same DVR behavior as the original player)
    # --------------------------------------------------------

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
        # Hook fetch/XHR before any embed JavaScript executes. This catches URLs
        # that may not have obvious extensions and also records MSE codec types.
        init_script = r"""
        (() => {
            if (window.__streamshiftHooked) return;
            window.__streamshiftHooked = true;
            window.__streamshiftSeenUrls = [];
            window.__streamshiftMseTypes = [];
            const remember = (u) => {
                try {
                    const s = typeof u === 'string' ? u : (u && u.url ? u.url : String(u || ''));
                    if (s && !window.__streamshiftSeenUrls.includes(s)) window.__streamshiftSeenUrls.push(s);
                } catch (_) {}
            };
            const originalFetch = window.fetch;
            if (originalFetch) {
                window.fetch = function(...args) {
                    remember(args[0]);
                    return originalFetch.apply(this, args);
                };
            }
            const originalOpen = XMLHttpRequest.prototype.open;
            XMLHttpRequest.prototype.open = function(method, url, ...rest) {
                remember(url);
                return originalOpen.call(this, method, url, ...rest);
            };
            if (window.MediaSource && MediaSource.prototype.addSourceBuffer) {
                const originalAdd = MediaSource.prototype.addSourceBuffer;
                MediaSource.prototype.addSourceBuffer = function(mime) {
                    try {
                        if (mime && !window.__streamshiftMseTypes.includes(String(mime))) {
                            window.__streamshiftMseTypes.push(String(mime));
                        }
                    } catch (_) {}
                    return originalAdd.call(this, mime);
                };
            }
        })();
        """

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
            self.context = context
            await context.add_init_script(init_script)
            context.on("response", self.response_event)
            context.on("page", self.on_new_page)

            cleanup = asyncio.create_task(self.cleanup_task())
            try:
                while not self.state.stop_event.is_set():
                    try:
                        selected = await self.acquire_best_source(
                            "Resolving the event's actual highest-quality source..."
                        )
                        code = await self.ingest_selected_source(selected)
                        if self.state.stop_event.is_set():
                            break

                        print(f"[FFMPEG] Ingest stopped with exit code {code}; refreshing source/session...")
                        self.state.set_status(
                            "High-quality source expired/stopped — refreshing browser session..."
                        )
                        await asyncio.sleep(FFMPEG_RESTART_DELAY_SECONDS)
                    except Exception as exc:
                        if self.state.stop_event.is_set():
                            break
                        print(f"[ENGINE] {type(exc).__name__}: {exc}")
                        self.state.set_error(f"Source/FFmpeg error: {exc}")
                        await asyncio.sleep(5)
                        with self.state.lock:
                            self.state.error = ""
            finally:
                cleanup.cancel()
                await asyncio.gather(cleanup, return_exceptions=True)
                if self.ffmpeg_process is not None and self.ffmpeg_process.poll() is None:
                    try:
                        self.ffmpeg_process.terminate()
                        self.ffmpeg_process.wait(timeout=3)
                    except Exception:
                        try:
                            self.ffmpeg_process.kill()
                        except Exception:
                            pass
                for page in list(self.opened_pages):
                    try:
                        if not page.is_closed():
                            await page.close()
                    except Exception:
                        pass
                try:
                    await context.close()
                except Exception:
                    pass


class DownloaderThread(threading.Thread):
    def __init__(
        self,
        stream_options: list[StreamOption],
        state: SharedState,
        event_title: str,
    ):
        super().__init__(daemon=True)
        self.stream_options = list(stream_options)
        self.state = state
        self.event_title = event_title

    def run(self):
        try:
            engine = DownloaderEngine(self.stream_options, self.state, self.event_title)
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
    def __init__(self, state: SharedState, server: LocalStreamServer, event_title: str):
        super().__init__()
        self.state = state
        self.server = server
        self.event_title = event_title

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

        self.setWindowTitle(f"{self.event_title} — StreamShift DVR")
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
            f"{snap['quality']}   •   "
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
# LIVE EVENT PICKER
# ============================================================

class EventPicker(QDialog):
    def __init__(self, settings: dict, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.events: list[dict] = []
        self.selected_event: dict | None = None

        self.latest_release: dict | None = None
        self._update_check_running = False
        self._update_check_done = False
        self._update_check_manual = False
        self._update_result: dict | None = None
        self._update_error = ""
        self._update_download_done = False
        self._update_download_error = ""
        self._update_download_path: Path | None = None
        self._update_download_bytes = 0
        self._update_download_total = 0
        self._update_download_cancel = False
        self._update_progress: QProgressDialog | None = None

        self.setWindowTitle(f"{APP_NAME} {APP_VERSION} — Live Events")
        self.resize(860, 640)
        self.setMinimumSize(660, 460)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 18, 18, 18)
        outer.setSpacing(10)

        title = QLabel("Live events")
        title.setStyleSheet("font-size: 22px; font-weight: 700;")
        outer.addWidget(title)

        subtitle = QLabel(
            "Choose an event, then choose Auto or a specific source. Auto probes the "
            "available embeds and feeds the highest verified video quality into the "
            "same 180-second DVR buffer."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("color: #aeb5bf;")
        outer.addWidget(subtitle)

        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search live events...")
        self.search.textChanged.connect(self.apply_filter)
        filters.addWidget(self.search, 1)

        self.category = QComboBox()
        self.category.addItem("All sports", "")
        self.category.currentIndexChanged.connect(self.apply_filter)
        filters.addWidget(self.category)

        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh_events)
        filters.addWidget(self.refresh_btn)

        self.update_btn = QPushButton("Check updates")
        self.update_btn.clicked.connect(self.on_update_button)
        filters.addWidget(self.update_btn)
        outer.addLayout(filters)

        self.status = QLabel("Loading live events...")
        self.status.setStyleSheet("color: #aeb5bf;")
        outer.addWidget(self.status)

        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _item: self.accept_selected())
        self.list.itemSelectionChanged.connect(self.update_watch_button)
        outer.addWidget(self.list, 1)

        actions = QHBoxLayout()
        version_label = QLabel(f"{APP_NAME} v{APP_VERSION}")
        version_label.setStyleSheet("color: #747b86;")
        actions.addWidget(version_label)
        actions.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        self.watch_btn = QPushButton("Choose source →")
        self.watch_btn.setEnabled(False)
        self.watch_btn.clicked.connect(self.accept_selected)
        actions.addWidget(self.watch_btn)
        outer.addLayout(actions)

        self.setStyleSheet(
            """
            QDialog { background: #0d0f12; color: #f2f3f5; font-family: Segoe UI, Arial; }
            QLineEdit, QComboBox, QListWidget {
                background: #15181d; border: 1px solid #343b45; border-radius: 7px;
                padding: 8px; color: #f2f3f5;
            }
            QListWidget::item { padding: 10px 8px; border-bottom: 1px solid #242932; }
            QListWidget::item:selected { background: #28313d; }
            QPushButton {
                background: #232830; border: 1px solid #343b45; border-radius: 7px;
                padding: 8px 14px;
            }
            QPushButton:hover { background: #2d333d; }
            QPushButton:disabled { color: #747b86; }
            """
        )

        self.update_check_timer = QTimer(self)
        self.update_check_timer.timeout.connect(self.poll_update_check)
        self.update_download_timer = QTimer(self)
        self.update_download_timer.timeout.connect(self.poll_update_download)

        QTimer.singleShot(0, self.refresh_events)
        if bool(self.settings.get("check_updates", True)):
            QTimer.singleShot(700, lambda: self.begin_update_check(manual=False))

    # ---------------------------- Updates ----------------------------

    def begin_update_check(self, manual: bool):
        if self._update_check_running:
            return
        self._update_check_running = True
        self._update_check_done = False
        self._update_check_manual = manual
        self._update_result = None
        self._update_error = ""
        self.update_btn.setEnabled(False)
        self.update_btn.setText("Checking…")

        def worker():
            try:
                self._update_result = fetch_latest_release()
            except Exception as exc:
                self._update_error = str(exc)
            finally:
                self._update_check_done = True

        threading.Thread(target=worker, daemon=True).start()
        self.update_check_timer.start(200)

    def poll_update_check(self):
        if not self._update_check_done:
            return
        self.update_check_timer.stop()
        self._update_check_running = False
        self.update_btn.setEnabled(True)

        if self._update_error:
            self.update_btn.setText("Check updates")
            if self._update_check_manual:
                QMessageBox.warning(self, "Update check", self._update_error)
            else:
                print(f"[UPDATE] {self._update_error}")
            return

        if self._update_result:
            self.latest_release = self._update_result
            self.update_btn.setText(f"Update v{self.latest_release['version']}")
            print(f"[UPDATE] v{self.latest_release['version']} is available")
            if self._update_check_manual:
                self.offer_update()
        else:
            self.latest_release = None
            self.update_btn.setText("Up to date")
            if self._update_check_manual:
                QMessageBox.information(
                    self,
                    "Updates",
                    f"You already have the latest release ({APP_VERSION}).",
                )

    def on_update_button(self):
        if self.latest_release:
            self.offer_update()
        else:
            self.begin_update_check(manual=True)

    def offer_update(self):
        release = self.latest_release
        if not release:
            return
        if not release.get("asset_url"):
            QMessageBox.warning(
                self,
                "Update available",
                f"StreamShift DVR {release['version']} is available, but that release "
                "does not contain the Windows installer asset yet.",
            )
            return

        notes = str(release.get("notes") or "").strip()
        if len(notes) > 1200:
            notes = notes[:1200].rstrip() + "…"
        message = (
            f"StreamShift DVR {release['version']} is available.\n\n"
            f"Installed: {APP_VERSION}\n\n"
        )
        if notes:
            message += f"Release notes:\n{notes}\n\n"
        message += "Download and install it now? StreamShift will close when the installer starts."

        answer = QMessageBox.question(
            self,
            "StreamShift update",
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.start_update_download(release)

    def start_update_download(self, release: dict):
        if self._update_progress is not None:
            return

        self._update_download_done = False
        self._update_download_error = ""
        self._update_download_path = None
        self._update_download_bytes = 0
        self._update_download_total = 0
        self._update_download_cancel = False

        progress = QProgressDialog(
            f"Downloading StreamShift DVR {release['version']}…",
            "Cancel",
            0,
            100,
            self,
        )
        progress.setWindowTitle("StreamShift update")
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setMinimumDuration(0)
        progress.show()
        self._update_progress = progress

        url = str(release.get("asset_url") or "")
        target = Path(tempfile.gettempdir()) / UPDATE_ASSET_NAME

        def worker():
            try:
                request = Request(
                    url,
                    headers={
                        "Accept": "application/octet-stream",
                        "User-Agent": f"StreamShift-DVR/{APP_VERSION}",
                    },
                )
                with urlopen(request, timeout=45) as response, target.open("wb") as output:
                    try:
                        self._update_download_total = int(response.headers.get("Content-Length") or 0)
                    except Exception:
                        self._update_download_total = 0
                    while True:
                        if self._update_download_cancel:
                            try:
                                target.unlink(missing_ok=True)
                            except Exception:
                                pass
                            return
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
                        self._update_download_bytes += len(chunk)
                self._update_download_path = target
            except Exception as exc:
                self._update_download_error = f"Update download failed: {exc}"
            finally:
                self._update_download_done = True

        threading.Thread(target=worker, daemon=True).start()
        self.update_download_timer.start(150)

    def poll_update_download(self):
        progress = self._update_progress
        if progress is None:
            self.update_download_timer.stop()
            return

        if progress.wasCanceled():
            self._update_download_cancel = True

        total = self._update_download_total
        if total > 0:
            percent = int(min(100, self._update_download_bytes * 100 / total))
            progress.setValue(percent)
            progress.setLabelText(
                f"Downloading update… {self._update_download_bytes / 1024 / 1024:.0f} / "
                f"{total / 1024 / 1024:.0f} MB"
            )
        else:
            progress.setValue(0)
            progress.setLabelText(
                f"Downloading update… {self._update_download_bytes / 1024 / 1024:.0f} MB"
            )

        if not self._update_download_done:
            return

        self.update_download_timer.stop()
        progress.close()
        self._update_progress = None

        if self._update_download_cancel:
            return
        if self._update_download_error:
            QMessageBox.critical(self, "Update failed", self._update_download_error)
            return
        installer = self._update_download_path
        if installer is None or not installer.exists():
            QMessageBox.critical(self, "Update failed", "The downloaded installer could not be found.")
            return

        try:
            subprocess.Popen(
                [
                    str(installer),
                    "/SILENT",
                    "/SUPPRESSMSGBOXES",
                    "/CLOSEAPPLICATIONS",
                    "/NORESTART",
                ],
                close_fds=True,
            )
        except Exception as exc:
            QMessageBox.critical(self, "Update failed", f"Could not start the installer: {exc}")
            return

        QApplication.quit()

    # ---------------------------- Events -----------------------------

    def refresh_events(self):
        self.refresh_btn.setEnabled(False)
        self.status.setText("Loading live events from Streamed API...")
        QApplication.processEvents()
        try:
            self.events = fetch_live_events()
        except Exception as exc:
            self.events = []
            self.status.setText(f"Could not load live events: {exc}")
            QMessageBox.critical(self, "Streamed API", str(exc))
            self.refresh_btn.setEnabled(True)
            return

        categories = sorted(
            {str(event.get("category") or "Other") for event in self.events},
            key=str.lower,
        )
        current_category = self.category.currentData() or ""
        self.category.blockSignals(True)
        self.category.clear()
        self.category.addItem("All sports", "")
        for category in categories:
            self.category.addItem(category.replace("-", " ").title(), category)
        index = self.category.findData(current_category)
        if index >= 0:
            self.category.setCurrentIndex(index)
        self.category.blockSignals(False)

        self.refresh_btn.setEnabled(True)
        self.apply_filter()

    def apply_filter(self):
        search_text = self.search.text().strip().lower()
        category = str(self.category.currentData() or "")
        self.list.clear()

        visible = 0
        for event in self.events:
            title = str(event.get("title") or "Untitled event")
            event_category = str(event.get("category") or "Other")
            if category and event_category != category:
                continue
            haystack = f"{title} {event_category}".lower()
            if search_text and search_text not in haystack:
                continue

            sources = event.get("sources") or []
            popular = "★ " if event.get("popular") else ""
            text = (
                f"{popular}{title}\n"
                f"{event_category.replace('-', ' ').title()}  •  {len(sources)} provider(s)"
            )
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, event)
            self.list.addItem(item)
            visible += 1

        self.status.setText(f"{visible} live event(s) shown • {len(self.events)} total")
        self.update_watch_button()

    def update_watch_button(self):
        self.watch_btn.setEnabled(self.list.currentItem() is not None)

    def accept_selected(self):
        item = self.list.currentItem()
        if item is None:
            return
        self.selected_event = item.data(Qt.ItemDataRole.UserRole)
        self.accept()


class SourcePicker(QDialog):
    """Let the user use automatic best-quality selection or force one API source."""

    def __init__(
        self,
        event_title: str,
        stream_options: list[StreamOption],
        settings: dict,
        parent=None,
    ):
        super().__init__(parent)
        self.stream_options = list(stream_options)
        self.settings = settings
        self.selected_option: StreamOption | None = None

        self.setWindowTitle(f"Choose source — {event_title}")
        self.resize(720, 560)
        self.setMinimumSize(560, 420)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 18, 18, 18)
        outer.setSpacing(10)

        title = QLabel(event_title)
        title.setStyleSheet("font-size: 20px; font-weight: 700;")
        title.setWordWrap(True)
        outer.addWidget(title)

        explanation = QLabel(
            "Auto is recommended: StreamShift checks the available providers and uses the "
            "highest verified video quality. Choose a specific provider/stream when you want "
            "different commentary, reliability, or picture characteristics."
        )
        explanation.setWordWrap(True)
        explanation.setStyleSheet("color: #aeb5bf;")
        outer.addWidget(explanation)

        self.list = QListWidget()
        auto_item = QListWidgetItem(
            f"★ Auto — Best verified quality\nProbe all {len(stream_options)} available stream(s)"
        )
        auto_item.setData(Qt.ItemDataRole.UserRole, None)
        self.list.addItem(auto_item)

        preferred = str(settings.get("preferred_source") or "").lower()
        preferred_row = 0
        for option in self.stream_options:
            badge = "HD" if option.hd else "SD"
            language = option.language or "Unknown language"
            text = f"{option.source} #{option.stream_no}  •  {badge}\n{language}"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, option)
            self.list.addItem(item)
            if preferred and option.source.lower() == preferred and preferred_row == 0:
                preferred_row = self.list.count() - 1

        self.list.setCurrentRow(preferred_row)
        self.list.itemDoubleClicked.connect(lambda _item: self.accept_selected())
        outer.addWidget(self.list, 1)

        self.remember = QCheckBox("Remember this provider as my preferred source")
        self.remember.setToolTip(
            "Future events will preselect this provider when it is available. "
            "You can always choose Auto or another provider."
        )
        outer.addWidget(self.remember)

        actions = QHBoxLayout()
        actions.addStretch(1)
        cancel = QPushButton("Back")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        watch = QPushButton("Start DVR")
        watch.clicked.connect(self.accept_selected)
        actions.addWidget(watch)
        outer.addLayout(actions)

        self.setStyleSheet(
            """
            QDialog { background: #0d0f12; color: #f2f3f5; font-family: Segoe UI, Arial; }
            QListWidget {
                background: #15181d; border: 1px solid #343b45; border-radius: 7px;
                padding: 6px; color: #f2f3f5;
            }
            QListWidget::item { padding: 11px 9px; border-bottom: 1px solid #242932; }
            QListWidget::item:selected { background: #28313d; }
            QPushButton {
                background: #232830; border: 1px solid #343b45; border-radius: 7px;
                padding: 8px 14px;
            }
            QPushButton:hover { background: #2d333d; }
            QCheckBox { padding: 5px 0; }
            """
        )

    def accept_selected(self):
        item = self.list.currentItem()
        if item is None:
            return
        option = item.data(Qt.ItemDataRole.UserRole)
        self.selected_option = option if isinstance(option, StreamOption) else None

        if self.remember.isChecked():
            self.settings["preferred_source"] = (
                self.selected_option.source if self.selected_option else ""
            )
            save_app_settings(self.settings)
        self.accept()


# ============================================================
# MAIN
# ============================================================

def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)

    settings = load_app_settings()

    try:
        ffmpeg_path, ffprobe_path = find_ffmpeg_tools()
        print(f"[FFMPEG] ffmpeg: {ffmpeg_path}")
        print(f"[FFMPEG] ffprobe: {ffprobe_path}")
        if BUNDLED_PLAYWRIGHT_DIR.exists():
            print(f"[PLAYWRIGHT] bundled Chromium: {BUNDLED_PLAYWRIGHT_DIR}")
    except Exception as exc:
        QMessageBox.critical(
            None,
            "FFmpeg required",
            f"{exc}\n\nOfficial Windows installers bundle FFmpeg automatically.",
        )
        return 1

    picker = EventPicker(settings)
    if picker.exec() != QDialog.DialogCode.Accepted or not picker.selected_event:
        return 0

    event = picker.selected_event
    event_title = str(event.get("title") or "Live event")

    QApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
    try:
        stream_options = fetch_stream_options(event)
    except Exception as exc:
        QApplication.restoreOverrideCursor()
        QMessageBox.critical(None, "Stream sources", str(exc))
        return 1
    QApplication.restoreOverrideCursor()

    if not stream_options:
        QMessageBox.warning(
            None,
            "No streams",
            "The Streamed API returned no usable embed URLs for this live event.",
        )
        return 1

    source_picker = SourcePicker(event_title, stream_options, settings)
    if source_picker.exec() != QDialog.DialogCode.Accepted:
        return 0

    selected_option = source_picker.selected_option
    active_options = [selected_option] if selected_option is not None else stream_options

    print(f"[API] Selected event: {event_title}")
    if selected_option is None:
        print(f"[SOURCE] Auto mode — probing all {len(active_options)} stream option(s)")
    else:
        print(f"[SOURCE] Manual mode — {selected_option.label}")
    for option in active_options:
        print(f"[API] {'HD' if option.hd else 'SD'} {option.label}: {option.embed_url}")

    STATE.folder = make_session_folder()

    server = LocalStreamServer()
    server.start()

    downloader = DownloaderThread(active_options, STATE, event_title)
    downloader.start()

    window = DVRPlayer(STATE, server, event_title)
    window.show()

    result = app.exec()

    STATE.stop_event.set()
    with STATE.condition:
        STATE.active_stream_id += 1
        STATE.condition.notify_all()

    server.shutdown()

    # Give the downloader thread time to terminate FFmpeg/Chromium before the
    # temporary DVR directory is removed.
    try:
        downloader.join(timeout=8)
    except Exception:
        pass

    if DELETE_SESSION_ON_EXIT and STATE.folder and STATE.folder.exists():
        try:
            shutil.rmtree(STATE.folder)
        except Exception:
            pass

    return result


if __name__ == "__main__":
    raise SystemExit(main())
