"""ffmpeg-based decoding/encoding and the end-to-end denoise job.

Audio is decoded by ffmpeg to raw float32 on a pipe, denoised block by block
and piped straight into an ffmpeg encoder, so files of any length are handled
with constant memory. For video inputs the cleaned audio goes to a temporary
lossless file first and is then muxed back with the original video stream
copied untouched (no re-encode, no quality loss).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

from .dsp import StreamingDenoiser

AUDIO_EXTS = {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma",
              ".aiff", ".aif", ".ac3", ".amr", ".mka", ".alac", ".caf", ".wv"}

# extension -> ffmpeg audio encoder arguments
AUDIO_CODECS = {
    ".wav": ["-c:a", "pcm_s16le"],
    ".flac": ["-c:a", "flac"],
    ".mp3": ["-c:a", "libmp3lame", "-q:a", "2"],
    ".m4a": ["-c:a", "aac", "-b:a", "192k"],
    ".aac": ["-c:a", "aac", "-b:a", "192k"],
    ".ogg": ["-c:a", "libvorbis", "-q:a", "6"],
    ".opus": ["-c:a", "libopus", "-b:a", "128k"],
    ".wma": ["-c:a", "wmav2", "-b:a", "192k"],
    ".aiff": ["-c:a", "pcm_s16be"],
    ".aif": ["-c:a", "pcm_s16be"],
    ".mka": ["-c:a", "flac"],
    ".ac3": ["-c:a", "ac3", "-b:a", "384k"],
}
# audio codec to use when muxing into a video container
VIDEO_AUDIO_CODECS = {
    ".mp4": ["-c:a", "aac", "-b:a", "192k"],
    ".m4v": ["-c:a", "aac", "-b:a", "192k"],
    ".mov": ["-c:a", "aac", "-b:a", "192k"],
    ".mkv": ["-c:a", "flac"],
    ".webm": ["-c:a", "libopus", "-b:a", "160k"],
    ".avi": ["-c:a", "libmp3lame", "-q:a", "2"],
    ".wmv": ["-c:a", "wmav2", "-b:a", "192k"],
    ".flv": ["-c:a", "aac", "-b:a", "192k"],
    ".ts": ["-c:a", "aac", "-b:a", "192k"],
    ".mts": ["-c:a", "aac", "-b:a", "192k"],
    ".m2ts": ["-c:a", "aac", "-b:a", "192k"],
    ".3gp": ["-c:a", "aac", "-b:a", "128k"],
    ".mpg": ["-c:a", "mp2", "-b:a", "224k"],
    ".mpeg": ["-c:a", "mp2", "-b:a", "224k"],
}

BLOCK_SECONDS = 2.0


class Cancelled(Exception):
    pass


class MediaError(Exception):
    pass


def _creationflags() -> int:
    # Keep ffmpeg from flashing a console window on Windows.
    return 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW


def find_tool(name: str) -> str:
    """Locate ffmpeg/ffprobe: bundled next to the app first, then PATH."""
    exe = name + (".exe" if sys.platform == "win32" else "")
    roots = []
    if getattr(sys, "frozen", False):
        roots.append(getattr(sys, "_MEIPASS", ""))
        roots.append(os.path.dirname(sys.executable))
    roots.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for root in roots:
        for sub in ("", "ffmpeg", os.path.join("ffmpeg", "bin")):
            cand = os.path.join(root, sub, exe)
            if root and os.path.isfile(cand):
                return cand
    found = shutil.which(name)
    if found:
        return found
    raise MediaError(f"{exe} not found. Put it in the 'ffmpeg' folder next to the app or on PATH.")


@dataclass
class MediaInfo:
    duration: float
    sample_rate: int
    channels: int
    has_video: bool
    audio_streams: int = 1


def probe(path: str) -> MediaInfo:
    cmd = [find_tool("ffprobe"), "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", path]
    res = subprocess.run(cmd, capture_output=True, creationflags=_creationflags())
    if res.returncode != 0:
        detail = res.stderr.decode(errors="replace").strip().splitlines()
        reason = detail[-1].split(": ")[-1] if detail else "unknown error"
        raise MediaError(f"Not a readable audio/video file ({reason})")
    data = json.loads(res.stdout.decode("utf-8", errors="replace") or "{}")
    streams = data.get("streams", [])
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    video = [s for s in streams if s.get("codec_type") == "video"
             and not s.get("disposition", {}).get("attached_pic")]
    if not audio:
        raise MediaError("File has no audio track.")
    a = audio[0]
    duration = 0.0
    for src in (data.get("format", {}).get("duration"), a.get("duration")):
        try:
            duration = float(src)
            if duration > 0:
                break
        except (TypeError, ValueError):
            pass
    sr = int(a.get("sample_rate") or 48000)
    ch = int(a.get("channels") or 2)
    return MediaInfo(duration, sr, max(1, min(ch, 8)), bool(video), len(audio))


def default_output_path(src: str, out_dir: Optional[str], out_ext: Optional[str]) -> str:
    base, ext = os.path.splitext(os.path.basename(src))
    ext = (out_ext or ext).lower()
    if not ext.startswith("."):
        ext = "." + ext
    if ext not in AUDIO_CODECS and ext not in VIDEO_AUDIO_CODECS:
        ext = ".wav"  # input container we can't write back (e.g. .amr): WAV always works
    folder = out_dir or os.path.dirname(os.path.abspath(src))
    path = os.path.join(folder, f"{base}_clean{ext}")
    n = 2
    while os.path.exists(path):
        path = os.path.join(folder, f"{base}_clean ({n}){ext}")
        n += 1
    return path


@dataclass
class JobOptions:
    strength: float = 0.7      # 0..1
    low_cut_hz: float = 70.0   # 0 disables
    keep_video: bool = True


ProgressFn = Callable[[float, str], None]


@dataclass
class _Procs:
    items: list = field(default_factory=list)

    def kill_all(self):
        for p in self.items:
            if p.poll() is None:
                try:
                    p.kill()
                except OSError:
                    pass


def _drain(stream, sink: list):
    # Read stderr in the background so ffmpeg can never block on a full pipe.
    for line in iter(stream.readline, b""):
        sink.append(line)
        if len(sink) > 200:
            del sink[:100]
    stream.close()


def _err_text(lines: list) -> str:
    return b"".join(lines[-15:]).decode(errors="replace").strip()


def denoise_file(src: str, dst: str, opts: JobOptions, progress: ProgressFn,
                 cancel: threading.Event) -> str:
    """Denoise src into dst. Calls progress(fraction 0..1, stage text)."""
    info = probe(src)
    ext = os.path.splitext(dst)[1].lower()
    to_video = info.has_video and opts.keep_video and ext not in AUDIO_EXTS
    if not to_video and ext not in AUDIO_CODECS and ext not in VIDEO_AUDIO_CODECS:
        raise MediaError(f"Unsupported output format: {ext}")

    os.makedirs(os.path.dirname(os.path.abspath(dst)) or ".", exist_ok=True)
    tmp_dir = tempfile.mkdtemp(prefix="noiseremover_")
    procs = _Procs()
    try:
        if to_video:
            clean_audio = os.path.join(tmp_dir, "clean.mka")
            enc_target, enc_codec = clean_audio, ["-c:a", "flac"]
            denoise_weight = 0.95
        else:
            enc_target = os.path.join(tmp_dir, "out" + ext)
            enc_codec = AUDIO_CODECS.get(ext) or VIDEO_AUDIO_CODECS[ext]
            denoise_weight = 1.0

        _denoise_stream(src, info, enc_target, enc_codec, opts, procs, cancel,
                        lambda f: progress(f * denoise_weight, "Removing noise"))

        if to_video:
            progress(denoise_weight, "Rebuilding video")
            mux_target = os.path.join(tmp_dir, "out" + ext)
            _mux(src, clean_audio, mux_target, ext, info, procs, cancel, progress, denoise_weight)
            enc_target = mux_target

        if cancel.is_set():
            raise Cancelled()
        shutil.move(enc_target, dst)
        progress(1.0, "Done")
        return dst
    finally:
        procs.kill_all()
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _denoise_stream(src, info: MediaInfo, target, codec_args, opts, procs, cancel, on_frac):
    sr, ch = info.sample_rate, info.channels
    ffmpeg = find_tool("ffmpeg")
    dec = subprocess.Popen(
        [ffmpeg, "-v", "error", "-nostdin", "-i", src, "-map", "0:a:0", "-vn", "-sn", "-dn",
         "-f", "f32le", "-acodec", "pcm_f32le", "-ac", str(ch), "-ar", str(sr), "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
        creationflags=_creationflags())
    procs.items.append(dec)
    enc = subprocess.Popen(
        [ffmpeg, "-v", "error", "-y", "-f", "f32le", "-ar", str(sr), "-ac", str(ch),
         "-i", "pipe:0", *codec_args, target],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        creationflags=_creationflags())
    procs.items.append(enc)

    dec_err, enc_err = [], []
    for p, sink in ((dec, dec_err), (enc, enc_err)):
        threading.Thread(target=_drain, args=(p.stderr, sink), daemon=True).start()

    den = StreamingDenoiser(sr, ch, strength=opts.strength, low_cut_hz=opts.low_cut_hz)
    frame_bytes = 4 * ch
    block_bytes = int(sr * BLOCK_SECONDS) * frame_bytes
    total = max(info.duration * sr, 1.0)
    done = 0
    leftover = b""

    def write(arr: np.ndarray):
        if arr.size:
            try:
                enc.stdin.write(np.ascontiguousarray(arr, dtype="<f4").tobytes())
            except (BrokenPipeError, OSError):
                raise MediaError(f"Encoder failed: {_err_text(enc_err)}")

    try:
        while True:
            if cancel.is_set():
                raise Cancelled()
            chunk = dec.stdout.read(block_bytes)
            if not chunk:
                break
            chunk = leftover + chunk
            usable = len(chunk) - len(chunk) % frame_bytes
            leftover = chunk[usable:]
            samples = np.frombuffer(chunk[:usable], dtype="<f4").reshape(-1, ch)
            write(den.process(samples))
            done += samples.shape[0]
            on_frac(min(done / total, 0.999))
        write(den.flush())
        enc.stdin.close()
    except Cancelled:
        raise
    finally:
        if dec.poll() is None and cancel.is_set():
            dec.kill()

    if dec.wait() != 0:
        raise MediaError(f"Could not decode audio: {_err_text(dec_err)}")
    if enc.wait() != 0:
        raise MediaError(f"Could not encode output: {_err_text(enc_err)}")
    if done == 0:
        raise MediaError("No audio could be decoded from this file.")
    on_frac(1.0)


def _mux(src, clean_audio, target, ext, info, procs, cancel, progress, start):
    audio_codec = VIDEO_AUDIO_CODECS.get(ext, ["-c:a", "aac", "-b:a", "192k"])
    cmd = [find_tool("ffmpeg"), "-v", "error", "-nostdin", "-y",
           "-i", src, "-i", clean_audio,
           "-map", "0:v", "-map", "1:a:0", "-map_metadata", "0",
           "-c:v", "copy", *audio_codec, "-shortest",
           "-progress", "pipe:1", "-nostats"]
    if ext in (".mp4", ".m4v", ".mov"):
        cmd += ["-movflags", "+faststart"]
    cmd.append(target)
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         stdin=subprocess.DEVNULL, creationflags=_creationflags())
    procs.items.append(p)
    err = []
    threading.Thread(target=_drain, args=(p.stderr, err), daemon=True).start()
    for line in iter(p.stdout.readline, b""):
        if cancel.is_set():
            p.kill()
            raise Cancelled()
        if line.startswith(b"out_time_us=") and info.duration > 0:
            try:
                t = int(line.split(b"=")[1]) / 1e6
                progress(start + (1 - start) * min(t / info.duration, 0.999), "Rebuilding video")
            except ValueError:
                pass
    if p.wait() != 0:
        raise MediaError(f"Could not rebuild video: {_err_text(err)}")
