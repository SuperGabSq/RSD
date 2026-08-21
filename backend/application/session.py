"""Acquisition: one session, one thread, one explicit state machine.

The acquisition thread does only what the brief makes mandatory for *every* frame:
hash it, validate its length, estimate the rate, hand off a report. Nothing here draws,
serialises, or waits on the browser. Measured, that is roughly 30 us against a 10 ms
frame period -- so the 100 Hz obligation is met with about 300x of headroom, and the
two expensive pieces (``xxhash`` and the numpy view) release the GIL while they run, so
the publisher thread genuinely overlaps rather than merely interleaving.

Everything that could be slow or could block -- decimation, FFT, JSON, socket writes --
belongs to ``ThrottledPublisher`` on the other side of the throttling boundary.

**Why the state is an enum and not three booleans.** ``is_connected`` /
``is_connecting`` / ``has_error`` can disagree with each other, and the disagreement is
always discovered later, in the UI, as a Connect button that is enabled when it should
not be. One value cannot contradict itself.

The four terminal states are distinct on purpose, because the brief treats them
differently:

* ``ERROR``        -- never connected. Popup: "could not connect".
* ``DISCONNECTED`` -- was connected, lost it. Popup: "connection dropped", then Connect
  is re-enabled once the popup is dismissed.
* ``IDLE``         -- the *user* pressed Disconnect. No popup: they know.
* ``CONNECTED``    -- streaming.

Collapsing IDLE into DISCONNECTED would pop a "connection lost" dialog in the face of
someone who just clicked Disconnect.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime

import numpy as np

from backend.application.ports import (
    ConnectionState,
    UpstreamClosed,
    UpstreamConnectionError,
    UpstreamSource,
)
from backend.application.publisher import ThrottledPublisher
from backend.domain.frame import FrameReport, RawFrame
from backend.domain.hashing import FrameHasher
from backend.domain.rate import SampleRateEstimator
from backend.domain.validation import FrameValidator

log = logging.getLogger(__name__)

SourceFactory = Callable[[str], UpstreamSource]


class AcquisitionSession:
    """Owns the acquisition thread and the connection lifecycle for one browser."""

    def __init__(
        self,
        *,
        source_factory: SourceFactory,
        publisher: ThrottledPublisher,
        hasher: FrameHasher,
        validator: FrameValidator,
        rate_estimator: SampleRateEstimator,
        wall_clock: Callable[[], datetime] = datetime.now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._source_factory = source_factory
        self._publisher = publisher
        self._hasher = hasher
        self._validator = validator
        self._rate_estimator = rate_estimator
        self._wall_clock = wall_clock
        self._monotonic = monotonic

        self._lock = threading.Lock()
        self._state = ConnectionState.IDLE
        self._source: UpstreamSource | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()

        self.frames_received = 0  # written only by the acquisition thread

    @property
    def state(self) -> ConnectionState:
        with self._lock:
            return self._state

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    # ------------------------------------------------------------------ lifecycle

    def start(self, url: str) -> None:
        """Idempotent: starting an already-running session is a no-op, not an error.

        A double-click on Connect is a user event, not an exception.
        """
        if self.is_running:
            log.info("session already running; ignoring start(%s)", url)
            return
        self._stopping.clear()
        self.frames_received = 0
        self._rate_estimator.reset()  # a reconnect is a new session, not a continuation
        self._thread = threading.Thread(
            target=self._run, args=(url,), name="acquisition", daemon=True
        )
        self._thread.start()

    def stop(self, timeout_s: float = 2.0) -> None:
        """Idempotent, and safe to call from any thread.

        The acquisition thread is parked in a blocking ``recv()``; nothing short of
        closing the socket underneath it will wake it. So we close first, then join --
        joining first would simply wait out the timeout every time.
        """
        self._stopping.set()
        with self._lock:
            source = self._source
        if source is not None:
            try:
                source.close()
            except Exception:  # noqa: BLE001 - teardown must not raise
                log.debug("ignoring error while closing upstream", exc_info=True)

        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout_s)
            if thread.is_alive():
                log.warning("acquisition thread did not stop within %.1fs", timeout_s)

    # ----------------------------------------------------------------- the thread

    def _set_state(self, state: ConnectionState, message: str = "") -> None:
        with self._lock:
            self._state = state
        self._publisher.publish_status(state, message)

    def _run(self, url: str) -> None:
        self._set_state(ConnectionState.CONNECTING, f"connecting to {url}")

        try:
            source = self._source_factory(url)
        except Exception as exc:  # noqa: BLE001 - a bad URL must not be a traceback
            self._set_state(ConnectionState.ERROR, str(exc))
            return

        with self._lock:
            self._source = source

        try:
            source.open()
        except UpstreamConnectionError as exc:
            self._set_state(ConnectionState.ERROR, str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            # An adapter that raises something else is a bug in the adapter. Log it
            # with the traceback for us; send the client a sentence, never a stack.
            log.exception("unexpected error opening upstream")
            self._set_state(ConnectionState.ERROR, f"unexpected upstream error: {exc}")
            return
        finally:
            if self._stopping.is_set():
                self._safe_close(source)

        self._set_state(ConnectionState.CONNECTED, f"connected to {url}")

        try:
            for payload in source.messages():
                if self._stopping.is_set():
                    break
                self._process(payload)
        except UpstreamClosed as exc:
            if not self._stopping.is_set():
                self._set_state(ConnectionState.DISCONNECTED, str(exc))
                return
        except Exception as exc:  # noqa: BLE001
            log.exception("unexpected error while streaming")
            self._set_state(ConnectionState.ERROR, f"stream failed: {exc}")
            return
        finally:
            self._safe_close(source)

        # Fell out of the loop without an upstream error: the user pressed Disconnect.
        self._set_state(ConnectionState.IDLE, "disconnected")

    def _safe_close(self, source: UpstreamSource) -> None:
        try:
            source.close()
        except Exception:  # noqa: BLE001
            log.debug("ignoring error while closing upstream", exc_info=True)
        with self._lock:
            self._source = None

    def _process(self, payload: bytes) -> None:
        """The hot path. Everything in here runs 100 times a second, for ever."""
        self.frames_received += 1
        frame = RawFrame(
            number=self.frames_received,  # assumption #6: 1-based, per session
            payload=payload,
            received_at=self._wall_clock(),
            monotonic_s=self._monotonic(),
        )

        # Hash first, unconditionally, over the bytes exactly as received: a frame that
        # fails validation still needs a fingerprint or it cannot be diagnosed.
        digest = self._hasher.hash(payload)
        validation = self._validator.validate(payload)
        rate = self._rate_estimator.update(validation.sample_count, frame.monotonic_s)

        self._publisher.submit_report(
            FrameReport.build(
                frame,
                hash_hex=digest,
                sample_count=validation.sample_count,
                is_valid=validation.is_valid,
                malformed=validation.malformed,
                estimated_rate_hz=rate.instantaneous,
            ),
            rate.smoothed,
        )

        # Zero-copy, read-only view over the payload -- no allocation, and immutable,
        # so handing it to the publisher thread needs no defensive copy. `count` drops
        # a malformed tail rather than letting frombuffer raise on it.
        if validation.sample_count:
            samples = np.frombuffer(
                payload, dtype="<i4", count=validation.sample_count
            )
            self._publisher.offer_waveform(
                frame.number,
                samples,
                is_valid=validation.is_valid,
                malformed=validation.malformed,
            )
