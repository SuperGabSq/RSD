"""Upstream adapter tests -- the one place in the suite that binds real sockets.

Everything above this file runs against in-memory fakes. This file exists to prove the
fakes are faithful: that the real ``websockets`` client really does raise what the
adapter claims to translate. Without it, the pipeline tests would be asserting against
an imagined library.

Servers bind to port 0 (an ephemeral port the OS chooses) so the suite never collides
with a developer's running simulator and never fails because a fixed port was busy.
"""

from __future__ import annotations

import itertools
import threading
import time

import pytest
from websockets.sync.server import serve

from backend.application.ports import UpstreamClosed, UpstreamConnectionError
from backend.infrastructure.upstream_ws import WebSocketUpstreamSource, make_source_factory

FRAME = b"\x01\x00\x00\x00" * 100

# Server-side pacing, not an assertion timeout. A handler that closes in the same
# breath as its last send can race its own output buffer; these two constants make the
# scripted microcontroller behave like a real one, which sends and then keeps running.
_FLUSH_S = 0.15
_IDLE_S = 10.0


def _stay_open() -> None:
    """Hold the handler open, like a healthy microcontroller between bursts."""
    threading.Event().wait(_IDLE_S)


class LocalServer:
    """A microcontroller stand-in that speaks the real protocol over a real socket."""

    def __init__(self, handler) -> None:
        self._server = serve(handler, "127.0.0.1", 0, max_size=None, compression=None)
        self.port = self._server.socket.getsockname()[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    def close(self) -> None:
        self._server.shutdown()
        self._thread.join(timeout=5)


@pytest.fixture
def server_factory():
    servers: list[LocalServer] = []

    def make(handler) -> LocalServer:
        server = LocalServer(handler)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.close()


# --------------------------------------------------------------------- happy path


def test_receives_binary_frames_in_order(server_factory):
    """The server stays open and the *client* stops reading, so the assertion is about
    ordering only. Whether a server-side close races the last send is a different
    question, tested on its own below with a single frame."""

    def handler(ws):
        for n in range(5):
            ws.send(bytes([n]) + FRAME[1:])
        _stay_open()

    server = server_factory(handler)
    source = WebSocketUpstreamSource(server.url)
    source.open()
    try:
        received = list(itertools.islice(source.messages(), 5))
    finally:
        source.close()

    assert [payload[0] for payload in received] == [0, 1, 2, 3, 4]
    assert all(len(payload) == len(FRAME) for payload in received)


def test_text_messages_are_counted_and_skipped_never_yielded(server_factory):
    """Assumption #5. A microcontroller that logs a line over the same socket must not
    produce a phantom frame with a phantom hash."""

    def handler(ws):
        ws.send("boot complete")
        ws.send(FRAME)
        ws.send("temperature nominal")
        ws.send(FRAME)
        _stay_open()

    server = server_factory(handler)
    source = WebSocketUpstreamSource(server.url)
    source.open()
    try:
        received = list(itertools.islice(source.messages(), 2))
    finally:
        source.close()

    assert all(isinstance(payload, bytes) for payload in received)
    assert len(received) == 2
    assert source.text_messages_ignored == 2


def test_the_factory_binds_the_timeout_so_the_session_stays_ignorant_of_it():
    source = make_source_factory(connect_timeout_s=0.25)("ws://127.0.0.1:1")
    assert isinstance(source, WebSocketUpstreamSource)
    assert source.url == "ws://127.0.0.1:1"


# ------------------------------------------------------------------ failure paths
# Each of these is a real failure at a different layer -- URL parsing, TCP, the
# WebSocket handshake, mid-stream -- collapsed into the two cases the brief cares about.


@pytest.mark.parametrize(
    "url",
    ["not-a-url", "http://example.com/stream", "ws://", "ws://[bad"],
)
def test_an_unusable_url_is_a_connection_error_not_a_library_exception(url):
    source = WebSocketUpstreamSource(url, connect_timeout_s=0.5)
    with pytest.raises(UpstreamConnectionError):
        source.open()


def test_a_refused_port_names_the_url_the_user_typed():
    """errno 111 in a dialog box helps nobody; the URL they typed does."""
    source = WebSocketUpstreamSource("ws://127.0.0.1:1", connect_timeout_s=1.0)
    with pytest.raises(UpstreamConnectionError, match="ws://127.0.0.1:1"):
        source.open()


def test_a_server_that_is_not_a_websocket_server_is_a_connection_error():
    """A plain TCP listener accepts the connection and then fails the handshake -- a
    different code path from 'refused', and a realistic mistake (wrong port)."""
    import socket

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def accept_and_ignore():
        try:
            conn, _ = listener.accept()
            conn.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
            conn.close()
        except OSError:
            pass

    threading.Thread(target=accept_and_ignore, daemon=True).start()
    try:
        source = WebSocketUpstreamSource(f"ws://127.0.0.1:{port}", connect_timeout_s=2.0)
        with pytest.raises(UpstreamConnectionError):
            source.open()
    finally:
        listener.close()


def test_a_mid_stream_kill_is_an_upstream_closed(server_factory):
    """The --drop-after case: we had a stream and lost it. Distinct from never having
    had one, because the UI shows a different dialog."""

    def handler(ws):
        ws.send(FRAME)
        time.sleep(_FLUSH_S)
        ws.close(code=1001, reason="simulated uC disconnect")

    server = server_factory(handler)
    source = WebSocketUpstreamSource(server.url)
    source.open()
    try:
        stream = source.messages()
        assert next(stream) == FRAME
        with pytest.raises(UpstreamClosed):
            next(stream)
    finally:
        source.close()


def test_an_abrupt_socket_death_is_also_an_upstream_closed(server_factory):
    """No close frame, just a dead TCP connection -- what an unplugged cable looks
    like. It must not surface as a raw OSError from a daemon thread."""

    import socket

    def handler(ws):
        ws.send(FRAME)
        time.sleep(_FLUSH_S)
        # Tear the TCP connection down under the protocol: no close frame, no close
        # handshake, exactly what an unplugged cable looks like from our end.
        ws.socket.shutdown(socket.SHUT_RDWR)
        ws.socket.close()

    server = server_factory(handler)
    source = WebSocketUpstreamSource(server.url)
    source.open()
    try:
        stream = source.messages()
        assert next(stream) == FRAME
        with pytest.raises(UpstreamClosed):
            next(stream)
    finally:
        source.close()


def test_close_from_another_thread_unblocks_a_parked_receiver(server_factory):
    """This is how ``AcquisitionSession.stop()`` works: the acquisition thread is
    parked in recv() and nothing but closing the socket underneath it will wake it.
    If this did not hold, every Disconnect would wait out the join timeout."""

    def handler(ws):
        ws.send(FRAME)
        _stay_open()  # then go quiet, like a healthy but idle uC

    server = server_factory(handler)
    source = WebSocketUpstreamSource(server.url)
    source.open()

    closed = threading.Event()
    outcome: list[str] = []

    def consume():
        try:
            for _ in source.messages():
                pass
        except UpstreamClosed:
            outcome.append("closed")
        finally:
            closed.set()

    consumer = threading.Thread(target=consume, daemon=True)
    consumer.start()
    threading.Event().wait(0.2)  # let it park in recv()

    source.close()

    assert closed.wait(timeout=5), "close() did not unblock the parked receiver"
    assert outcome == ["closed"]


def test_close_is_idempotent_and_safe_before_open(server_factory):
    source = WebSocketUpstreamSource("ws://127.0.0.1:1")
    source.close()
    source.close()


def test_messages_before_open_is_a_connection_error_not_an_attribute_error():
    source = WebSocketUpstreamSource("ws://127.0.0.1:1")
    with pytest.raises(UpstreamConnectionError):
        next(source.messages())
