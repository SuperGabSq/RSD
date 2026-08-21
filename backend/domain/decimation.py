"""Min/max decimation for the time-domain plot.

20 000 samples per frame cannot be drawn into ~1 000 screen columns without reduction.
There are two ways to reduce, and only one of them is honest.

**Stride sampling** (keep every 20th sample) is fewer lines of code and gives a *wrong*
picture: it is undersampling, so any content above the new Nyquist frequency folds down
and appears as a lower-frequency artefact that is not in the signal. A 700 kHz tone can
render as a slow wobble. Worse, a single-sample transient falls between strides and
vanishes entirely -- exactly the event an engineer is looking for.

**Min/max** keeps the smallest and largest sample in each column. Nothing that happened
inside the column can escape the drawn band, so the envelope is preserved and spikes
survive. This is what an oscilloscope shows and what an engineer expects to see.

Cost is ~30 us per frame in numpy, with the GIL released for the reduction, so the
correctness is essentially free. YAGNI argues against unneeded features, never against
needed correctness.

Output is interleaved ``[min, max]`` per column and stays ``int32``: these are raw ADC
counts, and a measurement instrument must not quietly round the numbers it displays.
"""

from __future__ import annotations

import ctypes
import logging
import os
from dataclasses import dataclass

import numpy as np

log = logging.getLogger(__name__)

# 500 columns, not 1 000, and the reason is the canvas rather than the arithmetic.
#
# Min/max emits *two* points per column, so N columns is 2N vertices. The plot is about
# 1 000 px wide, so 1 000 columns put two vertices on every pixel -- a sub-pixel zigzag
# under a filled band spanning the full plot height, which is close to the worst case a
# 2D rasteriser can be handed. Measured against the real app, time-domain redraws
# collapsed to 7/s on a 30 Hz stream while the frequency-domain plot, drawing one line
# over the same 1 000 bins, held 31/s. A CPU profile put 97 % of the time in browser
# paint and ~0 in our decode loop, so no amount of tuning on this side of the wire would
# have moved it.
#
#     columns      1000   800   640   500   250
#     TD draws/s      7    22    31    29    31
#
# The knee is between 800 and 640, where vertex density crosses one per pixel; 500 sits
# comfortably past it. Nothing is lost by going there: min/max preserves the exact peak
# excursions at any column count -- that is what it is for -- so this trades horizontal
# resolution from one column per pixel to one per two, and no spike disappears.
DEFAULT_TARGET_COLUMNS = 500

# Optional C implementation of the same reduction, bound with ctypes -- stdlib, so the
# dependency rule still holds. It is not here for speed: numpy already does this in C in
# ~30 us. It is here because the boundary between Python and a compiled library is a
# thing this system has to demonstrate, and 40 lines of kernel is the honest size for it.
#
# Missing library, wrong architecture, no compiler in the image -> `_load()` returns None
# and the numpy path runs. A build-time optimisation that can take the app down is not an
# optimisation; a test hides the .so to prove this branch is real.
_LIB_PATH = os.path.join(os.path.dirname(__file__), os.pardir, "native", "libminmax.so")

_INT32_ARRAY = np.ctypeslib.ndpointer(dtype=np.int32, flags="C_CONTIGUOUS")


def _load():
    try:
        lib = ctypes.CDLL(os.path.abspath(_LIB_PATH))
    except OSError:
        log.info("libminmax.so not loaded; using the numpy decimation path")
        return None
    lib.minmax_decimate.restype = None
    lib.minmax_decimate.argtypes = [_INT32_ARRAY, ctypes.c_int64, ctypes.c_int64, _INT32_ARRAY]
    log.info("using the C decimation path")
    return lib


_LIB = _load()


@dataclass(frozen=True, slots=True)
class MinMaxEnvelope:
    """``values`` is ``int32``, length ``2 * columns``, interleaved [min, max].

    Interleaved because that is the layout the wire protocol sends and the frontend
    reads back as a zero-copy typed-array view; keeping it in that order here avoids a
    reshuffle on the hot path.
    """

    columns: int
    values: np.ndarray

    @property
    def mins(self) -> np.ndarray:
        """Strided view, not a copy."""
        return self.values[0::2]

    @property
    def maxs(self) -> np.ndarray:
        """Strided view, not a copy."""
        return self.values[1::2]


class MinMaxDecimator:
    """Reduces a sample buffer to at most ``target_columns`` min/max pairs."""

    __slots__ = ("_target_columns",)

    def __init__(self, target_columns: int = DEFAULT_TARGET_COLUMNS) -> None:
        if target_columns <= 0:
            raise ValueError("target_columns must be positive")
        self._target_columns = target_columns

    @property
    def target_columns(self) -> int:
        return self._target_columns

    def decimate(self, samples: np.ndarray) -> MinMaxEnvelope:
        n = samples.shape[0]
        if n == 0:
            return MinMaxEnvelope(columns=0, values=np.empty(0, dtype=np.int32))

        # Fewer samples than columns: one column per sample. Never invent columns by
        # interpolating -- the plot would show detail the instrument never measured.
        columns = min(self._target_columns, n)

        if _LIB is not None:
            values = np.empty(columns * 2, dtype=np.int32)
            _LIB.minmax_decimate(np.ascontiguousarray(samples, dtype=np.int32), n, columns, values)
            return MinMaxEnvelope(columns=columns, values=values)

        if n % columns == 0:
            # Fast path, and the only one that runs in practice (20 000 / 1 000).
            block = samples.reshape(columns, n // columns)
            mins = block.min(axis=1)
            maxs = block.max(axis=1)
        else:
            # Ragged path, for short/malformed frames. Bucket edges spread the
            # remainder evenly instead of dumping it all into the last column.
            edges = (np.arange(columns, dtype=np.int64) * n) // columns
            mins = np.minimum.reduceat(samples, edges)
            maxs = np.maximum.reduceat(samples, edges)

        values = np.empty(columns * 2, dtype=np.int32)
        values[0::2] = mins
        values[1::2] = maxs
        return MinMaxEnvelope(columns=columns, values=values)
