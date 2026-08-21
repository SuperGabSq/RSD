from __future__ import annotations

import numpy as np
import pytest

from backend.domain.spectrum import SpectrumAnalyzer

SAMPLE_RATE_HZ = 2_000_000.0
FRAME_SAMPLES = 20_000
TARGET_BINS = 1_000
AMPLITUDE = 200_000  # raw ADC counts, matching the simulator


@pytest.fixture
def analyzer() -> SpectrumAnalyzer:
    return SpectrumAnalyzer(sample_rate_hz=SAMPLE_RATE_HZ, target_bins=TARGET_BINS)


def tone(frequency_hz: float, amplitude: float = AMPLITUDE, n: int = FRAME_SAMPLES) -> np.ndarray:
    t = np.arange(n, dtype=np.float64) / SAMPLE_RATE_HZ
    return (amplitude * np.sin(2 * np.pi * frequency_hz * t)).astype(np.int32)


def bucket_width_hz(spectrum) -> float:
    return float(spectrum.frequencies_hz[1] - spectrum.frequencies_hz[0])


def test_single_tone_peaks_in_the_correct_bin(analyzer):
    """The core correctness assertion: a 50 kHz input peaks at 50 kHz, within one bin."""
    spectrum = analyzer.analyze(tone(50_000))
    peak_hz = float(spectrum.frequencies_hz[int(np.argmax(spectrum.magnitudes_db))])
    assert abs(peak_hz - 50_000) <= bucket_width_hz(spectrum)


@pytest.mark.parametrize("frequency_hz", [10_000, 210_000, 700_000, 950_000])
def test_peak_tracks_the_input_frequency_across_the_band(analyzer, frequency_hz):
    spectrum = analyzer.analyze(tone(frequency_hz))
    peak_hz = float(spectrum.frequencies_hz[int(np.argmax(spectrum.magnitudes_db))])
    assert abs(peak_hz - frequency_hz) <= bucket_width_hz(spectrum)


def test_resolves_the_simulators_three_tones(analyzer):
    """End-to-end sanity against the actual signal the simulator emits: all three tones
    must be visible above the surrounding floor, in the right places."""
    signal = (tone(50_000) + tone(210_000, AMPLITUDE / 2) + tone(700_000, AMPLITUDE / 4)).astype(
        np.int32
    )
    spectrum = analyzer.analyze(signal)
    width = bucket_width_hz(spectrum)

    for expected_hz in (50_000, 210_000, 700_000):
        near = np.abs(spectrum.frequencies_hz - expected_hz) <= width
        local_peak = spectrum.magnitudes_db[near].max()
        far = np.abs(spectrum.frequencies_hz - expected_hz) > 20 * width
        assert local_peak > spectrum.magnitudes_db[far].mean() + 40.0


def test_amplitude_is_corrected_for_the_windows_coherent_gain(analyzer):
    """Without the correction a Hann-windowed full-scale sine reads ~6 dB low, which
    would make every absolute magnitude on the plot wrong."""
    spectrum = analyzer.analyze(tone(100_000, amplitude=AMPLITUDE))
    expected_db = 20 * np.log10(AMPLITUDE)
    assert float(spectrum.magnitudes_db.max()) == pytest.approx(expected_db, abs=0.5)


def test_bin_metadata_matches_the_frame_geometry(analyzer):
    """20 000 samples at 2 Msps: 100 Hz resolution, 1 MHz Nyquist."""
    spectrum = analyzer.analyze(tone(50_000))
    assert spectrum.bin_width_hz == pytest.approx(100.0)
    assert spectrum.frequencies_hz[-1] <= SAMPLE_RATE_HZ / 2
    assert spectrum.frequencies_hz[-1] > SAMPLE_RATE_HZ / 2 * 0.99


def test_reduction_hits_the_target_bin_count_with_ascending_frequencies(analyzer):
    spectrum = analyzer.analyze(tone(50_000))
    assert analyzer.target_bins == TARGET_BINS
    assert spectrum.magnitudes_db.shape == (TARGET_BINS,)
    assert spectrum.frequencies_hz.shape == (TARGET_BINS,)
    assert np.all(np.diff(spectrum.frequencies_hz) > 0)
    assert spectrum.magnitudes_db.dtype == np.float32


def test_bucket_reduction_takes_the_maximum_so_a_narrow_spur_survives(analyzer):
    """Averaging bins would bury a single-bin spur under its ten quiet neighbours.
    A spectrum plot exists to reveal exactly that spur."""
    quiet = tone(50_000, amplitude=100)
    spur = tone(310_100, amplitude=AMPLITUDE)  # deliberately off a bucket boundary
    spectrum = analyzer.analyze((quiet + spur).astype(np.int32))

    width = bucket_width_hz(spectrum)
    near = np.abs(spectrum.frequencies_hz - 310_100) <= width
    assert spectrum.magnitudes_db[near].max() > 20 * np.log10(AMPLITUDE) - 3.0


def test_silence_produces_a_floored_spectrum_not_negative_infinity(analyzer):
    """An all-zero frame is legal input. log10(0) would render the axis unusable."""
    spectrum = analyzer.analyze(np.zeros(FRAME_SAMPLES, dtype=np.int32))
    assert np.all(np.isfinite(spectrum.magnitudes_db))
    assert float(spectrum.magnitudes_db.max()) == pytest.approx(-40.0)


def test_sample_rate_can_be_overridden_per_call(analyzer):
    """The measured rate can drift from nominal (the simulator's --rate-factor); the
    frequency axis has to follow the rate that actually produced the samples."""
    half_rate = SAMPLE_RATE_HZ / 2
    spectrum = analyzer.analyze(tone(50_000), sample_rate_hz=half_rate)
    assert spectrum.frequencies_hz[-1] <= half_rate / 2
    # The same buffer now describes a tone at half the frequency.
    peak_hz = float(spectrum.frequencies_hz[int(np.argmax(spectrum.magnitudes_db))])
    assert abs(peak_hz - 25_000) <= bucket_width_hz(spectrum)


def test_short_frame_keeps_full_resolution_instead_of_padding(analyzer):
    """Fewer FFT bins than target buckets: return what exists rather than inventing."""
    spectrum = analyzer.analyze(tone(50_000, n=512))
    assert spectrum.magnitudes_db.shape == (512 // 2 + 1,)


@pytest.mark.parametrize("n", [0, 1])
def test_degenerate_frame_returns_an_empty_spectrum_rather_than_raising(analyzer, n):
    """This runs on the acquisition path. One pathological frame must not end the
    session with a traceback."""
    spectrum = analyzer.analyze(np.zeros(n, dtype=np.int32))
    assert spectrum.magnitudes_db.size == 0
    assert spectrum.frequencies_hz.size == 0


@pytest.mark.parametrize(
    ("sample_rate_hz", "target_bins"),
    [(0.0, TARGET_BINS), (-1.0, TARGET_BINS), (SAMPLE_RATE_HZ, 0)],
)
def test_rejects_nonsense_configuration(sample_rate_hz, target_bins):
    with pytest.raises(ValueError):
        SpectrumAnalyzer(sample_rate_hz=sample_rate_hz, target_bins=target_bins)
