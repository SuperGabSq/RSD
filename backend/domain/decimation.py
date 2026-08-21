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

from dataclasses import dataclass

import numpy as np

DEFAULT_TARGET_COLUMNS = 1_000


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
