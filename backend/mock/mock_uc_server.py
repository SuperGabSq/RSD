"""Standalone microcontroller (uC) simulator for SignalScope.

Streams synthetic 2 Msps / int32_le frames over a WebSocket at a monotonic-clock-paced
100 Hz cadence, matching the brief's protocol exactly:

    - one WebSocket binary message == one frame
    - 20 000 samples/frame, int32_le
    - ~8 MB/s sustained

This is a standalone process (not part of the Flask app), so plain asyncio is fine here —
the "asyncio adds nothing at 100 Hz" argument in the README is about the *backend*, which
has to share a process with Flask/gunicorn. This script has no such constraint.

Fault injection (see README §Assumptions #13):
    --bad-frame-every N   every Nth frame is short by 5 whole samples (19995 instead of
                          20000), to exercise the red-line / sample-count-mismatch path.
    --malformed-every N   every Nth frame is truncated by 3 bytes, so its length is not a
                          multiple of 4 and it does not describe a whole number of
                          int32_le samples at all. A different fault from the one above:
                          that one means the producer sent the wrong size, this one means
                          the framing itself is corrupt, and the backend reports them
                          separately.
    --drop-after N        close the connection after N frames, to exercise the
                          connection-drop popup requirement.
    --rate-factor F       scale the frame rate (0.5 -> ~1 Msps, 2.0 -> ~4 Msps), to exercise
                          the sample-rate gauge / tolerance styling.

Usage:
    python backend/mock/mock_uc_server.py --port 8765
    python backend/mock/mock_uc_server.py --port 8765 --bad-frame-every 50 --drop-after 500
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

import numpy as np
import websockets

# Windows' default timer granularity is ~15.6 ms, which is longer than the 10 ms frame
# period -- `asyncio.sleep` there would wake up late on almost every frame and the
# stream would run at ~64 Hz instead of 100 Hz. Asking the multimedia timer for 1 ms
# resolution fixes it at the source. On Linux the default granularity is already tens
# of microseconds, so there is nothing to fix and nothing to pay for.
if sys.platform == "win32":  # pragma: no cover - platform-specific
    try:
        import ctypes

        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:  # noqa: BLE001 - a simulator must not fail to start over timing
        logging.getLogger("mock_uc").warning(
            "could not raise Windows timer resolution; frame pacing may be coarse"
        )

# Residual jitter after timeBeginPeriod is still ~1 ms on Windows, so the last stretch
# before each deadline is spun rather than slept. It is deliberately short: a spin is a
# fully-consumed CPU core for its duration, and this process is meant to model a
# microcontroller, not to compete with the backend it is feeding. On Linux the sleep
# lands accurately enough that no spin is needed at all.
SPIN_WINDOW_S = 0.0004 if sys.platform == "win32" else 0.0

SAMPLE_RATE_HZ = 2_000_000
FRAME_SAMPLES = 20_000
BASE_FRAME_PERIOD_S = FRAME_SAMPLES / SAMPLE_RATE_HZ  # 0.01 s = 10 ms
# One full second of signal, 100 frames, phase-continuous end-to-end so cycling the
# buffer never introduces a discontinuity (a click) in the waveform.
MASTER_BUFFER_SAMPLES = SAMPLE_RATE_HZ

log = logging.getLogger("mock_uc")


def build_master_buffer(seed: int) -> np.ndarray:
    """Precompute one second of multi-tone + noise signal as raw ADC counts (int32).

    Sum of sines at 50 kHz, 210 kHz, 700 kHz plus Gaussian noise, so the time-domain
    plot shows a real-looking waveform and the (optional) frequency-domain plot shows
    three identifiable peaks. Frequencies are chosen to divide evenly into the 1 Hz
    buffer length so the loop point is phase-continuous (no click when it wraps).
    """
    rng = np.random.default_rng(seed)
    t = np.arange(MASTER_BUFFER_SAMPLES, dtype=np.float64) / SAMPLE_RATE_HZ

    amplitude = 200_000  # raw ADC counts, comfortably inside int32 range
    signal = (
        1.0 * np.sin(2 * np.pi * 50_000 * t)
        + 0.5 * np.sin(2 * np.pi * 210_000 * t)
        + 0.25 * np.sin(2 * np.pi * 700_000 * t)
    )
    signal = signal / np.max(np.abs(signal)) * amplitude
    noise = rng.normal(loc=0.0, scale=amplitude * 0.02, size=MASTER_BUFFER_SAMPLES)
    buf = np.clip(signal + noise, -(2**31), 2**31 - 1).astype("<i4")
    return buf


class FaultConfig:
    def __init__(
        self,
        bad_frame_every: int,
        drop_after: int,
        rate_factor: float,
        malformed_every: int = 0,
    ):
        self.bad_frame_every = bad_frame_every
        self.malformed_every = malformed_every
        self.drop_after = drop_after
        self.rate_factor = rate_factor
        self.period_s = BASE_FRAME_PERIOD_S / rate_factor

    def fault_for(self, frame_index: int) -> str | None:
        """Which fault, if any, this frame carries. Checked in order, so a frame index
        divisible by both flags is reported as malformed -- the worse diagnosis."""
        if frame_index == 0:
            return None  # never fault the first frame; it is the one a demo starts on
        if self.malformed_every and frame_index % self.malformed_every == 0:
            return "malformed"
        if self.bad_frame_every and frame_index % self.bad_frame_every == 0:
            return "short"
        return None


async def stream_to_client(ws, master: np.ndarray, faults: FaultConfig) -> None:
    peer = ws.remote_address
    log.info("client connected: %s", peer)

    frame_index = 0  # 0-based internally; only used to compute buffer offset / fault triggers
    bytes_sent_window = 0
    frames_sent_window = 0
    window_start = time.monotonic()
    start = time.monotonic()

    try:
        while True:
            if faults.drop_after and frame_index >= faults.drop_after:
                log.info("--drop-after %d reached, closing connection", faults.drop_after)
                await ws.close(code=1001, reason="simulated uC disconnect")
                return

            # Absolute deadlines from a fixed start, never `sleep(period)`: sleeping a
            # fixed amount loses whatever the wake-up overshoots, every frame, for ever.
            target_time = start + frame_index * faults.period_s
            delay = target_time - time.monotonic()
            if delay > SPIN_WINDOW_S:
                await asyncio.sleep(delay - SPIN_WINDOW_S)
            while time.monotonic() < target_time:  # no-op when SPIN_WINDOW_S is 0
                pass

            offset = (frame_index * FRAME_SAMPLES) % MASTER_BUFFER_SAMPLES
            frame = master[offset : offset + FRAME_SAMPLES]
            if len(frame) < FRAME_SAMPLES:
                # wrap around the end of the master buffer
                frame = np.concatenate([frame, master[: FRAME_SAMPLES - len(frame)]])

            payload = frame.tobytes()
            fault = faults.fault_for(frame_index)
            if fault == "short":
                payload = payload[:-20]  # 5 fewer whole int32 samples -> 19995
            elif fault == "malformed":
                payload = payload[:-3]  # 79997 B: not a whole number of int32 samples

            await ws.send(payload)

            frame_index += 1
            bytes_sent_window += len(payload)
            frames_sent_window += 1

            elapsed = time.monotonic() - window_start
            if elapsed >= 1.0:
                fps = frames_sent_window / elapsed
                mbps = bytes_sent_window / elapsed / 1e6
                log.info("measured: %.1f frames/s, %.2f MB/s", fps, mbps)
                window_start = time.monotonic()
                bytes_sent_window = 0
                frames_sent_window = 0

    except websockets.exceptions.ConnectionClosed:
        log.info("client disconnected: %s", peer)


async def main_async(args: argparse.Namespace) -> None:
    master = build_master_buffer(args.seed)
    faults = FaultConfig(
        args.bad_frame_every,
        args.drop_after,
        args.rate_factor,
        malformed_every=args.malformed_every,
    )

    log.info(
        "config: expected_rate=%.0f Msps (factor=%.2f), bad_frame_every=%s, "
        "malformed_every=%s, drop_after=%s",
        SAMPLE_RATE_HZ * args.rate_factor / 1e6,
        args.rate_factor,
        args.bad_frame_every or "off",
        args.malformed_every or "off",
        args.drop_after or "off",
    )

    async def handler(ws):
        await stream_to_client(ws, master, faults)

    async with websockets.serve(
        handler,
        args.host,
        args.port,
        max_size=None,
        compression=None,
    ):
        log.info("uC simulator listening on ws://%s:%d", args.host, args.port)
        await asyncio.Future()  # run forever


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument(
        "--bad-frame-every",
        type=int,
        default=0,
        metavar="N",
        help="emit a short (19995-sample) frame every Nth frame; 0 disables (default)",
    )
    p.add_argument(
        "--malformed-every",
        type=int,
        default=0,
        metavar="N",
        help="emit a frame truncated to a non-multiple of 4 bytes every Nth frame; 0 disables",
    )
    p.add_argument(
        "--drop-after",
        type=int,
        default=0,
        metavar="N",
        help="close the connection after N frames; 0 disables (default)",
    )
    p.add_argument(
        "--rate-factor",
        type=float,
        default=1.0,
        metavar="F",
        help="scale the frame rate, e.g. 0.5 -> ~1 Msps, 2.0 -> ~4 Msps (default 1.0)",
    )
    return p.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    args = parse_args()
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
