@echo off
setlocal EnableExtensions
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  py -3.12 -m venv .venv || goto :error
)

call .venv\Scripts\activate.bat || goto :error
python -m pip install --upgrade pip || goto :error
pip install -r requirements-build.txt || goto :error

if not exist "vendor\playwright" mkdir "vendor\playwright"
set "PLAYWRIGHT_BROWSERS_PATH=%CD%\vendor\playwright"
python -m playwright install chromium || goto :error

if not exist "vendor\ffmpeg\bin\ffmpeg.exe" (
  echo.
  echo FFmpeg is not bundled yet.
  echo Copy ffmpeg.exe and ffprobe.exe into vendor\ffmpeg\bin\ and run this file again.
  echo You can locate an installed copy with: where ffmpeg ^& where ffprobe
  goto :error
)

rmdir /s /q build 2>nul
rmdir /s /q "dist\StreamShift DVR" 2>nul
rmdir /s /q release 2>nul

pyinstaller --noconfirm --clean StreamShiftDVR.spec || goto :error

set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
  echo.
  echo Inno Setup 6 is required to build the installer.
  echo Install it from https://jrsoftware.org/isdl.php then run this file again.
  goto :error
)

"%ISCC%" installer.iss || goto :error

echo.
echo SUCCESS: release\StreamShift-DVR-Setup.exe
exit /b 0

:error
echo.
echo Build failed.
exit /b 1
