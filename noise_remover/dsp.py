"""Streaming spectral noise suppression.

The signal is split into overlapping STFT frames. For every frame the noise
power spectrum is tracked continuously (minimum tracking on smoothed power),
and a Wiener gain is computed with the decision-directed a-priori SNR
estimator (Ephraim-Malah style). Gains are smoothed across frequency and
floored so speech stays natural and "musical noise" is kept down.

Everything is incremental: audio is fed in blocks of any size and comes back
out in blocks, so memory use is constant regardless of file length.
"""

from __future__ import annotations

import numpy as np


def _fft_size(sample_rate: int) -> int:
    # ~42 ms frames, rounded to a power of two
    if sample_rate <= 12000:
        return 512
    if sample_rate <= 24000:
        return 1024
    if sample_rate <= 48000:
        return 2048
    return 4096


class StreamingDenoiser:
    """Denoise multi-channel float32 audio fed block by block.

    strength: 0.0 (gentle) .. 1.0 (aggressive)
    low_cut_hz: frequencies below this are attenuated to the gain floor (0 disables)
    """

    def __init__(self, sample_rate: int, channels: int, strength: float = 0.7,
                 low_cut_hz: float = 70.0):
        self.sr = int(sample_rate)
        self.ch = int(channels)
        self.n_fft = _fft_size(self.sr)
        self.hop = self.n_fft // 4
        self.bins = self.n_fft // 2 + 1

        n = np.arange(self.n_fft)
        hann = 0.5 - 0.5 * np.cos(2 * np.pi * n / self.n_fft)  # periodic Hann
        self.win = np.sqrt(hann).astype(np.float64)
        self.ola_norm = 1.0 / 2.0  # sum of hann at 75% overlap == 2

        strength = float(np.clip(strength, 0.0, 1.0))
        floor_db = -6.0 - 24.0 * strength          # -6 dB .. -30 dB
        self.gain_floor = 10.0 ** (floor_db / 20.0)
        self.over_sub = 1.0 + 1.0 * strength       # noise overestimation factor
        self.dd_alpha = 0.98                       # decision-directed smoothing

        fps = self.sr / self.hop
        self.psd_smooth = 0.7                      # smoothing of power for tracking
        self.noise_rise = 10.0 ** ((4.0 / 10.0) / fps)   # noise floor may rise 4 dB/s
        self.minstat_bias = 1.5                    # min-tracking underestimates noise

        freqs = np.fft.rfftfreq(self.n_fft, 1.0 / self.sr)
        self.low_cut = freqs < low_cut_hz if low_cut_hz > 0 else np.zeros(self.bins, bool)

        # state
        pad = self.n_fft - self.hop
        self.inbuf = np.zeros((self.ch, pad))
        self.carry = np.zeros((self.ch, pad))
        self.skip = pad                 # algorithmic delay to drop from the output start
        self.total_in = 0
        self.total_out = 0
        self.noise = None               # (ch, bins) noise power estimate
        self.smooth_p = None
        self.prev_gain = np.ones((self.ch, self.bins))
        self.prev_post = np.ones((self.ch, self.bins))

    # ------------------------------------------------------------------ public
    def process(self, block: np.ndarray) -> np.ndarray:
        """block: (n_samples, channels) float32 -> denoised (m_samples, channels)."""
        if block.ndim == 1:
            block = block[:, None]
        self.total_in += block.shape[0]
        self.inbuf = np.concatenate([self.inbuf, block.T.astype(np.float64)], axis=1)
        return self._run()

    def flush(self) -> np.ndarray:
        """Push the remaining samples through and return the tail."""
        self.inbuf = np.concatenate([self.inbuf, np.zeros((self.ch, 2 * self.n_fft))], axis=1)
        out = self._run(final=True)
        remaining = self.total_in - self.total_out
        if remaining < out.shape[0]:
            out = out[:max(remaining, 0)]
        self.total_out += out.shape[0]
        return out

    # ---------------------------------------------------------------- internals
    def _run(self, final: bool = False) -> np.ndarray:
        n_fft, hop = self.n_fft, self.hop
        avail = self.inbuf.shape[1]
        if avail < n_fft:
            return np.zeros((0, self.ch), np.float32)
        n_frames = (avail - n_fft) // hop + 1

        idx = np.arange(n_fft)[None, :] + hop * np.arange(n_frames)[:, None]
        frames = self.inbuf[:, idx] * self.win            # (ch, F, n_fft)
        spec = np.fft.rfft(frames, axis=-1)               # (ch, F, bins)
        power = spec.real ** 2 + spec.imag ** 2

        gains = self._gains(power)                        # (ch, F, bins)
        frames_out = np.fft.irfft(spec * gains, n=n_fft, axis=-1) * (self.win * self.ola_norm)

        out_len = (n_frames - 1) * hop + n_fft
        out = np.zeros((self.ch, out_len))
        out[:, :self.carry.shape[1]] += self.carry
        for i in range(n_frames):
            out[:, i * hop:i * hop + n_fft] += frames_out[:, i]

        consumed = n_frames * hop
        self.inbuf = self.inbuf[:, consumed:]
        self.carry = out[:, consumed:].copy()
        emitted = out[:, :consumed]

        if self.skip:
            drop = min(self.skip, emitted.shape[1])
            emitted = emitted[:, drop:]
            self.skip -= drop

        result = emitted.T.astype(np.float32)
        if not final:
            self.total_out += result.shape[0]
        return result

    def _gains(self, power: np.ndarray) -> np.ndarray:
        ch, n_frames, bins = power.shape
        eps = 1e-12

        if self.noise is None:
            # Initial noise estimate: low percentile of the first block's frames
            # (callers feed >= 1 s first), robust even when the file starts with speech.
            self.noise = np.percentile(power, 20, axis=1) + eps
            self.smooth_p = self.noise.copy()

        gains = np.empty_like(power)
        a = self.dd_alpha
        ps = self.psd_smooth
        for t in range(n_frames):
            p = power[:, t]
            # --- noise tracking (continuous minimum tracking on smoothed power)
            self.smooth_p = ps * self.smooth_p + (1 - ps) * p
            rising = self.noise * self.noise_rise
            self.noise = np.where(self.smooth_p < rising,
                                  0.6 * self.noise + 0.4 * self.smooth_p,
                                  rising)
            self.noise = np.maximum(self.noise, eps)
            noise_est = self.noise * self.minstat_bias * self.over_sub

            # --- decision-directed a-priori SNR and Wiener gain
            post = p / noise_est
            prio = a * (self.prev_gain ** 2) * self.prev_post + (1 - a) * np.maximum(post - 1.0, 0.0)
            g = prio / (1.0 + prio)

            # smooth across frequency to suppress isolated "musical" bins
            g[:, 1:-1] = 0.25 * g[:, :-2] + 0.5 * g[:, 1:-1] + 0.25 * g[:, 2:]
            g = np.maximum(g, self.gain_floor)
            g[:, self.low_cut] = self.gain_floor

            self.prev_gain = g
            self.prev_post = np.minimum(post, 1e4)
            gains[:, t] = g

        return gains
