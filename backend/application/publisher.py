"""The throttling boundary: where a 100 Hz obligation meets a 30 Hz display.

This is the class the whole design turns on, so it is worth stating the rule it
enforces in one line:

    **Acquisition is complete and runs at 100 Hz. Presentation is lossy and runs at
    ~30 Hz. The two have different loss semantics and must never be confused.**

Concretely:

* **Frame reports are complete.** Every frame gets a log line. They are batched, not
  sampled -- a tick sends three or four lines instead of one, which is a rendering
  optimisation, not a reduction in what is reported.
* **Waveforms are latest-wins.** The slot holds one frame. If a second arrives before
  the first is drawn, the first is discarded. That is *correct*: nobody wants to look
  at a trace from 400 ms ago, and drawing it would cost the same as drawing the current
  one while being wrong.

Confusing those two is the most likely way to get this problem wrong, in either
direction: dropping log lines loses the deliverable, and queueing waveforms builds an
unbounded backlog that grows latency until it exhausts memory.

**Thread safety, auditable by reading this file.** All cross-thread state is the four
fields guarded by ``self._lock``. Producers (the acquisition thread) do nothing under
the lock but append or replace. Everything expensive -- decimation, FFT, serialisation,
socket writes -- happens on the publisher thread *outside* the lock, so a stalled
browser can never apply backpressure to acquisition.

**Why decimation happens here rather than on the acquisition thread.** Decimation is a
presentation concern: it exists to turn 20 000 samples into ~1 000 screen columns. Only
the frames that are actually drawn need it, which is roughly 30 of every 100. Doing it
here rather than in the 100 Hz path cuts that work by ~70 % and puts it in the same
place as the FFT, which was always going to be gated this way. The acquisition thread
is left with only what the brief makes mandatory: hash, validate, estimate rate.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass

import numpy as np

from backend.application.ports import (
    ConnectionState,
    DownstreamSink,
    MessageCodec,
    WaveformDomain,
)
from backend.domain.decimation import MinMaxDecimator
from backend.domain.frame import FrameReport
from backend.domain.spectrum import SpectrumAnalyzer

log = logging.getLogger(__name__)

DEFAULT_PUBLISH_HZ = 30.0
# Assumption #15: the in-memory log is bounded. At 100 lines/s an unbounded queue
# exhausts memory in minutes if the browser ever stops reading.
DEFAULT_MAX_PENDING_REPORTS = 5_000


@dataclass(frozen=True, slots=True)
class _WaveformJob:
    """Raw samples parked for the publisher to render. Frozen, and the array is never
    mutated after construction, so no copy is needed to hand it across the threads."""

    frame_number: int
    samples: np.ndarray
    is_valid: bool
    malformed: bool


@dataclass(frozen=True, slots=True)
class PublisherStats:
    published_reports: int
    published_waveforms: int
    dropped_reports: int
    superseded_waveforms: int
    ticks: int


class ThrottledPublisher:
    """Owns the downstream sink. Nothing else may write to it."""

    def __init__(
        self,
        sink: DownstreamSink,
        codec: MessageCodec,
        *,
        decimator: MinMaxDecimator,
        analyzer: SpectrumAnalyzer,
        publish_hz: float = DEFAULT_PUBLISH_HZ,
        max_pending_reports: int = DEFAULT_MAX_PENDING_REPORTS,
        monotonic=time.monotonic,
    ) -> None:
        if publish_hz <= 0:
            raise ValueError("publish_hz must be positive")
        self._sink = sink
        self._codec = codec
        self._decimator = decimator
        self._analyzer = analyzer
        self._interval_s = 1.0 / publish_hz
        self._monotonic = monotonic

        self._lock = threading.Lock()
        # --- everything below is guarded by _lock ---
        self._pending_status: deque[tuple[ConnectionState, str]] = deque()
        self._pending_reports: deque[FrameReport] = deque(maxlen=max_pending_reports)
        self._waveform: _WaveformJob | None = None
        self._domain = WaveformDomain.NONE
        self._smoothed_rate_hz: float | None = None
        self._dropped_reports = 0
        self._superseded_waveforms = 0
        self._axis_signature: tuple | None = None
        # --- end guarded state ---

        self._published_reports = 0
        self._published_waveforms = 0
        self._ticks = 0

        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._send_failed = threading.Event()

    # ------------------------------------------------------------------ producers
    # Called from the acquisition thread. These must stay cheap: they hold the lock.

    def publish_status(self, state: ConnectionState, message: str = "") -> None:
        with self._lock:
            self._pending_status.append((state, message))

    def submit_report(self, report: FrameReport, smoothed_rate_hz: float | None = None) -> None:
        with self._lock:
            if (
                self._pending_reports.maxlen is not None
                and len(self._pending_reports) == self._pending_reports.maxlen
            ):
                # The bound has been reached, which means the browser stopped reading
                # minutes ago. Count what we lose and tell the operator, rather than
                # dropping log lines silently or growing until the process dies.
                self._dropped_reports += 1
            self._pending_reports.append(report)
            if smoothed_rate_hz is not None:
                self._smoothed_rate_hz = smoothed_rate_hz

    def offer_waveform(
        self,
        frame_number: int,
        samples: np.ndarray,
        *,
        is_valid: bool = True,
        malformed: bool = False,
    ) -> None:
        """Latest-wins. Replacing an undrawn frame is the intended behaviour."""
        with self._lock:
            if self._domain is WaveformDomain.NONE:
                return  # nothing is watching; do not even park it
            if self._waveform is not None:
                self._superseded_waveforms += 1
            self._waveform = _WaveformJob(frame_number, samples, is_valid, malformed)

    # ------------------------------------------------------------------- control

    def set_domain(self, domain: WaveformDomain) -> None:
        with self._lock:
            self._domain = domain
            # Force the frequency axis to be re-sent: the client may have just opened
            # the tab for the first time, or reloaded and forgotten it.
            self._axis_signature = None
            if domain is WaveformDomain.NONE:
                self._waveform = None

    @property
    def domain(self) -> WaveformDomain:
        with self._lock:
            return self._domain

    def stats(self) -> PublisherStats:
        with self._lock:
            dropped = self._dropped_reports
            superseded = self._superseded_waveforms
        return PublisherStats(
            published_reports=self._published_reports,
            published_waveforms=self._published_waveforms,
            dropped_reports=dropped,
            superseded_waveforms=superseded,
            ticks=self._ticks,
        )

    @property
    def pending_report_count(self) -> int:
        with self._lock:
            return len(self._pending_reports)

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Idempotent."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, name="publisher", daemon=True)
        self._thread.start()

    def stop(self, timeout_s: float = 2.0, *, flush: bool = True) -> None:
        """Idempotent, and joins with a timeout so a wedged socket cannot hang teardown.

        Daemon threads mean a stuck send can never keep the process alive, but we still
        join: leaking a thread per connect/disconnect cycle would be a real defect, and
        there is a test that counts them.
        """
        self._stopping.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout_s)
            if thread.is_alive():
                log.warning("publisher thread did not stop within %.1fs", timeout_s)
        if flush and not self._send_failed.is_set():
            # Last flush so a final "disconnected" status still reaches the browser --
            # that status is what re-enables the Connect button.
            self.tick()

    @property
    def send_failed(self) -> bool:
        """True once a downstream write raised: the browser is gone."""
        return self._send_failed.is_set()

    def _run(self) -> None:
        next_at = self._monotonic()
        while not self._stopping.is_set():
            next_at += self._interval_s
            self.tick()
            if self._send_failed.is_set():
                return
            delay = next_at - self._monotonic()
            if delay <= 0.0:
                # We fell behind. Re-anchor instead of trying to catch up, which would
                # spin the loop at full speed and make the stall worse.
                next_at = self._monotonic()
                continue
            if self._stopping.wait(delay):
                return

    # ----------------------------------------------------------------------- tick

    def tick(self) -> None:
        """One publish pass. Called by the loop, and directly by tests with a fake
        clock -- which is why the loop is three lines and the work is all here."""
        self._ticks += 1

        with self._lock:
            statuses = list(self._pending_status)
            self._pending_status.clear()
            reports = list(self._pending_reports)
            self._pending_reports.clear()
            dropped, self._dropped_reports = self._dropped_reports, 0
            waveform, self._waveform = self._waveform, None
            domain = self._domain
            smoothed = self._smoothed_rate_hz
            axis_signature = self._axis_signature

        # Everything from here on is outside the lock: a slow socket delays the next
        # tick, never the acquisition thread.
        try:
            if reports:
                self._sink.send_text(
                    self._codec.encode_frames(
                        reports, smoothed_rate_hz=smoothed, dropped=dropped
                    )
                )
                self._published_reports += len(reports)
            # Reports before status, so the log is chronologically consistent with the
            # state change that follows it -- a "disconnected" popup must not appear
            # above the last frames that arrived before the drop.
            for state, message in statuses:
                self._sink.send_text(self._codec.encode_status(state, message))
            if waveform is not None:
                self._send_waveform(waveform, domain, axis_signature)
        except Exception:
            # The browser went away mid-write. This is routine, not exceptional: the
            # user closed the tab. Record it so the route can tear the session down,
            # and never let it surface as a traceback.
            log.info("downstream send failed; treating the client as gone", exc_info=True)
            self._send_failed.set()
            self._stopping.set()

    def _send_waveform(
        self, job: _WaveformJob, domain: WaveformDomain, axis_signature: tuple | None
    ) -> None:
        flags = self._codec.waveform_flags(is_valid=job.is_valid, malformed=job.malformed)
        if domain is WaveformDomain.TIME:
            envelope = self._decimator.decimate(job.samples)
            self._sink.send_binary(
                self._codec.encode_time_domain(job.frame_number, envelope, flags)
            )
        elif domain is WaveformDomain.FREQUENCY:
            spectrum = self._analyzer.analyze(job.samples)
            signature = (
                spectrum.magnitudes_db.size,
                float(spectrum.bin_width_hz),
            )
            if signature != axis_signature:
                # Send the exact x-axis once per geometry, not per frame: 1 000 floats
                # of JSON at 30 Hz would cost more than every waveform combined.
                self._sink.send_text(self._codec.encode_spectrum_axis(spectrum.frequencies_hz))
                with self._lock:
                    self._axis_signature = signature
            self._sink.send_binary(
                self._codec.encode_frequency_domain(job.frame_number, spectrum, flags)
            )
        else:
            return
        self._published_waveforms += 1
