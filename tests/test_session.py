"""AcquisitionSession tests.

Every one of these runs the real session, the real threads and the real state machine
against an in-memory upstream. No sockets are bound and nothing sleeps waiting for a
network, which is the payoff of putting ``UpstreamSource`` behind a protocol.
"""

from __future__ import annotations

import threading
from datetime import datetime

import pytest

from backend.application.ports import ConnectionState, UpstreamConnectionError, WaveformDomain
from backend.application.publisher import ThrottledPublisher
from backend.application.session import AcquisitionSession
from backend.domain.decimation import MinMaxDecimator
from backend.domain.hashing import Xxh3_128Hasher
from backend.domain.rate import SampleRateEstimator
from backend.domain.spectrum import SpectrumAnalyzer
from backend.domain.validation import FrameValidator
from backend.infrastructure.wire import WireCodec
from tests.fakes import FakeUpstream, RecordingSink, frame_bytes

EXPECTED = 20_000


class Clock:
    """A monotonic clock that advances exactly one frame period per read, so rate
    assertions are exact and nothing has to sleep."""

    def __init__(self, step_s: float = 0.01) -> None:
        self.now = 1_000.0
        self.step = step_s

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def build(upstream: FakeUpstream, *, sink=None, clock=None):
    sink = sink or RecordingSink()
    publisher = ThrottledPublisher(
        sink,
        WireCodec(),
        decimator=MinMaxDecimator(1_000),
        analyzer=SpectrumAnalyzer(),
        publish_hz=1_000.0,  # tests drain by hand; a fast interval keeps stop() prompt
    )
    session = AcquisitionSession(
        source_factory=lambda url: upstream,
        publisher=publisher,
        hasher=Xxh3_128Hasher(),
        validator=FrameValidator(EXPECTED),
        rate_estimator=SampleRateEstimator(),
        wall_clock=lambda: datetime(2024, 1, 1, 12, 0, 0),
        monotonic=clock or Clock(),
    )
    return session, publisher, sink


def run_to_completion(session: AcquisitionSession, url: str = "ws://uc:8765", timeout: float = 5.0):
    session.start(url)
    thread = session._thread  # noqa: SLF001 - tests may look inside
    assert thread is not None
    thread.join(timeout=timeout)
    assert not thread.is_alive(), "acquisition thread did not finish"


# ---------------------------------------------------------------------- happy path


def test_every_received_frame_is_reported_exactly_once_and_in_order():
    """The core Phase 3 guarantee: frames received == frames reported, no gaps."""
    upstream = FakeUpstream([frame_bytes() for _ in range(250)])
    session, publisher, sink = build(upstream)

    run_to_completion(session)
    publisher.tick()

    assert session.frames_received == 250
    assert [item["n"] for item in sink.frame_items()] == list(range(1, 251))


def test_frame_numbering_starts_at_one_and_restarts_on_reconnect():
    """Assumption #6: numbering is ours, monotonic per session; a reconnect is a new
    session, not a continuation of the old one."""
    session, publisher, sink = build(FakeUpstream([frame_bytes() for _ in range(3)]))
    run_to_completion(session)
    publisher.tick()
    assert [item["n"] for item in sink.frame_items()] == [1, 2, 3]

    session._source_factory = lambda url: FakeUpstream([frame_bytes()])  # noqa: SLF001
    run_to_completion(session)
    publisher.tick()
    assert [item["n"] for item in sink.frame_items()] == [1, 2, 3, 1]


def test_reports_carry_the_hash_timestamp_and_rate():
    payload = frame_bytes()
    session, publisher, sink = build(FakeUpstream([payload, payload]))

    run_to_completion(session)
    publisher.tick()

    items = sink.frame_items()
    assert items[0]["hash"] == Xxh3_128Hasher().hash(payload)
    assert items[0]["ts"] == "2024-01-01 12:00:00"
    assert items[0]["rate"] is None  # first frame of the session has no interval
    assert items[1]["rate"] == pytest.approx(2_000_000.0)


def test_a_short_frame_is_reported_invalid_without_interrupting_the_stream():
    """The --bad-frame-every case: one bad frame is a log line, not an incident."""
    frames = [frame_bytes(), frame_bytes(19_995), frame_bytes()]
    session, publisher, sink = build(FakeUpstream(frames))

    run_to_completion(session)
    publisher.tick()

    items = sink.frame_items()
    assert [item["valid"] for item in items] == [True, False, True]
    assert items[1]["samples"] == 19_995
    assert len(items[1]["hash"]) == 32  # still hashed, so still diagnosable


def test_a_malformed_frame_is_flagged_and_its_tail_is_not_fed_to_the_plot():
    session, publisher, sink = build(FakeUpstream([frame_bytes(100) + b"\x01"]))
    publisher.set_domain(WaveformDomain.TIME)

    run_to_completion(session)
    publisher.tick()

    assert sink.frame_items()[0]["malformed"] is True
    # 401 bytes -> 100 whole samples; the stray byte is not turned into a 101st.
    assert sink.waveform_headers()[0].point_count == 100


def test_waveforms_are_offered_only_while_a_plot_is_visible():
    session, publisher, sink = build(FakeUpstream([frame_bytes() for _ in range(5)]))
    run_to_completion(session)
    publisher.tick()
    assert sink.binaries == []

    publisher.set_domain(WaveformDomain.TIME)
    session._source_factory = lambda url: FakeUpstream([frame_bytes()])  # noqa: SLF001
    run_to_completion(session)
    publisher.tick()
    assert len(sink.binaries) == 1


# ------------------------------------------------------------------- state machine


def test_a_healthy_session_walks_connecting_then_connected():
    session, publisher, sink = build(FakeUpstream([frame_bytes()]))
    run_to_completion(session)
    publisher.tick()
    assert sink.states()[:2] == ["connecting", "connected"]


def test_a_refused_connection_ends_in_error_with_a_readable_message():
    """Drives the 'connection could not be established' popup."""
    upstream = FakeUpstream([], fail_to_open="could not reach ws://nope:1: refused")
    session, publisher, sink = build(upstream)

    run_to_completion(session)
    publisher.tick()

    assert session.state is ConnectionState.ERROR
    assert sink.states() == ["connecting", "error"]
    assert "refused" in sink.messages("status")[-1]["message"]
    assert upstream.opened is False


def test_a_mid_stream_drop_ends_in_disconnected_not_error():
    """Drives the 'connection dropped' popup, which is a different dialog from the
    'could not connect' one, because the user's next action is different."""
    session, publisher, sink = build(FakeUpstream([frame_bytes() for _ in range(3)]))

    run_to_completion(session)
    publisher.tick()

    assert session.state is ConnectionState.DISCONNECTED
    assert sink.states() == ["connecting", "connected", "disconnected"]
    assert [item["n"] for item in sink.frame_items()] == [1, 2, 3]  # nothing lost


def test_a_user_initiated_disconnect_ends_idle_so_no_popup_fires():
    """IDLE and DISCONNECTED are deliberately distinct. Collapsing them would pop a
    'connection lost' dialog in the face of someone who just clicked Disconnect."""
    upstream = FakeUpstream([frame_bytes() for _ in range(2)], ending="block")
    session, publisher, sink = build(upstream)

    session.start("ws://uc:8765")
    _wait_until(lambda: session.state is ConnectionState.CONNECTED)
    session.stop()
    publisher.tick()

    assert session.state is ConnectionState.IDLE
    assert "disconnected" not in sink.states()
    assert sink.states()[-1] == "idle"


def test_an_adapter_bug_becomes_a_status_message_not_a_traceback():
    """If the upstream adapter ever raises something it was supposed to translate, the
    client gets a sentence. A stack trace must never reach the browser."""
    session, publisher, sink = build(FakeUpstream([frame_bytes()], ending="raise"))

    run_to_completion(session)
    publisher.tick()

    assert session.state is ConnectionState.ERROR
    assert "adapter bug" in sink.messages("status")[-1]["message"]


def test_a_source_factory_that_explodes_is_reported_as_a_connection_error():
    session, publisher, sink = build(FakeUpstream([]))

    def exploding_factory(url: str):
        raise UpstreamConnectionError(f"not a valid WebSocket URL: {url}")

    session._source_factory = exploding_factory  # noqa: SLF001
    run_to_completion(session, url="httpx://bad")
    publisher.tick()

    assert session.state is ConnectionState.ERROR
    assert "not a valid WebSocket URL" in sink.messages("status")[-1]["message"]


# ----------------------------------------------------------------- thread hygiene


def test_start_is_idempotent_so_a_double_click_does_not_open_two_streams():
    upstream = FakeUpstream([frame_bytes()], ending="block")
    session, publisher, _ = build(upstream)

    session.start("ws://uc:8765")
    _wait_until(lambda: session.state is ConnectionState.CONNECTED)
    before = threading.active_count()
    session.start("ws://uc:8765")
    assert threading.active_count() == before

    session.stop()
    publisher.stop()


def test_stop_closes_the_upstream_so_a_blocked_recv_is_unblocked():
    """The acquisition thread parks in a blocking recv(); joining first would just
    wait out the timeout every single time. Closing first is what makes stop() fast."""
    upstream = FakeUpstream([frame_bytes()], ending="block")
    session, publisher, _ = build(upstream)

    session.start("ws://uc:8765")
    _wait_until(lambda: session.state is ConnectionState.CONNECTED)
    session.stop(timeout_s=2.0)

    assert upstream.closed is True
    assert session.is_running is False
    publisher.stop()


def test_stop_is_idempotent_and_safe_before_start():
    session, publisher, _ = build(FakeUpstream([]))
    session.stop()
    session.stop()
    publisher.stop()


def test_ten_connect_disconnect_cycles_leak_no_threads():
    """The reconnect-leak test. A thread leaked per cycle is invisible for the first
    few minutes of a demo and fatal by the end of an afternoon."""
    sink = RecordingSink()
    session, publisher, _ = build(FakeUpstream([], ending="block"), sink=sink)
    publisher.start()
    baseline = threading.active_count()

    for cycle in range(10):
        session._source_factory = lambda url, c=cycle: FakeUpstream(  # noqa: SLF001
            [frame_bytes(1_000) for _ in range(3)], ending="block"
        )
        session.start("ws://uc:8765")
        _wait_until(lambda: session.state is ConnectionState.CONNECTED)
        session.stop()
        assert threading.active_count() <= baseline, f"thread leaked on cycle {cycle}"

    publisher.stop()
    assert threading.active_count() <= baseline - 1  # publisher's thread is gone too


def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = threading.Event()
    waited = 0.0
    while waited < timeout:
        if predicate():
            return
        deadline.wait(0.01)
        waited += 0.01
    raise AssertionError("condition not reached within timeout")
