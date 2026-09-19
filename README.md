# StreamShift DVR

StreamShift DVR is a Windows desktop live-stream player built around the Streamed API, Chromium/Playwright source discovery, FFmpeg/ffprobe verification, and a local 180-second DVR buffer.

## What the app does

On launch it loads the currently live events from the Streamed API. After selecting an event, you can choose either **Auto — Best verified quality** or an individual provider/stream. Auto checks the available embeds and feeds the highest real video quality it can verify into FFmpeg. The selected stream is then remuxed into the local DVR pipeline used by the PySide6 player.

The player keeps the existing DVR controls: pause, rewind, seek, jump to live, playback speed, screenshots, fullscreen, always-on-top, and rolling cleanup of old buffer data.

## Source selection

After choosing an event, StreamShift shows every stream returned by the API, for example `admin #1`, `delta #1`, `golf #1`, or `hotel #1`, together with language and the API's HD/SD flag.

- **Auto — Best verified quality** probes all available options and chooses the highest verified video quality.
- Selecting a specific source restricts source discovery to that stream. This is useful for alternate commentary, stability, or a provider you prefer.
- The **Remember this provider** checkbox stores your preferred provider in `%APPDATA%\StreamShift DVR\settings.json`. It is only a preference; you can choose a different source on any event.

## Windows install — normal users

You should not need Python, Playwright, or FFmpeg on a normal Windows 11 machine.

1. Open the repository's **Releases** page.
2. Download `StreamShift-DVR-Setup.exe` from the latest release.
3. Run the installer.
4. Start **StreamShift DVR** from the Start Menu or desktop shortcut.

The official installer bundles the Python application, PySide6 runtime, Chromium, FFmpeg, and ffprobe.

> The installer is currently unsigned. Windows SmartScreen may therefore show an unknown-publisher warning until a code-signing certificate is added.

## Updates

StreamShift checks the repository's latest GitHub Release at startup. If a newer semantic version is available, the event window changes the update button to `Update vX.Y.Z`.

Selecting it downloads the release's `StreamShift-DVR-Setup.exe`, starts the installer, and closes the current app. Settings stored under `%APPDATA%\StreamShift DVR` survive upgrades.

You can also press **Check updates** at any time from the live-events window.

## Building a release automatically

The repository contains `.github/workflows/release.yml`.

Creating and pushing a tag such as:

```text
v1.0.0
```

runs a Windows GitHub Actions build that:

1. installs the Python build dependencies,
2. downloads Playwright Chromium,
3. downloads FFmpeg + ffprobe,
4. packages the app with PyInstaller,
5. builds `StreamShift-DVR-Setup.exe` with Inno Setup,
6. publishes the installer to that GitHub Release.

For later versions, tag `v1.0.1`, `v1.1.0`, etc. The workflow injects the tag version into the packaged application and installer before building.

The workflow can also be started manually from **Actions → Build Windows release → Run workflow**. Manual runs create a downloadable Actions artifact but do not create a GitHub Release.

## Building locally on Windows

Local builds are optional; GitHub Actions is the intended release builder.

Requirements for a local installer build:

- Python 3.12
- Inno Setup 6
- `ffmpeg.exe` and `ffprobe.exe` copied into `vendor\ffmpeg\bin\`

Then run:

```bat
build_release.bat
```

The resulting installer is written to:

```text
release\StreamShift-DVR-Setup.exe
```

## Running from source

```bat
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
python dvr_player.py
```

When running from source, FFmpeg and ffprobe must be available in `PATH`. Packaged releases use the bundled copies instead.

## Storage

DVR session data is stored under:

```text
%USERPROFILE%\Videos\Buffered Livestream\
```

Screenshots are stored under:

```text
%USERPROFILE%\Pictures\Buffered Livestream Screenshots\
```

Normal exit deletes the current temporary DVR session by default. Application preferences are kept separately in:

```text
%APPDATA%\StreamShift DVR\settings.json
```

## Responsible use

Use StreamShift only for streams you are authorized to access and in accordance with the relevant service terms and applicable law. Streaming providers can change their APIs, embeds, codecs, or delivery methods at any time, so future releases may need compatibility updates.
