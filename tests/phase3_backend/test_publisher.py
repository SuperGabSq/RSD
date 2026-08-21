"""ThrottledPublisher tests.

The publisher is where requirement #1 (every frame is logged) and requirement #2 (not
every frame is drawn) are reconciled, so these tests are mostly about proving the two
loss semantics are genuinely different: reports survive, waveforms are allowed not to.

``tick()`` is driven directly rather than by running the thread, so the throttling
behaviour is asserted with no sleeping and no timing tolerance. The thread lifecycle is
tested separately, at the bottom.
"""

from __future__ import annotations

import threading

import numpy as np
import pytest

from backend.application.ports import ConnectionState, WaveformDomain
from backend.application.publisher import ThrottledPublisher
from backend.domain.decimation import MinMaxDecimator
from backend.domain.spectrum import SpectrumAnalyzer
from backend.infrastructure.wire import (
    FLAG_INVALID,
    KIND_FREQUENCY_DOMAIN,
    KIND_TIME_DOMAIN,
    WireCodec,
)
from tests.support.fakes import RecordingSink
from tests.support.fakes import frame_report as report

FRAME_SAMPLES = 20_000


@pytest.fixture
def sink() -> RecordingSink:
    return RecordingSink()


@pytest.fixture
def publisher(sink) -> ThrottledPublisher:
    return ThrottledPublisher(
        sink,
        WireCodec(),
        decimator=MinMaxDecimator(1_000),
        analyzer=SpectrumAnalyzer(target_bins=1_000),
        publish_hz=30.0,
    )


def samples(seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.integers(-1000, 1000, size=FRAME_SAMPLES, dtype=np.int32)


# ------------------------------------------------- reports are complete, not sampled


def test_every_report_survives_batching(publisher, sink):
    """THE completeness assertion. 100 frames in, one tick, 100 log lines out --
    batched into fewer messages, but not one line fewer."""
    for n in range(1, 101):
        publisher.submit_report(report(n))

    publisher.tick()

    assert [item["n"] for item in sink.frame_items()] == list(range(1, 101))


def test_reports_batch_across_ticks_without_gaps_or_repeats(publisher, sink):
    """The realistic shape: ~3-4 frames arrive per 33 ms tick."""
    number = 0
    for _ in range(25):
        for _ in range(4):
            number += 1
            publisher.submit_report(report(number))
        publisher.tick()

    assert [item["n"] for item in sink.frame_items()] == list(range(1, 101))
    assert len(sink.messages("frames")) == 25  # 100 lines in 25 messages, not 100


def test_a_tick_with_nothing_pending_sends_nothing(publisher, sink):
    """An idle instrument must not generate traffic."""
    publisher.tick()
    publisher.tick()
    assert sink.texts == []
    assert sink.binaries == []


def test_queue_overrun_is_counted_and_reported_rather_than_hidden(sink):
    """If the browser stalls for minutes the bounded queue overruns. Losing log lines
    silently would be the worst possible failure in a deliverable whose point is a
    complete log, so the count travels with the next batch."""
    publisher = ThrottledPublisher(
        sink,
        WireCodec(),
        decimator=MinMaxDecimator(1_000),
        analyzer=SpectrumAnalyzer(),
        max_pending_reports=10,
    )
    for n in range(1, 16):  # 15 in, room for 10
        publisher.submit_report(report(n))

    publisher.tick()

    message = sink.messages("frames")[0]
    assert message["dropped"] == 5
    assert len(message["items"]) == 10
    # The queue keeps the *newest* lines: on a live instrument the recent past is what
    # the operator is looking at.
    assert [item["n"] for item in message["items"]] == list(range(6, 16))


def test_drop_count_resets_after_being_reported(sink):
    publisher = ThrottledPublisher(
        sink, WireCodec(), decimator=MinMaxDecimator(10), analyzer=SpectrumAnalyzer(),
        max_pending_reports=2,
    )
    for n in range(5):
        publisher.submit_report(report(n))
    publisher.tick()
    publisher.submit_report(report(99))
    publisher.tick()

    assert sink.messages("frames")[0]["dropped"] == 3
    assert "dropped" not in sink.messages("frames")[1]


# ----------------------------------------------------- waveforms are latest-wins


def test_stale_waveforms_are_dropped_and_only_the_newest_is_drawn(publisher, sink):
    """Dropping a superseded trace is correct, not a compromise: drawing a 100 ms-old
    waveform costs exactly as much as drawing the current one and is wrong."""
    publisher.set_domain(WaveformDomain.TIME)
    for n in range(1, 11):
        publisher.offer_waveform(n, samples(n))

    publisher.tick()

    headers = sink.waveform_headers()
    assert len(headers) == 1
    assert headers[0].frame_number == 10
    assert publisher.stats().superseded_waveforms == 9


def test_one_waveform_per_tick_regardless_of_input_rate(publisher, sink):
    """100 Hz in, 30 Hz out: the rate-decoupling property, asserted."""
    number = 0
    publisher.set_domain(WaveformDomain.TIME)
    for _ in range(30):
        for _ in range(3):  # ~3 frames per tick at 100 Hz / 30 Hz
            number += 1
            publisher.offer_waveform(number, samples(number))
        publisher.tick()

    assert len(sink.binaries) == 30
    assert [h.frame_number for h in sink.waveform_headers()] == list(range(3, 91, 3))


def test_waveforms_are_not_even_parked_while_nothing_is_watching(publisher, sink):
    """Default domain is NONE. An unwatched plot should cost nothing at all -- not a
    decimation, not an allocation, not a retained 80 kB buffer."""
    assert publisher.domain is WaveformDomain.NONE
    for n in range(10):
        publisher.offer_waveform(n, samples(n))
    publisher.tick()

    assert sink.binaries == []
    assert publisher.stats().superseded_waveforms == 0


def test_switching_to_frequency_domain_changes_what_is_computed(publisher, sink):
    """setDomain is not cosmetic: it decides whether an FFT runs 30 times a second."""
    publisher.set_domain(WaveformDomain.TIME)
    publisher.offer_waveform(1, samples())
    publisher.tick()
    assert sink.waveform_headers()[-1].kind == KIND_TIME_DOMAIN

    publisher.set_domain(WaveformDomain.FREQUENCY)
    publisher.offer_waveform(2, samples())
    publisher.tick()
    assert sink.waveform_headers()[-1].kind == KIND_FREQUENCY_DOMAIN


def test_frequency_axis_is_sent_once_per_geometry_not_once_per_frame(publisher, sink):
    """1 000 floats of JSON at 30 Hz would cost more than every waveform combined."""
    publisher.set_domain(WaveformDomain.FREQUENCY)
    for n in range(1, 6):
        publisher.offer_waveform(n, samples(n))
        publisher.tick()

    assert len(sink.messages("spectrumAxis")) == 1
    assert len(sink.binaries) == 5


def test_axis_is_resent_when_the_tab_is_reopened(publisher, sink):
    """The client may have reloaded and forgotten it. Re-sending 10 kB on a tab switch
    is cheaper than a plot with no x-axis."""
    publisher.set_domain(WaveformDomain.FREQUENCY)
    publisher.offer_waveform(1, samples())
    publisher.tick()

    publisher.set_domain(WaveformDomain.TIME)
    publisher.set_domain(WaveformDomain.FREQUENCY)
    publisher.offer_waveform(2, samples())
    publisher.tick()

    assert len(sink.messages("spectrumAxis")) == 2


def test_leaving_a_plot_discards_the_parked_waveform(publisher, sink):
    publisher.set_domain(WaveformDomain.TIME)
    publisher.offer_waveform(1, samples())
    publisher.set_domain(WaveformDomain.NONE)
    publisher.tick()
    assert sink.binaries == []


def test_validation_flags_reach_the_waveform(publisher, sink):
    publisher.set_domain(WaveformDomain.TIME)
    publisher.offer_waveform(1, samples(), is_valid=False, malformed=True)
    publisher.tick()
    assert sink.waveform_headers()[0].flags == 0b11


def test_a_fault_survives_the_frame_that_carried_it_being_dropped(publisher, sink):
    """The samples are lossy; the diagnosis is not.

    This is the regression that motivated the split. Acquisition and presentation are
    both monotonic-paced, at 100 Hz and 30 Hz, so *which* frame is parked when a tick
    fires is not random -- it cycles through a fixed set of residues. Measured against
    the real simulator with ``--bad-frame-every 5``, that set never contained a faulted
    frame: 0 of 120 waveforms carried FLAG_INVALID while the log correctly showed 79 red
    lines. The red-trace requirement was unreachable, and it failed intermittently under
    load, which made it look like flake rather than a phase lock.

    Here the faulted frame is deliberately the one that gets superseded.
    """
    publisher.set_domain(WaveformDomain.TIME)
    publisher.offer_waveform(1, samples(1), is_valid=False)
    publisher.offer_waveform(2, samples(2))  # healthy, and the one that will be drawn
    publisher.tick()

    header = sink.waveform_headers()[0]
    assert header.frame_number == 2, "latest-wins still picks the newest samples"
    assert header.flags & FLAG_INVALID, "the fault from the dropped frame must survive"


def test_fault_bits_do_not_leak_into_the_next_tick(publisher, sink):
    """A latch that never clears is not a latch, it is a stuck red light."""
    publisher.set_domain(WaveformDomain.TIME)
    publisher.offer_waveform(1, samples(1), is_valid=False)
    publisher.tick()
    publisher.offer_waveform(2, samples(2))
    publisher.tick()

    assert [h.flags for h in sink.waveform_headers()] == [FLAG_INVALID, 0]


# ---------------------------------------------------------------- status messages


def test_status_is_queued_and_delivered_in_order(publisher, sink):
    publisher.publish_status(ConnectionState.CONNECTING, "connecting")
    publisher.publish_status(ConnectionState.CONNECTED, "connected")
    publisher.tick()
    assert sink.states() == ["connecting", "connected"]


def test_log_lines_precede_the_status_change_that_follows_them(publisher, sink):
    """A 'connection dropped' popup must not appear above the last frames that arrived
    before the drop -- the log would then contradict the dialog."""
    publisher.submit_report(report(1))
    publisher.publish_status(ConnectionState.DISCONNECTED, "cable pulled")
    publisher.tick()

    kinds = [text[:20] for text in sink.texts]
    assert '"type":"frames"' in kinds[0]
    assert '"type":"status"' in kinds[1]


def test_smoothed_rate_rides_along_with_the_batch(publisher, sink):
    publisher.submit_report(report(1), 1_999_500.0)
    publisher.tick()
    assert sink.messages("frames")[0]["rateAvg"] == 1_999_500.0


# ------------------------------------------------------------------- concurrency


def test_concurrent_producer_and_consumer_lose_nothing(sink):
    """A producer thread at full speed against a consumer thread ticking as fast as it
    can. Every report must arrive exactly once, in order -- if the lock discipline is
    wrong this is where it shows up."""
    publisher = ThrottledPublisher(
        sink,
        WireCodec(),
        decimator=MinMaxDecimator(1_000),
        analyzer=SpectrumAnalyzer(),
        max_pending_reports=100_000,  # generous: this test is about races, not bounds
    )
    publisher.set_domain(WaveformDomain.TIME)
    total = 5_000
    done = threading.Event()

    def produce():
        buffer = samples()
        for n in range(1, total + 1):
            publisher.submit_report(report(n))
            publisher.offer_waveform(n, buffer)
        done.set()

    def consume():
        while not done.is_set():
            publisher.tick()
        publisher.tick()  # final drain

    producer = threading.Thread(target=produce, name="producer")
    consumer = threading.Thread(target=consume, name="consumer")
    producer.start()
    consumer.start()
    producer.join(timeout=30)
    consumer.join(timeout=30)
    assert not producer.is_alive() and not consumer.is_alive()

    numbers = [item["n"] for item in sink.frame_items()]
    assert numbers == list(range(1, total + 1))  # complete, ordered, no duplicates
    assert publisher.stats().dropped_reports == 0
    # Waveforms, by contrast, are expected to have been dropped -- that is the design.
    assert len(sink.binaries) < total


def test_a_stalled_consumer_never_blocks_the_producer(sink):
    """Serialisation and socket writes happen outside the lock. If they did not, a
    slow browser would apply backpressure all the way to the 100 Hz acquisition
    thread, which is precisely what this class exists to prevent."""
    entered = threading.Event()
    release = threading.Event()

    class BlockingSink(RecordingSink):
        def send_text(self, payload: str) -> None:
            entered.set()
            release.wait(timeout=5)
            super().send_text(payload)

    publisher = ThrottledPublisher(
        BlockingSink(), WireCodec(), decimator=MinMaxDecimator(10), analyzer=SpectrumAnalyzer()
    )
    publisher.submit_report(report(1))

    consumer = threading.Thread(target=publisher.tick, name="stalled-consumer")
    consumer.start()
    assert entered.wait(timeout=5), "consumer never reached the sink"

    # The consumer is now parked mid-send. The producer must sail straight through.
    for n in range(2, 502):
        publisher.submit_report(report(n))
    assert publisher.pending_report_count == 500

    release.set()
    consumer.join(timeout=5)
    assert not consumer.is_alive()


# -------------------------------------------------------------------- lifecycle


def test_start_and_stop_are_idempotent_and_leak_no_threads(publisher):
    before = threading.active_count()
    publisher.start()
    publisher.start()  # second start is a no-op, not a second thread
    assert threading.active_count() == before + 1

    publisher.stop()
    publisher.stop()  # stopping twice is not an error
    assert threading.active_count() == before


def test_the_running_thread_actually_publishes(sink, publisher):
    """One integration-flavoured check that the loop calls tick, since every other test
    here calls tick directly."""
    publisher.start()
    try:
        publisher.submit_report(report(1))
        deadline = threading.Event()
        deadline.wait(0.25)  # ~7 ticks at 30 Hz
    finally:
        publisher.stop()
    assert [item["n"] for item in sink.frame_items()] == [1]


def test_stop_flushes_the_final_status_so_connect_is_re_enabled(sink, publisher):
    """The 'disconnected' status is what re-enables the Connect button. If teardown
    swallowed it the UI would be stuck with Connect greyed out for ever."""
    publisher.start()
    publisher.stop()
    publisher.publish_status(ConnectionState.DISCONNECTED, "dropped")
    publisher.stop()  # idempotent, and flushes
    assert "disconnected" in sink.states()


def test_a_dead_browser_stops_the_publisher_instead_of_raising(sink, publisher):
    """The user closed the tab. That is routine, so it must surface as a flag the route
    can act on, never as an exception escaping a daemon thread."""
    sink.fail_after = 0
    publisher.submit_report(report(1))
    publisher.tick()

    assert publisher.send_failed is True
    publisher.stop()  # must not raise
