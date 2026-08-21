"""Integration tests for Phase 7 Frequency-Domain (Spectrum) waveform streaming over /stream.

Defends Phase 7 acceptance criteria on real sockets:
- Setting domain to 'fd' delivers spectrumAxis metadata and KIND_FREQUENCY_DOMAIN binary payloads.
- Binary spectrum payloads have an 8-byte header and 1000 float32 dB values (4008 bytes).
- FFT spectrum accurately detects simulator's multi-tone frequencies (50 kHz, 210 kHz, 700 kHz).
- Fault frames (--bad-frame-every) propagate FLAG_INVALID on the frequency-domain header.
- Switching domains ('td', 'fd', 'none') updates stream output without losing log lines.
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
from backend.infrastructure.wire import (
    FLAG_INVALID,
    KIND_FREQUENCY_DOMAIN,
    KIND_TIME_DOMAIN,
    decode_header,
)


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


def test_spectrum_stream_delivers_axis_and_binary_frequency_waveforms(
    running_app: tuple[str, Config],
    simulator,
):
    url, config = running_app
    sim = simulator()

    with websockets.sync.client.connect(url) as ws:
        # Drain initial handshake
        ws.recv(timeout=5)
        ws.recv(timeout=5)

        # Request frequency-domain waveforms and start acquisition
        ws.send(json.dumps({"type": "setDomain", "domain": "fd"}))
        ws.send(json.dumps({"type": "connect", "url": sim.url}))

        spectrum_axes = []
        binary_spectrums = []

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and len(binary_spectrums) < 5:
            raw = ws.recv(timeout=2.0)
            if isinstance(raw, str):
                msg = json.loads(raw)
                if msg.get("type") == "spectrumAxis":
                    spectrum_axes.append(msg)
            elif isinstance(raw, bytes):
                binary_spectrums.append(raw)

        # Verify spectrum axis message was delivered
        assert len(spectrum_axes) >= 1, "Expected spectrumAxis JSON message"
        frequencies = spectrum_axes[0]["frequenciesHz"]
        assert len(frequencies) == config.target_columns
        assert frequencies[0] >= 0.0
        assert frequencies[-1] <= config.nominal_sample_rate_hz / 2.0

        # Verify binary spectrum payloads
        assert len(binary_spectrums) >= 3, "Expected binary spectrum waveforms"
        for raw_spec in binary_spectrums:
            # 8-byte header + 1000 * 4 bytes (float32 dB values) = 4008 bytes
            assert len(raw_spec) == 8 + config.target_columns * 4
            header = decode_header(raw_spec)
            assert header.kind == KIND_FREQUENCY_DOMAIN
            assert header.point_count == config.target_columns
            assert header.frame_number >= 1

            # Validate float32 dB magnitudes
            payload = np.frombuffer(raw_spec[8:], dtype="<f4")
            assert payload.size == config.target_columns
            assert np.all(np.isfinite(payload)), "dB values must be finite numbers"
            assert np.all(payload >= -50.0), "Magnitudes should be well above dB floor"


def test_spectrum_stream_identifies_simulator_spectral_peaks(
    running_app: tuple[str, Config],
    simulator,
):
    url, config = running_app
    sim = simulator()

    with websockets.sync.client.connect(url) as ws:
        ws.recv(timeout=5)
        ws.recv(timeout=5)

        ws.send(json.dumps({"type": "setDomain", "domain": "fd"}))
        ws.send(json.dumps({"type": "connect", "url": sim.url}))

        frequencies = None
        last_payload = None

        # Keep the *latest* of each rather than the first axis paired with a later
        # payload. The axis is derived from the session-mean sample rate, which is still
        # converging over the first second or so of a session, so it is re-sent a few
        # times before it settles -- pairing the opening estimate with a payload from
        # three seconds later labels every bucket with a rate that is no longer current.
        # A real client keeps the most recent axis for the same reason.
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            raw = ws.recv(timeout=2.0)
            if isinstance(raw, str):
                msg = json.loads(raw)
                if msg.get("type") == "spectrumAxis":
                    frequencies = np.array(msg["frequenciesHz"], dtype=np.float64)
            elif isinstance(raw, bytes):
                header = decode_header(raw)
                if header.kind == KIND_FREQUENCY_DOMAIN:
                    last_payload = np.frombuffer(raw[8:], dtype="<f4")

        assert frequencies is not None, "Failed to receive spectrumAxis"
        assert last_payload is not None, "Failed to receive frequency-domain waveform"

        # The simulator injects 3 tones: 50 kHz, 210 kHz, 700 kHz.
        #
        # Searched in a window rather than at the single nearest bin. The frequency axis
        # is derived from the *measured* sample rate (that is the S1 fix -- deriving it
        # from the nominal rate puts every peak 2x out under `--rate-factor 0.5`), and
        # the measurement carries the jitter bias documented in Phase 6, ~1 % on a loaded
        # host. Buckets here are 1 kHz wide, so a 1 % shift at 50 kHz is enough to move
        # the argmin one bucket off the tone and read the noise floor next to it.
        #
        # A window is also what the instrument itself does: S4's peak detector reports
        # the maximum, not the value at a nominal index. Asserting the tone is findable
        # within the estimator's uncertainty is the claim the system can actually make.
        tolerance = 0.02
        for expected_tone_hz in (50_000, 210_000, 700_000):
            window = np.abs(frequencies - expected_tone_hz) <= expected_tone_hz * tolerance
            assert window.any(), f"No bucket within {tolerance:.0%} of {expected_tone_hz} Hz"
            peak_db = float(last_payload[window].max())
            at_hz = float(frequencies[window][np.argmax(last_payload[window])])
            assert peak_db > 75.0, (
                f"Expected a tone within {tolerance:.0%} of {expected_tone_hz} Hz; the "
                f"strongest bucket in that window was {at_hz:.0f} Hz at {peak_db:.1f} dB"
            )


def test_spectrum_stream_flags_invalid_frames(
    running_app: tuple[str, Config],
    simulator,
):
    url, _ = running_app
    sim = simulator("--bad-frame-every", "5")

    with websockets.sync.client.connect(url) as ws:
        ws.recv(timeout=5)
        ws.recv(timeout=5)

        ws.send(json.dumps({"type": "setDomain", "domain": "fd"}))
        ws.send(json.dumps({"type": "connect", "url": sim.url}))

        flagged_spectrums = []
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            raw = ws.recv(timeout=2.0)
            if isinstance(raw, bytes):
                header = decode_header(raw)
                if header.kind == KIND_FREQUENCY_DOMAIN and (header.flags & FLAG_INVALID):
                    flagged_spectrums.append(header)
                    break

        assert flagged_spectrums, "Expected at least one spectrum waveform with FLAG_INVALID set"


def test_domain_switching_between_td_fd_and_none(
    running_app: tuple[str, Config],
    simulator,
):
    url, config = running_app
    sim = simulator()

    with websockets.sync.client.connect(url) as ws:
        ws.recv(timeout=5)
        ws.recv(timeout=5)

        # 1. Start in Time Domain
        ws.send(json.dumps({"type": "setDomain", "domain": "td"}))
        ws.send(json.dumps({"type": "connect", "url": sim.url}))

        td_received = False
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            raw = ws.recv(timeout=2.0)
            if isinstance(raw, bytes):
                header = decode_header(raw)
                if header.kind == KIND_TIME_DOMAIN:
                    td_received = True
                    break
        assert td_received, "Failed to receive Time Domain waveform"

        # 2. Switch to Frequency Domain
        ws.send(json.dumps({"type": "setDomain", "domain": "fd"}))
        fd_received = False
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            raw = ws.recv(timeout=2.0)
            if isinstance(raw, bytes):
                header = decode_header(raw)
                if header.kind == KIND_FREQUENCY_DOMAIN:
                    fd_received = True
                    break
        assert fd_received, "Failed to switch to Frequency Domain waveform"

        # 3. Switch to NONE (background tab simulation).
        #
        # `setDomain` travels one way over a socket, so whatever the publisher had
        # already written when the request was sent is still in flight and will be
        # delivered. Asserting zero binary messages from the instant of the send asserts
        # a property no networked system has, and it failed about one run in three --
        # which read as flake rather than as a test asking for the impossible. The
        # property that is real, and the one that matters, is that the stream *stops*:
        # drain a short settle window, then assert silence over a window long enough to
        # have carried ~45 waveforms at 30 Hz.
        ws.send(json.dumps({"type": "setDomain", "domain": "none"}))
        settle_deadline = time.monotonic() + 0.3
        while time.monotonic() < settle_deadline:
            ws.recv(timeout=1.0)

        deadline = time.monotonic() + 1.5
        none_binary_count = 0
        none_json_count = 0
        while time.monotonic() < deadline:
            raw = ws.recv(timeout=1.5)
            if isinstance(raw, bytes):
                none_binary_count += 1
            elif isinstance(raw, str):
                none_json_count += 1

        assert none_binary_count == 0, "No binary waveforms should arrive when domain is none"
        assert none_json_count > 0, "JSON frame log lines must continue flowing when domain is none"
