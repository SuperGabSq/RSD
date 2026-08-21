"""Frequency-domain analysis for the optional FD plot.

At 2 Msps a 20 000-sample frame gives 100 Hz resolution and a 1 MHz Nyquist limit --
10 001 real FFT bins. That is ten times more than a plot is worth, so bins are reduced
to ~1 000 buckets.

The reduction is **max-per-bucket**, for the same reason the time-domain reduction is
min/max rather than stride: averaging or sampling bins would bury a narrow spur under
its neighbours, and a narrow spur is precisely what a spectrum plot exists to reveal.
Taking the maximum guarantees a peak survives the reduction, at the cost of slightly
raising the apparent noise floor -- an honest trade, and the conservative direction to
err in for an instrument.

A Hann window is applied first. Rectangular windowing of a tone that is not exactly
bin-centred smears energy across the whole spectrum (spectral leakage), which would
make the noise floor meaningless. Hann trades a little resolution for roughly 30 dB
less leakage. Magnitudes are corrected for the window's coherent gain so a full-scale
sine reads at its true amplitude rather than ~6 dB low.

This is the one genuinely expensive step in the pipeline (~300 us), which is why it
runs only for frames that are actually published, and only while the FD tab is visible.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import numpy as np

DEFAULT_SAMPLE_RATE_HZ = 2_000_000.0
DEFAULT_TARGET_BINS = 1_000
# Floor for the log: raw ADC counts are integers, so anything below a hundredth of a
# count is silence. Without a floor, an all-zero frame produces -inf and breaks the axis.
MAGNITUDE_FLOOR = 1e-2


@dataclass(frozen=True, slots=True)
class Spectrum:
    """``frequencies_hz`` and ``magnitudes_db`` are parallel, one entry per bucket."""

    frequencies_hz: np.ndarray
    magnitudes_db: np.ndarray
    bin_width_hz: float


@lru_cache(maxsize=4)
def _hann(n: int) -> np.ndarray:
    """Windows are cached: the frame length is constant in practice, so this is
    computed once per process rather than 30 times a second."""
    window = np.hanning(n)
    window.flags.writeable = False
    return window


class SpectrumAnalyzer:
    """Hann-windowed magnitude spectrum, reduced to ``target_bins`` buckets."""

    __slots__ = ("_sample_rate_hz", "_target_bins")

    def __init__(
        self,
        sample_rate_hz: float = DEFAULT_SAMPLE_RATE_HZ,
        target_bins: int = DEFAULT_TARGET_BINS,
    ) -> None:
        if sample_rate_hz <= 0:
            raise ValueError("sample_rate_hz must be positive")
        if target_bins <= 0:
            raise ValueError("target_bins must be positive")
        self._sample_rate_hz = sample_rate_hz
        self._target_bins = target_bins

    @property
    def target_bins(self) -> int:
        return self._target_bins

    def analyze(self, samples: np.ndarray, sample_rate_hz: float | None = None) -> Spectrum:
        fs = self._sample_rate_hz if sample_rate_hz is None else sample_rate_hz
        n = samples.shape[0]
        if n < 2:
            # A frame this short has no meaningful spectrum. Return empty rather than
            # raising: this runs on the acquisition path, where one degenerate frame
            # must not take down the session.
            empty = np.empty(0, dtype=np.float32)
            return Spectrum(frequencies_hz=empty, magnitudes_db=empty, bin_width_hz=0.0)

        window = _hann(n)
        spectrum = np.fft.rfft(samples.astype(np.float64) * window)

        # 2x for the discarded negative-frequency half; /sum(window) undoes the
        # window's coherent gain so amplitudes are comparable to the input's.
        amplitude = np.abs(spectrum) * (2.0 / window.sum())
        magnitudes_db = 20.0 * np.log10(np.maximum(amplitude, MAGNITUDE_FLOOR))

        frequencies = np.fft.rfftfreq(n, d=1.0 / fs)
        bin_width = float(fs) / n

        n_bins = magnitudes_db.shape[0]
        buckets = min(self._target_bins, n_bins)
        if buckets == n_bins:
            reduced_db = magnitudes_db
            reduced_hz = frequencies
        else:
            starts = (np.arange(buckets, dtype=np.int64) * n_bins) // buckets
            ends = np.append(starts[1:], n_bins)
            reduced_db = np.maximum.reduceat(magnitudes_db, starts)
            # Label each bucket with its centre frequency, so a peak's reported
            # frequency is not biased low by half a bucket.
            reduced_hz = (frequencies[starts] + frequencies[ends - 1]) * 0.5

        return Spectrum(
            frequencies_hz=reduced_hz.astype(np.float32),
            magnitudes_db=reduced_db.astype(np.float32),
            bin_width_hz=bin_width,
        )
