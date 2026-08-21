"""Frame value objects.

Two immutable records cross every boundary in the system:

* :class:`RawFrame`   -- what came off the wire, plus the two clock readings taken at
  receipt time.
* :class:`FrameReport` -- what the user is shown: one log line's worth of facts.

Both are frozen. Frames are produced on the 100 Hz acquisition thread and consumed on
the ~30 Hz publisher thread; making them immutable means the hand-off needs no copying
and no defensive locking of the objects themselves. Under threads that stops being a
style preference and becomes the safety argument.

Two clocks, deliberately:

* ``received_at`` is wall-clock, and is *only* used to render the timestamp the brief
  specifies. Wall-clock time can jump (NTP correction, DST) and is unfit for deltas.
* ``monotonic_s`` is a steady clock, and is *only* used for the sample-rate estimate.
  A backwards NTP step must never produce a negative or absurd rate reading.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# Matches the brief's example log line exactly: [2024-01-01 12:00:00] Frame 1 | ...
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass(frozen=True, slots=True)
class RawFrame:
    """One WebSocket binary message, exactly as received, with its receipt clocks."""

    number: int
    payload: bytes
    received_at: datetime
    monotonic_s: float


@dataclass(frozen=True, slots=True)
class FrameReport:
    """The per-frame facts the frontend renders as one log line.

    ``estimated_rate_hz`` is ``None`` for the first frame of a session: it has no
    predecessor, so there is no interval to measure. Reporting a value derived from
    time-since-connect would be a fabrication, so we report nothing.
    """

    number: int
    timestamp: str
    sample_count: int
    hash: str
    is_valid: bool
    malformed: bool
    estimated_rate_hz: float | None

    @classmethod
    def build(
        cls,
        frame: RawFrame,
        *,
        hash_hex: str,
        sample_count: int,
        is_valid: bool,
        malformed: bool,
        estimated_rate_hz: float | None,
    ) -> FrameReport:
        """Assemble a report from a raw frame and the results of the pure analyses.

        Kept here rather than in the application layer so that timestamp formatting --
        the one piece of presentation the brief pins down exactly -- lives in a single
        unit-testable place.
        """
        return cls(
            number=frame.number,
            timestamp=frame.received_at.strftime(TIMESTAMP_FORMAT),
            sample_count=sample_count,
            hash=hash_hex,
            is_valid=is_valid,
            malformed=malformed,
            estimated_rate_hz=estimated_rate_hz,
        )
