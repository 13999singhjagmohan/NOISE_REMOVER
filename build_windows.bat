@echo off
REM Builds dist\NoiseRemover\NoiseRemover.exe (fully offline, ffmpeg bundled).
REM Requires Python 3.10+ for Windows (python.org) on PATH. Internet is needed
REM only for this one-time build; the finished app never goes online.
setlocal
cd /d "%~dp0"

if not exist .venv (
    python -m venv .venv || goto :error
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller || goto :error

if not exist ffmpeg\ffmpeg.exe (
    echo Downloading ffmpeg ^(one time^)...
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
      "$ErrorActionPreference='Stop';" ^
      "Invoke-WebRequest https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip -OutFile ffmpeg.zip;" ^
      "Expand-Archive ffmpeg.zip -DestinationPath ffmpeg_tmp -Force;" ^
      "New-Item -ItemType Directory -Force ffmpeg | Out-Null;" ^
      "Get-ChildItem ffmpeg_tmp -Recurse -Include ffmpeg.exe,ffprobe.exe | Copy-Item -Destination ffmpeg;" ^
      "Remove-Item ffmpeg.zip; Remove-Item ffmpeg_tmp -Recurse -Force" || goto :error
)

pyinstaller --noconfirm --clean --windowed --name NoiseRemover --icon installer\app.ico ^
  --add-binary "ffmpeg\ffmpeg.exe;ffmpeg" ^
  --add-binary "ffmpeg\ffprobe.exe;ffmpeg" ^
  --exclude-module PySide6.QtWebEngineCore --exclude-module PySide6.QtWebEngineWidgets ^
  --exclude-module PySide6.Qt3DCore --exclude-module PySide6.QtQuick --exclude-module PySide6.QtQml ^
  --exclude-module PySide6.QtMultimedia --exclude-module PySide6.QtPdf ^
  --exclude-module tkinter ^
  main.py || goto :error

echo.
echo Built: %CD%\dist\NoiseRemover\NoiseRemover.exe
echo Copy the whole dist\NoiseRemover folder anywhere (or zip it) to use it on any Windows PC.
exit /b 0

:error
echo Build failed.
exit /b 1
