"""Integration tests for the /stream route and Flask app wiring.

Defends Phase 3 acceptance criteria on real sockets:
- /stream initial handshake and ready status.
- Control message flow (connect, disconnect).
- Streaming frames through /stream.
- Single-client enforcement (second client refused with status:error).
- Clean teardown on socket close.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Generator

import pytest
import websockets.sync.client
from werkzeug.serving import make_server

from backend.app import create_app
from backend.config import Config


@pytest.fixture
def running_app() -> Generator[tuple[str, Config], None, None]:
    """Start a real Flask test server on an ephemeral port."""
    config = Config(publish_hz=30.0, expected_samples=20_000)
    app = create_app(config)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    port = server.port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield f"ws://127.0.0.1:{port}/stream", config

    server.shutdown()
    thread.join(timeout=5)


def _recv_status(ws) -> dict:
    while True:
        msg = json.loads(ws.recv(timeout=5))
        if msg.get("type") == "status":
            return msg


def test_stream_initial_connection_and_ready_status(running_app: tuple[str, Config]):
    url, _ = running_app
    with websockets.sync.client.connect(url) as ws:
        status = _recv_status(ws)
        assert status["type"] == "status"
        assert status["state"] == "idle"
        assert status["message"] == "ready"


def test_stream_connect_to_simulator_and_receive_frames(
    running_app: tuple[str, Config],
    simulator,
):
    url, _ = running_app
    sim = simulator()

    with websockets.sync.client.connect(url) as ws:
        # Initial status: ready
        initial = _recv_status(ws)
        assert initial["type"] == "status"
        assert initial["state"] == "idle"

        # Send connect command
        ws.send(json.dumps({"type": "connect", "url": sim.url}))

        # Read messages until we see connected status and frames
        saw_connected = False
        saw_frames = False
        deadline = time.monotonic() + 10.0

        while time.monotonic() < deadline and not (saw_connected and saw_frames):
            msg = json.loads(ws.recv(timeout=5))
            if msg.get("type") == "status":
                if msg.get("state") == "connected":
                    saw_connected = True
            elif msg.get("type") == "frames":
                saw_frames = True
                assert "items" in msg
                assert len(msg["items"]) > 0
                first_item = msg["items"][0]
                assert "n" in first_item
                assert "ts" in first_item
                assert first_item["samples"] == 20_000
                assert "hash" in first_item
                assert first_item["valid"] is True

        assert saw_connected, "never received connected status"
        assert saw_frames, "never received frames batch"

        # Send disconnect
        ws.send(json.dumps({"type": "disconnect"}))
        deadline = time.monotonic() + 5.0
        saw_idle = False
        while time.monotonic() < deadline and not saw_idle:
            msg = json.loads(ws.recv(timeout=5))
            if msg.get("type") == "status" and msg.get("state") == "idle":
                saw_idle = True
        assert saw_idle, "never received idle status after disconnect"


def test_stream_single_client_rejection(running_app: tuple[str, Config]):
    url, _ = running_app

    with websockets.sync.client.connect(url) as ws1:
        ready = _recv_status(ws1)
        assert ready["state"] == "idle"

        # Second client attempts to connect
        with websockets.sync.client.connect(url) as ws2:
            rejected = _recv_status(ws2)
            assert rejected["type"] == "status"
            assert rejected["state"] == "error"
            assert "another browser is already connected" in rejected["message"]


def test_stream_invalid_control_message_reported_without_crashing(
    running_app: tuple[str, Config],
):
    url, _ = running_app
    with websockets.sync.client.connect(url) as ws:
        _recv_status(ws)  # ready
        ws.send("not json")
        response = _recv_status(ws)
        assert response["type"] == "status"
        assert "ignored control message" in response["message"]


def test_closing_the_browser_socket_tears_both_threads_down(
    running_app: tuple[str, Config],
    simulator,
):
    """The gap this module was written to close.

    The handler owns an acquisition thread and a publisher thread. If closing the tab
    left either running, the leak would be invisible during a demo -- the next
    connection works fine -- and fatal after an afternoon of reloads. Nothing else in
    the suite exercises the route's teardown path, because nothing else runs the route.
    """
    url, _ = running_app
    sim = simulator()
    baseline = threading.active_count()

    for cycle in range(3):
        with websockets.sync.client.connect(url) as ws:
            assert _recv_status(ws)["state"] == "idle"
            ws.send(json.dumps({"type": "connect", "url": sim.url}))

            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                message = json.loads(ws.recv(timeout=5))
                if message.get("type") == "frames":
                    break
            else:  # pragma: no cover - only on a broken pipeline
                pytest.fail("never received a frames batch")
        # `with` closed the socket, which is all a closing browser tab does.

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and threading.active_count() > baseline:
            time.sleep(0.05)

        assert threading.active_count() <= baseline, (
            f"threads leaked on cycle {cycle}: "
            f"{threading.active_count()} alive against a baseline of {baseline}"
        )


def test_the_single_client_guard_is_released_when_the_first_browser_leaves(
    running_app: tuple[str, Config],
):
    """Refusing a second browser is only correct if the refusal is temporary. A guard
    that is taken and never released turns one closed tab into a dead instrument until
    the process is restarted."""
    url, _ = running_app

    with websockets.sync.client.connect(url) as first:
        assert _recv_status(first)["state"] == "idle"
        with websockets.sync.client.connect(url) as second:
            assert _recv_status(second)["state"] == "error"

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        with websockets.sync.client.connect(url) as third:
            message = _recv_status(third)
            if message["state"] == "idle":
                return
        time.sleep(0.1)
    pytest.fail("the single-client guard was never released")
