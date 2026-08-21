"""Throwaway smoke-test client: connects to the uC simulator briefly and reports
measured frame rate / byte rate / frame sizes, including any fault-injected frames seen.
Not part of the deliverable's test suite -- Phase 3 owns the real integration test against
the actual backend pipeline.
"""

from __future__ import annotations

import asyncio
import sys
import time

import websockets


async def probe(url: str, duration_s: float) -> None:
    frame_count = 0
    byte_count = 0
    short_frames = 0
    sizes_seen = set()

    async with websockets.connect(url, max_size=None, compression=None) as ws:
        start = time.monotonic()
        while time.monotonic() - start < duration_s:
            msg = await ws.recv()
            frame_count += 1
            byte_count += len(msg)
            sizes_seen.add(len(msg))
            if len(msg) != 80_000:
                short_frames += 1

    elapsed = time.monotonic() - start
    print(f"duration:        {elapsed:.2f} s")
    print(f"frames received: {frame_count}")
    print(f"measured fps:    {frame_count / elapsed:.2f}")
    print(f"measured MB/s:   {byte_count / elapsed / 1e6:.3f}")
    print(f"frame sizes seen: {sorted(sizes_seen)} bytes")
    print(f"short/fault frames seen: {short_frames}")


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "ws://localhost:8765"
    duration = float(sys.argv[2]) if len(sys.argv) > 2 else 3.0
    asyncio.run(probe(url, duration))
