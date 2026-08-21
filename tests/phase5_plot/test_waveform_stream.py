"""Integration tests for Phase 5 Time-Domain waveform streaming over /stream.

Defends Phase 5 acceptance criteria on real sockets:
- /stream initial handshake delivers config telemetry and ready status.
- Setting domain to 'td' delivers binary time-domain waveforms with correct 8-byte header.
- Decoded waveforms contain 1000 min/max int32 pairs (8008 bytes total).
- Fault frames (--bad-frame-every) propagate FLAG_INVALID on the waveform header.
- Setting domain to 'none' stops binary waveform delivery without affecting frame logs.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Generator

import numpy as np
import pytest
import websockets.sync.client
from werkzeug.serving import make_server

from backend.app import create_app
from backend.config import Config
from backend.infrastructure.wire import FLAG_INVALID, KIND_TIME_DOMAIN, decode_header


@pytest.fixture
def running_app() -> Generator[tuple[str, Config], None, None]:
    """Start a real Flask test server on an ephemeral port."""
    config = Config(publish_hz=30.0, expected_samples=20_000, target_columns=1_000)
    app = create_app(config)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    port = server.port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield f"ws://127.0.0.1:{port}/stream", config

    server.shutdown()
    thread.join(timeout=5)


def test_stream_handshake_emits_config_and_ready(running_app: tuple[str, Config]):
    url, config = running_app
    with websockets.sync.client.connect(url) as ws:
        # Handshake sends config then ready status
        messages = [json.loads(ws.recv(timeout=5)), json.loads(ws.recv(timeout=5))]
        config_msg = next((m for m in messages if m.get("type") == "config"), None)
        status_msg = next((m for m in messages if m.get("type") == "status"), None)

        assert config_msg is not None
        assert config_msg["nominalRateHz"] == config.nominal_sample_rate_hz
        assert config_msg["expectedSamples"] == config.expected_samples
        assert config_msg["targetColumns"] == config.target_columns

        assert status_msg is not None
        assert status_msg["state"] == "idle"
        assert status_msg["message"] == "ready"


def test_waveform_stream_delivers_binary_time_domain_waveforms(
    running_app: tuple[str, Config],
    simulator,
):
    url, config = running_app
    sim = simulator()

    with websockets.sync.client.connect(url) as ws:
        # Drain initial handshake
        ws.recv(timeout=5)
        ws.recv(timeout=5)

        # Request time-domain waveforms and start acquisition
        ws.send(json.dumps({"type": "setDomain", "domain": "td"}))
        ws.send(json.dumps({"type": "connect", "url": sim.url}))

        binary_waveforms = []
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and len(binary_waveforms) < 5:
            raw = ws.recv(timeout=2.0)
            if isinstance(raw, bytes):
                binary_waveforms.append(raw)

        assert len(binary_waveforms) >= 3, "Did not receive expected binary waveforms"

        for raw_wf in binary_waveforms:
            # 8-byte header + 1000 * 2 * 4 bytes (int32 min/max pairs) = 8008 bytes
            assert len(raw_wf) == 8 + config.target_columns * 8
            header = decode_header(raw_wf)
            assert header.kind == KIND_TIME_DOMAIN
            assert header.point_count == config.target_columns
            assert header.frame_number >= 1

            # Validate min/max integer data inside payload
            payload = np.frombuffer(raw_wf[8:], dtype="<i4")
            assert payload.size == config.target_columns * 2
            mins = payload[0::2]
            maxs = payload[1::2]
            assert np.all(mins <= maxs), "min values must be <= max values"


def test_waveform_stream_flags_invalid_frames(
    running_app: tuple[str, Config],
    simulator,
):
    """The red trace must be reachable, and not by luck.

    An earlier version of this test broke out of the loop on the first flagged waveform
    and asserted the list was non-empty. It passed alone and failed under load, which
    looked like flake. It was not: waveforms are latest-wins, and acquisition (100 Hz)
    and publication (30 Hz) are both monotonic-paced, so the frame that survives each
    tick cycles through a fixed set of residues that never included a faulted one.
    Measured: 0 flagged waveforms against 79 red log lines.

    So this asserts the ratio, not the existence. One in five frames is short, and the
    fault bits are OR-ed across each tick interval, so most drawn waveforms in a run
    this long should be flagged -- and every short frame must still appear in the log,
    because the log is the complete record and the waveform is not.
    """
    url, _ = running_app
    sim = simulator("--bad-frame-every", "5")

    with websockets.sync.client.connect(url) as ws:
        # Drain handshake
        ws.recv(timeout=5)
        ws.recv(timeout=5)

        ws.send(json.dumps({"type": "setDomain", "domain": "td"}))
        ws.send(json.dumps({"type": "connect", "url": sim.url}))

        waveforms = 0
        flagged = 0
        invalid_log_lines = 0
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            raw = ws.recv(timeout=2.0)
            if isinstance(raw, bytes):
                waveforms += 1
                if decode_header(raw).flags & FLAG_INVALID:
                    flagged += 1
            else:
                message = json.loads(raw)
                if message.get("type") == "frames":
                    invalid_log_lines += sum(
                        1 for item in message["items"] if not item["valid"]
                    )

        assert waveforms >= 30, "the time-domain stream should be running"
        assert invalid_log_lines > 0, "the simulator was asked to inject short frames"
        assert flagged > 0, "invalid frames must be able to tint the trace at all"
        # Every ~3.3rd frame is drawn and every 5th is short, so a tick interval almost
        # always contains one. A wide bound: the claim is "reliably", not a exact ratio.
        assert flagged / waveforms > 0.4, (
            f"only {flagged}/{waveforms} waveforms carried the fault; the flag is being "
            f"lost at the throttling boundary"
        )
        # A drawn waveform can carry the faults of several frames but never more faults
        # than were logged. The log stays the complete record; the trace stays a view.
        assert flagged <= invalid_log_lines


def test_waveform_domain_none_stops_binary_stream(
    running_app: tuple[str, Config],
    simulator,
):
    url, _ = running_app
    sim = simulator()

    with websockets.sync.client.connect(url) as ws:
        ws.recv(timeout=5)
        ws.recv(timeout=5)

        # Set domain to NONE
        ws.send(json.dumps({"type": "setDomain", "domain": "none"}))
        ws.send(json.dumps({"type": "connect", "url": sim.url}))

        json_messages = []
        binary_messages = []
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            raw = ws.recv(timeout=2.0)
            if isinstance(raw, str):
                json_messages.append(raw)
            else:
                binary_messages.append(raw)

        assert len(json_messages) > 0, "JSON frame reports should continue flowing"
        assert len(binary_messages) == 0, "No binary waveforms should be sent when domain is none"
