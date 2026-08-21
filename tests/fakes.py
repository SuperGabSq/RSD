"""In-memory doubles for the two ports.

Their existence is the payoff of ``application/ports.py``: the entire pipeline --
session, publisher, throttling, state machine, teardown -- is exercised below without a
socket, an event loop, a port to bind, or a sleep to wait out.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator

import numpy as np

from backend.application.ports import UpstreamClosed, UpstreamConnectionError
from backend.infrastructure.wire import HEADER_BYTES, WaveformHeader, decode_header


class RecordingSink:
    """Captures everything the publisher sends, and is safe to read from the test
    thread while the publisher thread writes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.texts: list[str] = []
        self.binaries: list[bytes] = []
        self.fail_after: int | None = None

    def send_text(self, payload: str) -> None:
        self._maybe_fail()
        with self._lock:
            self.texts.append(payload)

    def send_binary(self, payload: bytes) -> None:
        self._maybe_fail()
        with self._lock:
            self.binaries.append(payload)

    def _maybe_fail(self) -> None:
        if self.fail_after is None:
            return
        with self._lock:
            total = len(self.texts) + len(self.binaries)
        if total >= self.fail_after:
            raise OSError("simulated: the browser went away")

    # ------------------------------------------------------------------ readers

    def messages(self, kind: str) -> list[dict]:
        with self._lock:
            texts = list(self.texts)
        return [m for m in (json.loads(t) for t in texts) if m["type"] == kind]

    def frame_items(self) -> list[dict]:
        return [item for message in self.messages("frames") for item in message["items"]]

    def states(self) -> list[str]:
        return [m["state"] for m in self.messages("status")]

    def waveform_headers(self) -> list[WaveformHeader]:
        with self._lock:
            binaries = list(self.binaries)
        return [decode_header(b[:HEADER_BYTES]) for b in binaries]


class FakeUpstream:
    """A scripted microcontroller.

    ``payloads`` are delivered in order; then the source behaves according to
    ``ending``: it either closes cleanly (a drop) or blocks until ``close()`` is called
    (a healthy stream the user disconnects from).
    """

    def __init__(
        self,
        payloads: list[bytes],
        *,
        ending: str = "close",
        fail_to_open: str | None = None,
        texts_before: int = 0,
    ) -> None:
        self.payloads = payloads
        self.ending = ending
        self.fail_to_open = fail_to_open
        self.texts_before = texts_before
        self.opened = False
        self.closed = False
        self.close_count = 0
        self._released = threading.Event()

    def open(self) -> None:
        if self.fail_to_open is not None:
            raise UpstreamConnectionError(self.fail_to_open)
        self.opened = True

    def messages(self) -> Iterator[bytes]:
        for _ in range(self.texts_before):
            pass  # text messages are dropped inside the adapter; nothing is yielded
        for payload in self.payloads:
            if self.closed:
                break
            yield payload
        if self.ending == "close":
            raise UpstreamClosed("the microcontroller closed the connection")
        if self.ending == "raise":
            raise RuntimeError("adapter bug: an untranslated exception")
        # "block": stay open until close() is called, like a healthy stream.
        self._released.wait(timeout=5.0)
        raise UpstreamClosed("closed by client")

    def close(self) -> None:
        self.closed = True
        self.close_count += 1
        self._released.set()


def frame_bytes(sample_count: int = 20_000, seed: int = 0) -> bytes:
    rng = np.random.default_rng(seed)
    return rng.integers(-1000, 1000, size=sample_count, dtype=np.int32).astype("<i4").tobytes()
