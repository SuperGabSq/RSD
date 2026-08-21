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
    --bad-frame-every N   every Nth frame is short by 5 samples (19995 instead of 20000),
                          to exercise the red-line / sample-count-mismatch requirement.
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
import time

import numpy as np
import websockets

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
    def __init__(self, bad_frame_every: int, drop_after: int, rate_factor: float):
        self.bad_frame_every = bad_frame_every
        self.drop_after = drop_after
        self.rate_factor = rate_factor
        self.period_s = BASE_FRAME_PERIOD_S / rate_factor


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

            target_time = start + frame_index * faults.period_s
            now = time.monotonic()
            if target_time > now:
                await asyncio.sleep(target_time - now)

            offset = (frame_index * FRAME_SAMPLES) % MASTER_BUFFER_SAMPLES
            frame = master[offset : offset + FRAME_SAMPLES]
            if len(frame) < FRAME_SAMPLES:
                # wrap around the end of the master buffer
                frame = np.concatenate([frame, master[: FRAME_SAMPLES - len(frame)]])

            payload = frame.tobytes()
            is_fault_frame = (
                faults.bad_frame_every > 0
                and frame_index > 0
                and frame_index % faults.bad_frame_every == 0
            )
            if is_fault_frame:
                payload = payload[:-20]  # 5 fewer int32 samples -> 19995

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
    faults = FaultConfig(args.bad_frame_every, args.drop_after, args.rate_factor)

    log.info(
        "config: expected_rate=%.0f Msps (factor=%.2f), bad_frame_every=%s, drop_after=%s",
        SAMPLE_RATE_HZ * args.rate_factor / 1e6,
        args.rate_factor,
        args.bad_frame_every or "off",
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
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
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
