"""Command line: python -m noise_remover.cli input [output] [--strength 0.7]"""

import argparse
import sys
import threading

from .media import JobOptions, default_output_path, denoise_file


def main(argv=None):
    ap = argparse.ArgumentParser(description="Offline noise remover")
    ap.add_argument("input")
    ap.add_argument("output", nargs="?")
    ap.add_argument("--strength", type=float, default=0.7, help="0 (gentle) .. 1 (strong)")
    ap.add_argument("--low-cut", type=float, default=70.0, help="Hz, 0 to disable")
    ap.add_argument("--audio-only", action="store_true", help="drop video, write audio only")
    a = ap.parse_args(argv)
    out = a.output or default_output_path(a.input, None, None)
    opts = JobOptions(strength=a.strength, low_cut_hz=a.low_cut, keep_video=not a.audio_only)

    def prog(f, stage):
        if sys.stderr is None:  # windowed .exe has no console
            return
        sys.stderr.write(f"\r{stage}: {f * 100:5.1f}%")
        sys.stderr.flush()

    denoise_file(a.input, out, opts, prog, threading.Event())
    if sys.stderr is not None:
        sys.stderr.write(f"\nSaved {out}\n")


if __name__ == "__main__":
    main()
