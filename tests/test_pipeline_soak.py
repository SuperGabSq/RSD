"""End-to-end pipeline tests against the real simulator over a real socket.

Everything else in the suite substitutes something. This file substitutes nothing: a
real ``mock_uc_server.py`` subprocess streaming real 80 kB frames at 100 Hz into the
real upstream adapter, the real acquisition thread, the real publisher thread. It is
the only place the two clocks, the two threads and the socket all run at once, which
makes it the only place certain bugs can appear at all:

* a lock held across a socket write, which shows up as a falling frame rate;
* an unbounded queue, which shows up as RSS climbing and nothing else;
* an off-by-one in frame numbering, which shows up as a gap.

The short test runs by default. The five-minute soak is marked ``slow`` and deselected
by default -- a suite you will not run because it takes five minutes is a suite that
stops catching things. Run it with ``pytest -m slow``.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

import pytest

from backend.application.ports import ConnectionState, WaveformDomain
from backend.application.publisher import ThrottledPublisher
from backend.application.session import AcquisitionSession
from backend.domain.decimation import MinMaxDecimator
from backend.domain.hashing import Xxh3_128Hasher
from backend.domain.rate import SampleRateEstimator
from backend.domain.spectrum import SpectrumAnalyzer
from backend.domain.validation import FrameValidator
from backend.infrastructure.upstream_ws import make_source_factory
from backend.infrastructure.wire import HEADER_BYTES, WireCodec, decode_header

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIMULATOR = os.path.join(REPO_ROOT, "backend", "mock", "mock_uc_server.py")

DEFAULT_SHORT_SECONDS = 8.0
DEFAULT_SOAK_SECONDS = float(os.environ.get("SOAK_SECONDS", "300"))


class CountingSink:
    """Counts and checks, but never accumulates.

    A sink that stored every message would itself grow without bound and make the
    flat-memory assertion meaningless -- the test would be measuring its own leak.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reports = 0
        self.waveforms = 0
        self.dropped = 0
        self.gaps: list[tuple[int, int]] = []
        self.invalid = 0
        self.states: list[str] = []
        self.last_rate_avg: float | None = None
        self.waveform_points: set[int] = set()
        self._expected_next = 1

    def send_text(self, payload: str) -> None:
        message = json.loads(payload)
        kind = message["type"]
        with self._lock:
            if kind == "status":
                self.states.append(message["state"])
                return
            if kind != "frames":
                return
            self.dropped += message.get("dropped", 0)
            if "rateAvg" in message:
                self.last_rate_avg = message["rateAvg"]
            for item in message["items"]:
                number = item["n"]
                if number != self._expected_next:
                    self.gaps.append((self._expected_next, number))
                self._expected_next = number + 1
                self.reports += 1
                if not item["valid"]:
                    self.invalid += 1

    def send_binary(self, payload: bytes) -> None:
        header = decode_header(payload[:HEADER_BYTES])
        with self._lock:
            self.waveforms += 1
            self.waveform_points.add(header.point_count)


@dataclass
class Simulator:
    process: subprocess.Popen
    port: int

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover
            self.process.kill()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _wait_for_port(port: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError(f"simulator never came up on port {port}")


@pytest.fixture
def simulator():
    started: list[Simulator] = []

    def start(*extra_args: str) -> Simulator:
        port = _free_port()
        process = subprocess.Popen(
            [sys.executable, SIMULATOR, "--host", "127.0.0.1", "--port", str(port), *extra_args],
            cwd=REPO_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _wait_for_port(port)
        sim = Simulator(process, port)
        started.append(sim)
        return sim

    yield start
    for sim in started:
        sim.stop()


def build_pipeline(sink: CountingSink) -> tuple[AcquisitionSession, ThrottledPublisher]:
    publisher = ThrottledPublisher(
        sink,
        WireCodec(),
        decimator=MinMaxDecimator(1_000),
        analyzer=SpectrumAnalyzer(),
        publish_hz=30.0,
    )
    session = AcquisitionSession(
        source_factory=make_source_factory(5.0),
        publisher=publisher,
        hasher=Xxh3_128Hasher(),
        validator=FrameValidator(20_000),
        rate_estimator=SampleRateEstimator(),
    )
    return session, publisher


def rss_bytes() -> int:
    """Resident set size from /proc. No psutil dependency for one number."""
    with open("/proc/self/status", encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    raise AssertionError("VmRSS not found")  # pragma: no cover


def run_pipeline(sim, duration_s: float, *, domain=WaveformDomain.TIME, sample_rss=False):
    sink = CountingSink()
    session, publisher = build_pipeline(sink)
    publisher.set_domain(domain)
    publisher.start()
    session.start(sim.url)

    deadline = time.monotonic() + duration_s
    samples: list[int] = []
    settled_at = time.monotonic() + min(5.0, duration_s / 4)
    while time.monotonic() < deadline:
        time.sleep(0.5)
        if sample_rss and time.monotonic() > settled_at:
            samples.append(rss_bytes())

    session.stop()
    publisher.stop()
    return sink, session, publisher, samples


@pytest.mark.skipif(not os.path.exists("/proc/self/status"), reason="needs Linux /proc")
def test_pipeline_streams_the_real_simulator_without_losing_a_frame(simulator):
    """Frames received == frames reported, no gaps, at a real 100 Hz over a real
    socket. This is the Phase 3 guarantee in a single assertion."""
    sim = simulator()
    sink, session, publisher, _ = run_pipeline(sim, DEFAULT_SHORT_SECONDS)

    assert session.frames_received > DEFAULT_SHORT_SECONDS * 90  # ~100 Hz, allow startup
    assert sink.reports == session.frames_received, "a frame was received but never reported"
    assert sink.gaps == [], f"frame numbering gaps: {sink.gaps[:5]}"
    assert sink.dropped == 0
    assert sink.states[:3] == ["connecting", "connected", "idle"] or sink.states[:2] == [
        "connecting",
        "connected",
    ]


@pytest.mark.skipif(not os.path.exists("/proc/self/status"), reason="needs Linux /proc")
def test_presentation_is_throttled_while_acquisition_is_not(simulator):
    """The central architectural claim, measured end to end: ~100 frames/s in,
    ~30 waveforms/s out, and every one of the 100 still logged."""
    sim = simulator()
    sink, session, publisher, _ = run_pipeline(sim, DEFAULT_SHORT_SECONDS)

    acquisition_hz = session.frames_received / DEFAULT_SHORT_SECONDS
    presentation_hz = sink.waveforms / DEFAULT_SHORT_SECONDS

    assert 90 < acquisition_hz < 110, f"acquisition ran at {acquisition_hz:.1f} Hz"
    assert 20 < presentation_hz < 35, f"presentation ran at {presentation_hz:.1f} Hz"
    assert sink.reports == session.frames_received  # complete despite the throttling
    assert publisher.stats().superseded_waveforms > 0  # stale frames really were dropped
    assert sink.waveform_points == {1_000}


@pytest.mark.skipif(not os.path.exists("/proc/self/status"), reason="needs Linux /proc")
def test_estimated_rate_tracks_the_simulators_actual_rate(simulator):
    sim = simulator()
    sink, _, _, _ = run_pipeline(sim, DEFAULT_SHORT_SECONDS)
    assert sink.last_rate_avg == pytest.approx(2_000_000, rel=0.05)


@pytest.mark.skipif(not os.path.exists("/proc/self/status"), reason="needs Linux /proc")
def test_injected_bad_frames_are_reported_without_disturbing_the_stream(simulator):
    """--bad-frame-every, end to end: the red-line requirement, proven against the
    real fault injector rather than a hand-made short payload."""
    sim = simulator("--bad-frame-every", "20")
    sink, session, _, _ = run_pipeline(sim, DEFAULT_SHORT_SECONDS)

    assert sink.invalid > 0
    assert sink.reports == session.frames_received
    assert sink.gaps == []


@pytest.mark.skipif(not os.path.exists("/proc/self/status"), reason="needs Linux /proc")
def test_a_simulated_drop_reaches_the_client_as_disconnected(simulator):
    """--drop-after, end to end: the connection-dropped popup requirement."""
    sim = simulator("--drop-after", "150")
    sink = CountingSink()
    session, publisher = build_pipeline(sink)
    publisher.start()
    session.start(sim.url)

    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline and session.state is not ConnectionState.DISCONNECTED:
        time.sleep(0.05)
    publisher.stop()
    session.stop()

    assert session.state is ConnectionState.DISCONNECTED
    assert "disconnected" in sink.states
    assert sink.reports == session.frames_received == 150
    assert sink.gaps == []


@pytest.mark.slow
@pytest.mark.skipif(not os.path.exists("/proc/self/status"), reason="needs Linux /proc")
def test_five_minute_soak_holds_flat_memory_and_loses_nothing(simulator):
    """The test that catches an unbounded queue.

    A leak of one 80 kB frame per second is invisible for the length of a demo and
    fatal by the end of an afternoon, and it does not fail any other assertion in this
    suite -- the frame rate stays right up until the moment the process dies. So memory
    is asserted, not eyeballed.

    Growth is measured after a settling period, because the first few seconds legitimately
    allocate numpy scratch buffers and the interpreter's arenas.
    """
    duration = DEFAULT_SOAK_SECONDS
    sim = simulator()
    sink, session, publisher, rss = run_pipeline(sim, duration, sample_rss=True)

    assert sink.reports == session.frames_received
    assert sink.gaps == [], f"frame numbering gaps: {sink.gaps[:5]}"
    assert sink.dropped == 0
    assert session.frames_received > duration * 95

    # The leak this catches is one that *retains frames*. At 8 MB/s that is 480 MB per
    # minute, so the interesting signal is not "did RSS move" -- it will, by a few
    # hundred kB of interpreter arenas during warm-up -- but "does it keep moving in
    # proportion to throughput". Hence a plateau check rather than a byte budget.
    warm_up = len(rss) // 4
    plateau = rss[warm_up:]
    assert len(plateau) >= 4, "not enough samples to judge the plateau"

    growth = max(plateau) - min(plateau)
    assert growth < 16 * 1024 * 1024, (
        f"RSS moved {growth / 1e6:.1f} MB after warm-up over "
        f"{session.frames_received} frames -- something is accumulating"
    )

    # ...and the movement must not be a sustained slope: the second half of the run
    # may not grow more than the first, beyond a megabyte of allocator noise.
    midpoint = len(plateau) // 2
    first_half = plateau[midpoint] - plateau[0]
    second_half = plateau[-1] - plateau[midpoint]
    assert second_half <= first_half + 1024 * 1024, (
        f"RSS grew {first_half / 1e6:.2f} MB then {second_half / 1e6:.2f} MB -- "
        "growth is not levelling off"
    )
