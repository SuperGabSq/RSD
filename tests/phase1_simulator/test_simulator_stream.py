"""Phase 1 acceptance: does the simulator actually behave like the microcontroller?

Everything downstream is verified *against* this process, so if it lies about its rate
or its frame size, every measurement in Phases 2-7 is measured against the wrong thing.
That makes it the one piece of scaffolding worth testing on its own terms.

These talk to the simulator with a plain ``websockets`` client and import nothing from
``backend/`` except the module under test. Phase 1 predates the backend, and keeping
that true means a failure here can only be the simulator's fault.

Phase 1's original acceptance criterion -- "100 +/- 2 frames/s and 8 MB/s sustained for
60 s" -- was checked by hand over three seconds at the time and never automated. It is
automated here: the short version runs by default, the full 60 s is marked ``slow``.
"""

from __future__ import annotations

import time

import pytest
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

FRAME_BYTES = 80_000
FRAME_SAMPLES = 20_000
NOMINAL_FPS = 100.0
SHORT_RUN_S = 4.0
SUSTAINED_RUN_S = 60.0


def measure(url: str, duration_s: float) -> dict:
    """Read for a fixed wall-clock window and report what arrived.

    Timing starts *after* the first frame: the connection handshake is not part of the
    stream, and charging it against the rate makes short runs read low for no reason.
    """
    sizes: dict[int, int] = {}
    frames = 0
    total_bytes = 0

    with connect(url, max_size=None, compression=None) as ws:
        first = ws.recv()
        start = time.monotonic()
        sizes[len(first)] = 1
        frames = 1
        total_bytes = len(first)

        while time.monotonic() - start < duration_s:
            try:
                payload = ws.recv(timeout=2.0)
            except (ConnectionClosed, TimeoutError):
                break
            frames += 1
            total_bytes += len(payload)
            sizes[len(payload)] = sizes.get(len(payload), 0) + 1

        elapsed = time.monotonic() - start

    return {
        "frames": frames,
        "elapsed": elapsed,
        "fps": frames / elapsed,
        "mb_per_s": total_bytes / elapsed / 1e6,
        "sizes": sizes,
    }


def test_streams_full_size_frames_at_100_hz_and_8_mb_per_second(simulator):
    """The five numbers the whole architecture is derived from, measured rather than
    assumed: 20 000 samples, 80 000 bytes, 100 frames/s, 8 MB/s, 10 ms period."""
    result = measure(simulator().url, SHORT_RUN_S)

    assert result["sizes"] == {FRAME_BYTES: result["frames"]}, "not every frame was 80 kB"
    assert result["fps"] == pytest.approx(NOMINAL_FPS, abs=2.0)
    assert result["mb_per_s"] == pytest.approx(8.0, rel=0.03)
    assert FRAME_BYTES / 4 == FRAME_SAMPLES  # int32_le, stated once so it is not folklore


def test_pacing_does_not_drift(simulator):
    """`sleep(0.01)` in a loop loses a little on every iteration and ends up minutes
    behind after an hour. The simulator schedules against absolute deadlines from a
    fixed start instead, so elapsed time and frame count stay locked together."""
    result = measure(simulator().url, SHORT_RUN_S)
    expected_frames = result["elapsed"] * NOMINAL_FPS
    assert result["frames"] == pytest.approx(expected_frames, rel=0.02)


def test_bad_frame_every_injects_short_frames_and_keeps_streaming(simulator):
    """The red-line requirement's source. A fault frame must be a log line, not an
    incident: the stream carries on around it."""
    sim = simulator("--bad-frame-every", "20")
    result = measure(sim.url, SHORT_RUN_S)

    short_sizes = {size: count for size, count in result["sizes"].items() if size != FRAME_BYTES}
    assert short_sizes, "no fault frames were injected"
    assert set(short_sizes) == {FRAME_BYTES - 20}  # 19 995 samples
    # Roughly one in twenty, and the stream did not stop or slow down around them.
    assert sum(short_sizes.values()) == pytest.approx(result["frames"] / 20, rel=0.35)
    assert result["fps"] == pytest.approx(NOMINAL_FPS, abs=3.0)


def test_bad_frames_stay_a_whole_number_of_samples(simulator):
    """`--bad-frame-every` means *wrong size*, not *corrupt framing*.

    It truncates by 20 bytes -- five whole int32 samples -- so the payload still
    describes a whole number of samples. That distinction is the entire reason the
    backend reports `valid` and `malformed` separately, and it only holds if this flag
    stays on its side of the line.
    """
    result = measure(simulator("--bad-frame-every", "10").url, SHORT_RUN_S)

    assert all(size % 4 == 0 for size in result["sizes"])
    assert FRAME_BYTES - 20 in result["sizes"]


def test_malformed_every_produces_a_payload_that_is_not_whole_samples(simulator):
    """The other failure kind, which had no end-to-end demonstration until now.

    A length that is not a multiple of 4 does not describe int32 samples at all: the
    framing itself is corrupt, rather than the producer having sent the wrong count.
    Phase 2 covers the branch in unit tests; this is what lets the red-line demo -- and
    the Phase 7 screen capture -- show both kinds instead of one.
    """
    result = measure(simulator("--malformed-every", "10").url, SHORT_RUN_S)

    malformed = [size for size in result["sizes"] if size % 4 != 0]
    assert malformed == [FRAME_BYTES - 3]
    assert sum(result["sizes"][size] for size in malformed) == pytest.approx(
        result["frames"] / 10, rel=0.4
    )
    assert result["fps"] == pytest.approx(NOMINAL_FPS, abs=3.0)


def test_the_two_fault_flags_can_run_together(simulator):
    """A demo will want both on screen at once. Malformed wins a shared index -- it is
    the worse diagnosis, and reporting a corrupt frame as merely mis-sized would
    understate it."""
    result = measure(simulator("--bad-frame-every", "8", "--malformed-every", "8").url, SHORT_RUN_S)

    assert FRAME_BYTES - 3 in result["sizes"]
    assert FRAME_BYTES - 20 not in result["sizes"]


def test_drop_after_closes_the_connection(simulator):
    """The connection-dropped popup's source. The count must be exact, because Phase 3
    asserts frames-received == frames-reported against it."""
    sim = simulator("--drop-after", "40")
    received = 0
    with connect(sim.url, max_size=None, compression=None) as ws:
        while True:
            try:
                ws.recv(timeout=5.0)
            except ConnectionClosed:
                break
            received += 1

    assert received == 40


def test_rate_factor_scales_the_stream(simulator):
    """The only way to demonstrate the gauge's out-of-tolerance styling in Phase 6, so
    it needs to actually work before Phase 6 depends on it."""
    result = measure(simulator("--rate-factor", "0.5").url, SHORT_RUN_S)

    assert result["fps"] == pytest.approx(NOMINAL_FPS / 2, abs=2.0)
    assert result["mb_per_s"] == pytest.approx(4.0, rel=0.05)
    # Frames stay full size: --rate-factor changes the cadence, not the payload, so a
    # half-rate stream is 1 Msps rather than 2 Msps of half-frames.
    assert set(result["sizes"]) == {FRAME_BYTES}


def test_the_signal_is_not_constant(simulator):
    """A simulator emitting a flat line would satisfy every rate assertion above and
    tell us nothing about the plot, the decimator, or the FFT."""
    with connect(simulator().url, max_size=None, compression=None) as ws:
        first = ws.recv()
        second = ws.recv()

    import numpy as np

    samples = np.frombuffer(first, dtype="<i4")
    assert samples.std() > 1_000, "the waveform is flat"
    assert first != second, "consecutive frames are identical"


@pytest.mark.slow
def test_sustains_the_rate_for_sixty_seconds(simulator):
    """Phase 1's acceptance criterion as originally written. Short runs cannot catch
    slow drift; this is the one that can."""
    result = measure(simulator().url, SUSTAINED_RUN_S)

    assert result["fps"] == pytest.approx(NOMINAL_FPS, abs=2.0)
    assert result["mb_per_s"] == pytest.approx(8.0, rel=0.02)
    assert set(result["sizes"]) == {FRAME_BYTES}
