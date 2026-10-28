# Noise Remover (offline, Windows)

A desktop app that removes background noise (hiss, fan/AC hum, room noise,
rumble) from **any audio or video file of any length**, entirely on your PC.
Nothing is uploaded and no internet connection is used.

- **Any format in:** WAV, MP3, FLAC, M4A/AAC, OGG, Opus, WMA, AIFF, AMR, AC3,
  MP4, MOV, MKV, WebM, AVI, WMV, FLV, TS/MTS, 3GP, MPG… (anything ffmpeg reads).
- **Any length:** audio is streamed through in 2-second blocks, so a 10-hour
  file uses the same ~70 MB of memory as a 10-second one. A 1-hour stereo file
  takes about 2 minutes on a laptop CPU.
- **Background processing:** work runs on a separate thread, so the window
  never freezes. Each file has its own progress bar, plus an overall bar with
  elapsed time and an estimate of time left. **Cancel** stops immediately and
  leaves no half-written files.
- **Videos stay videos:** the video stream is copied untouched (no re-encode,
  no quality loss); only the audio is replaced with the cleaned version.
- **Batch:** drop many files or whole folders at once. A bad file is marked
  *Failed* and the queue keeps going.

Output files are saved as `name_clean.ext` next to the original (or in a
folder you choose). Originals are never modified.

## Installing

Download `NoiseRemover-Setup-<version>.exe` from the repository's
**Releases** page (or from the latest *Build Windows installer* run under
**Actions**), run it, and follow the wizard. It adds a Start menu entry, an
optional desktop shortcut, and an uninstaller under Settings > Apps.

**Smart App Control / SmartScreen:** Windows only trusts apps signed by a
verified publisher. The installer is unsigned until signing is set up (below),
so Smart App Control blocks it, and SmartScreen shows "Windows protected your
PC" (click *More info* > *Run anyway*).

### Turning on code signing

The workflow `.github/workflows/build-windows.yml` signs every `.exe`, `.dll`
and `.pyd` plus the installer with Azure Artifact Signing (formerly Trusted
Signing) as soon as these repository secrets exist
(Settings > Secrets and variables > Actions):

| Secret | Value |
|---|---|
| `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` | an Entra app registration with the *Artifact Signing Certificate Profile Signer* role |
| `AZURE_SIGNING_ENDPOINT` | e.g. `https://eus.codesigning.azure.net/` |
| `AZURE_SIGNING_ACCOUNT` | the signing account name |
| `AZURE_SIGNING_PROFILE` | the certificate profile name |

No code changes are needed; the next build is signed.

## Using it

1. Run `NoiseRemover.exe`.
2. Drag files or folders onto the window (or click **Add files…**).
3. Pick a **Strength** (70% is a good default; lower it if voices sound thin,
   raise it for very noisy recordings) and an **Output format** ("Same as
   input" keeps the original format; videos stay videos).
4. Click **Remove noise**.

## Building the .exe yourself

On a Windows 10/11 PC:

1. Install Python 3.10+ from <https://www.python.org/downloads/windows/>
   (tick **Add python.exe to PATH**).
2. Open this folder and double-click `build_windows.bat`
   (or run it from a Command Prompt).

The script creates a virtual environment, installs the dependencies,
downloads ffmpeg once (from gyan.dev) into `ffmpeg\`, and builds
`dist\NoiseRemover\NoiseRemover.exe` with ffmpeg bundled inside. Copy or zip
the whole `dist\NoiseRemover` folder to use it on any Windows PC; it needs
nothing else installed. Internet is only needed during the build.

If you'd rather supply ffmpeg yourself, put `ffmpeg.exe` and `ffprobe.exe`
in an `ffmpeg\` folder here before building and the download is skipped.

### Running from source (any OS)

```
pip install -r requirements.txt
python main.py                                   # GUI
python -m noise_remover.cli input.mp4            # command line
python -m noise_remover.cli in.wav out.flac --strength 0.5
```

ffmpeg/ffprobe must be in `ffmpeg/` next to `main.py` or on PATH.

## How it works

`noise_remover/dsp.py` is a streaming speech-preserving spectral suppressor:

1. Audio is cut into overlapping ~43 ms frames (STFT, square-root Hann window,
   75% overlap) and turned into a spectrum.
2. The **noise spectrum is tracked continuously** per frequency bin by
   following the minimum of the smoothed signal power, so it adapts if the
   noise changes during a long recording (AC turning on, moving rooms) and
   doesn't need a "noise sample".
3. For each frame a **Wiener gain** is computed using the decision-directed
   a-priori SNR estimator (Ephraim–Malah). Bins dominated by speech pass
   through; bins dominated by noise are turned down.
4. Gains are smoothed across frequency and floored (−6 dB to −30 dB depending
   on Strength), which avoids the warbly "musical noise" artifact and keeps
   voices natural instead of gated.
5. Optional low-cut removes mains hum and rumble below 70 Hz.
6. Frames are overlap-added back to audio. The reconstruction is exact (verified:
   with all gains at 1 the output equals the input), so only noise is removed.

`noise_remover/media.py` pipes ffmpeg's decoder → denoiser → ffmpeg's encoder,
reports progress from samples processed vs. the duration from ffprobe, and for
videos remuxes the cleaned audio with the original video stream.

Tested on synthetic speech + white noise + 50 Hz hum: SNR improved from
3.8 dB to 11.6 dB (WAV), with ~23× real-time speed.

## Project layout

```
main.py                  entry point (GUI; `--cli` for command line)
noise_remover/dsp.py     streaming noise suppression
noise_remover/media.py   ffmpeg probing, decode/encode, video remux, job runner
noise_remover/gui.py     PySide6 window, background worker, progress, cancel
noise_remover/cli.py     command line interface
build_windows.bat        one-click local Windows .exe build
installer/               Inno Setup installer script and app icon
.github/workflows/       cloud build of the installer (+ optional signing)
```

## Possible upgrades

For heavier noise (traffic, babble, keyboard clicks) a neural model such as
RNNoise or DeepFilterNet can be dropped in place of `StreamingDenoiser`
(same block-in/block-out interface). It would still run offline but makes the
download much larger.
