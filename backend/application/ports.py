"""Ports: the seams between orchestration and the outside world.

The application layer talks to two things it must not know the implementation of --
the microcontroller upstream and the browser downstream. Both are expressed as
Protocols here, which is what lets the whole pipeline be exercised in tests with
in-memory fakes: no sockets, no event loop, no Flask test client, no ports to bind.

Upstream failures arrive as library-specific exceptions from three different layers
(DNS, TCP, the WebSocket handshake, the WebSocket close handshake). The adapter's job
is to collapse that zoo into the two cases the application actually distinguishes,
because those are the two the *brief* distinguishes:

* :class:`UpstreamConnectionError` -- we never got a stream. The UI shows the
  "connection could not be established" popup.
* :class:`UpstreamClosed` -- we had a stream and lost it. The UI shows the "connection
  dropped" popup and re-enables Connect once it is dismissed.

Anything else escaping the adapter is a bug in the adapter, not a condition the session
is expected to reason about.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from enum import StrEnum
from typing import Protocol

import numpy as np

from backend.domain.decimation import MinMaxEnvelope
from backend.domain.frame import FrameReport
from backend.domain.spectrum import Spectrum


class ConnectionState(StrEnum):
    """One enum, not a scatter of is_connected / is_connecting booleans that can
    disagree with each other. A ``StrEnum`` so it serialises to the wire as its own name,
    with no conversion table to drift out of sync with the frontend."""

    IDLE = "idle"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"
    DISCONNECTED = "disconnected"


class WaveformDomain(StrEnum):
    """Which waveform the browser currently wants.

    ``NONE`` is not a placeholder: while no plot is visible there is nothing to compute,
    and computing an unwatched FFT thirty times a second is pure waste.
    """

    NONE = "none"
    TIME = "td"
    FREQUENCY = "fd"


class UpstreamError(Exception):
    """Base for every failure the upstream adapter is allowed to raise."""


class UpstreamConnectionError(UpstreamError):
    """The stream was never established: bad URL, refused, unreachable, timed out."""


class UpstreamClosed(UpstreamError):
    """An established stream ended -- cleanly or otherwise."""


class UpstreamSource(Protocol):
    """A source of binary frames from the microcontroller."""

    def open(self) -> None:
        """Establish the stream. Raises :class:`UpstreamConnectionError` on failure."""
        ...

    def messages(self) -> Iterator[bytes]:
        """Yield binary payloads until the stream ends.

        Raises :class:`UpstreamClosed` when it does. Text messages are counted and
        skipped, never yielded (assumption #5): only binary messages are frames.
        """
        ...

    def close(self) -> None:
        """Idempotent. Safe to call from another thread to unblock ``messages()``."""
        ...


class DownstreamSink(Protocol):
    """The browser connection. Exactly one thread may call these -- see
    ``ThrottledPublisher``, which owns the sink and is the only caller."""

    def send_text(self, payload: str) -> None: ...

    def send_binary(self, payload: bytes) -> None: ...


class MessageCodec(Protocol):
    """Serialisation, injected rather than imported.

    The publisher decides *what* to send and *when*; it must not know that the answer
    is currently JSON and a little-endian struct. Without this seam the application
    layer would import ``infrastructure.wire`` and the dependency rule would run
    backwards -- which the architecture test in ``tests/`` would then catch, so this is
    the shape that keeps the test honest rather than the shape that dodges it.
    """

    def encode_status(self, state: ConnectionState, message: str = "") -> str: ...

    def encode_config(
        self,
        *,
        nominal_rate_hz: float,
        expected_samples: int,
        target_columns: int,
    ) -> str: ...

    def encode_frames(
        self,
        reports: Sequence[FrameReport],
        *,
        smoothed_rate_hz: float | None = None,
        dropped: int = 0,
        superseded: int = 0,
    ) -> str: ...

    def encode_spectrum_axis(self, frequencies_hz: np.ndarray) -> str: ...

    def encode_time_domain(
        self, frame_number: int, envelope: MinMaxEnvelope, flags: int = 0
    ) -> bytes: ...

    def encode_frequency_domain(
        self, frame_number: int, spectrum: Spectrum, flags: int = 0
    ) -> bytes: ...

    def waveform_flags(self, *, is_valid: bool, malformed: bool) -> int: ...
