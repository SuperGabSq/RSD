"""S2: the ctypes-bound C decimator must agree with numpy, and must be optional.

Two claims, and the second matters more than the first. Byte-identical output is what
makes the C path safe to enable; the numpy fallback is what makes it safe to *ship*,
because the library is a build artefact and build artefacts go missing.
"""

from __future__ import annotations

import numpy as np
import pytest

from backend.domain import decimation
from backend.domain.decimation import MinMaxDecimator

pytestmark = pytest.mark.skipif(
    decimation._LIB is None,
    reason="libminmax.so not built; the numpy path is covered by test_decimation.py",
)


@pytest.fixture
def no_native(monkeypatch):
    """Hide the library, exactly as a missing .so would."""
    monkeypatch.setattr(decimation, "_LIB", None)


@pytest.mark.parametrize(
    "n",
    [
        20_000,  # the real frame
        19_995,  # --bad-frame-every: short by whole samples
        19_999,  # --malformed-every: ragged bucket edges
        1_001,  # one more sample than columns
        1_000,  # exactly one sample per column
        999,  # fewer samples than columns
        7,  # degenerate
    ],
)
@pytest.mark.parametrize("seed", [0, 1, 2])
def test_c_and_numpy_paths_are_byte_identical(n, seed, monkeypatch):
    """The C bucket edges are `(i * n) / columns` in int64 precisely so this holds on
    the ragged shapes, which are the ones the fault injector produces."""
    rng = np.random.default_rng(seed)
    samples = rng.integers(-(2**31), 2**31 - 1, size=n, dtype=np.int32)
    decimator = MinMaxDecimator(1_000)

    from_c = decimator.decimate(samples).values.copy()
    monkeypatch.setattr(decimation, "_LIB", None)
    from_numpy = decimator.decimate(samples).values

    assert np.array_equal(from_c, from_numpy)


def test_falls_back_to_numpy_when_the_library_is_absent(no_native):
    """The branch that keeps a missing build artefact from being an outage."""
    samples = np.arange(20_000, dtype=np.int32)
    envelope = MinMaxDecimator(1_000).decimate(samples)

    assert envelope.columns == 1_000
    assert envelope.values[0] == 0  # min of the first bucket
    assert envelope.values[1] == 19  # max of the first bucket
