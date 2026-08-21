"""Decimation tests.

The important one is `test_preserves_a_single_sample_spike...`: it is the assertion that
this project did not take the cheap route. Stride decimation would pass every other test
in this file and fail that one, which is exactly why it is written down.
"""

from __future__ import annotations

import numpy as np
import pytest

from backend.domain.decimation import MinMaxDecimator

FRAME_SAMPLES = 20_000
COLUMNS = 1_000


@pytest.fixture
def decimator() -> MinMaxDecimator:
    return MinMaxDecimator(target_columns=COLUMNS)


def test_output_shape_and_dtype_match_the_wire_format(decimator):
    """Interleaved [min, max] int32 -- the exact bytes the TD waveform message carries."""
    envelope = decimator.decimate(np.arange(FRAME_SAMPLES, dtype=np.int32))
    assert envelope.columns == COLUMNS
    assert envelope.values.shape == (COLUMNS * 2,)
    assert envelope.values.dtype == np.int32


def test_values_are_exact_raw_counts_not_rescaled(decimator):
    """A measurement instrument must display the numbers it received. int32 in,
    int32 out, no float round-trip anywhere on this path."""
    samples = np.full(FRAME_SAMPLES, 2_147_483_647, dtype=np.int32)  # int32 max
    envelope = decimator.decimate(samples)
    assert np.all(envelope.values == 2_147_483_647)


def test_preserves_a_single_sample_spike_that_stride_decimation_would_lose(decimator):
    """THE anti-aliasing assertion.

    One sample in 20 000 is set to a large value at an index that stride decimation
    (every 20th sample) does not visit. Min/max keeps it because it is the maximum of
    its bucket; stride sampling would drop it silently and the operator would never
    know a transient occurred.
    """
    samples = np.zeros(FRAME_SAMPLES, dtype=np.int32)
    spike_index = 12_345  # 12345 % 20 != 0, so a stride of 20 skips it entirely
    assert spike_index % (FRAME_SAMPLES // COLUMNS) != 0
    samples[spike_index] = 1_000_000

    envelope = decimator.decimate(samples)

    assert envelope.maxs.max() == 1_000_000
    # ...and it lands in the right column, so the plot puts it at the right time.
    assert int(np.argmax(envelope.maxs)) == spike_index // (FRAME_SAMPLES // COLUMNS)


def test_negative_transients_survive_too(decimator):
    """Min and max are both kept; an instrument that only tracked peaks would miss
    half of every fault."""
    samples = np.zeros(FRAME_SAMPLES, dtype=np.int32)
    samples[7_777] = -999_999
    envelope = decimator.decimate(samples)
    assert envelope.mins.min() == -999_999


def test_envelope_bounds_every_sample_in_its_column(decimator):
    """The general property: nothing that happened inside a column escapes the band
    drawn for that column. Asserted over the whole buffer, not a sampled few."""
    rng = np.random.default_rng(1234)
    samples = rng.integers(-2**20, 2**20, size=FRAME_SAMPLES, dtype=np.int32)

    envelope = decimator.decimate(samples)
    per_column = samples.reshape(COLUMNS, FRAME_SAMPLES // COLUMNS)

    assert np.array_equal(envelope.mins, per_column.min(axis=1))
    assert np.array_equal(envelope.maxs, per_column.max(axis=1))
    assert np.all(envelope.mins <= envelope.maxs)


def test_mins_and_maxs_are_views_not_copies(decimator):
    """They are strided views into the interleaved buffer -- no extra allocation on a
    path that runs 30 times a second."""
    envelope = decimator.decimate(np.arange(FRAME_SAMPLES, dtype=np.int32))
    assert envelope.mins.base is envelope.values
    assert envelope.maxs.base is envelope.values


def test_ragged_frame_is_bucketed_without_dropping_samples(decimator):
    """Short/malformed frames are not a multiple of the column count. The remainder is
    spread across buckets rather than truncated, so no sample goes unrepresented."""
    samples = np.arange(19_995, dtype=np.int32)  # the --bad-frame-every case
    envelope = decimator.decimate(samples)

    assert envelope.columns == COLUMNS
    assert envelope.mins[0] == 0
    assert envelope.maxs[-1] == 19_994  # the final sample is still in the envelope
    assert np.all(np.diff(envelope.mins) > 0)  # monotonic input -> monotonic buckets


def test_short_frame_gets_one_column_per_sample(decimator):
    """Fewer samples than columns: never interpolate. Inventing columns would draw
    detail the instrument never measured."""
    samples = np.array([5, -3, 11], dtype=np.int32)
    envelope = decimator.decimate(samples)
    assert envelope.columns == 3
    assert np.array_equal(envelope.mins, samples)
    assert np.array_equal(envelope.maxs, samples)


def test_empty_frame_produces_an_empty_envelope(decimator):
    envelope = decimator.decimate(np.empty(0, dtype=np.int32))
    assert envelope.columns == 0
    assert envelope.values.size == 0


def test_target_column_count_is_configurable():
    """Column count is a display concern, so it is configuration rather than a
    constant: a wider viewport should be able to ask for more detail."""
    wide = MinMaxDecimator(target_columns=2_000)
    assert wide.target_columns == 2_000
    assert wide.decimate(np.arange(FRAME_SAMPLES, dtype=np.int32)).columns == 2_000


@pytest.mark.parametrize("bad", [0, -5])
def test_rejects_nonsense_column_count(bad):
    with pytest.raises(ValueError):
        MinMaxDecimator(target_columns=bad)
