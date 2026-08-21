"""The ``/stream`` WebSocket endpoint: browser <-> backend.

This is the only place the three long-lived objects meet. It is deliberately thin --
it wires a session to a publisher, pumps control messages, and tears both down. All
the behaviour worth testing lives in the application layer, reachable without binding
a port.

**One browser at a time, enforced rather than assumed.** The brief scopes this to a
single client, and session state lives in process memory behind ``gunicorn -w 1``. A
second browser is refused with a status message it can display, because the failure
mode of *not* refusing is two tabs silently fighting over one microcontroller
connection, which looks like a bug in the instrument rather than a misuse of it.

**The handler thread does no work.** It parks in ``receive()`` waiting for control
messages. Frames flow acquisition-thread -> publisher-thread -> socket without passing
through here, which is why a browser that stops reading cannot slow acquisition down.
"""

from __future__ import annotations

import logging
import threading

from flask import Blueprint
from flask_sock import Sock
from simple_websocket import ConnectionClosed

from backend.application.ports import ConnectionState, WaveformDomain
from backend.application.publisher import ThrottledPublisher
from backend.application.session import AcquisitionSession
from backend.config import Config
from backend.domain.decimation import MinMaxDecimator
from backend.domain.hashing import Xxh3_128Hasher
from backend.domain.rate import SampleRateEstimator
from backend.domain.spectrum import SpectrumAnalyzer
from backend.domain.validation import FrameValidator
from backend.infrastructure.upstream_ws import make_source_factory
from backend.infrastructure.wire import ControlDecodeError, WireCodec, decode_control, encode_status

log = logging.getLogger(__name__)

# How long the handler parks before looping. It is a liveness check interval, not a
# latency budget: control messages wake it immediately.
_RECEIVE_TIMEOUT_S = 0.5

_single_client = threading.Lock()


class SockSink:
    """Adapts flask-sock's ``Server`` to the ``DownstreamSink`` port.

    ``ws.send`` already distinguishes text from binary by Python type, so this is two
    lines -- but it is two lines that keep ``flask_sock`` out of the application layer.
    """

    __slots__ = ("_ws",)

    def __init__(self, ws) -> None:
        self._ws = ws

    def send_text(self, payload: str) -> None:
        self._ws.send(payload)

    def send_binary(self, payload: bytes) -> None:
        self._ws.send(payload)


def build_blueprint(config: Config) -> tuple[Blueprint, Sock]:
    bp = Blueprint("stream", __name__)
    sock = Sock()

    @sock.route("/stream", bp=bp)
    def stream(ws):  # pragma: no cover - exercised end-to-end, not by unit tests
        if not _single_client.acquire(blocking=False):
            ws.send(
                encode_status(
                    ConnectionState.ERROR,
                    "another browser is already connected to this backend",
                )
            )
            return
        try:
            _serve(ws, config)
        finally:
            _single_client.release()

    return bp, sock


def _serve(ws, config: Config) -> None:  # pragma: no cover - see module docstring
    publisher = ThrottledPublisher(
        SockSink(ws),
        WireCodec(),
        decimator=MinMaxDecimator(config.target_columns),
        analyzer=SpectrumAnalyzer(
            sample_rate_hz=config.nominal_sample_rate_hz,
            target_bins=config.spectrum_bins,
        ),
        publish_hz=config.publish_hz,
        max_pending_reports=config.max_pending_reports,
    )
    session = AcquisitionSession(
        source_factory=make_source_factory(config.upstream_connect_timeout_s),
        publisher=publisher,
        hasher=Xxh3_128Hasher(),
        validator=FrameValidator(config.expected_samples),
        rate_estimator=SampleRateEstimator(config.rate_ema_alpha),
    )

    publisher.start()
    publisher.publish_config(
        nominal_rate_hz=config.nominal_sample_rate_hz,
        expected_samples=config.expected_samples,
        target_columns=config.target_columns,
    )
    publisher.publish_status(ConnectionState.IDLE, "ready")
    log.info("browser connected")

    try:
        while True:
            if publisher.send_failed:
                break
            try:
                raw = ws.receive(timeout=_RECEIVE_TIMEOUT_S)
            except ConnectionClosed:
                break
            if raw is None:
                continue  # timeout: loop round and re-check publisher health
            _handle_control(raw, session, publisher)
    finally:
        log.info("browser gone; tearing down session")
        session.stop()
        publisher.stop()


def _handle_control(  # pragma: no cover - see module docstring
    raw, session: AcquisitionSession, publisher: ThrottledPublisher
) -> None:
    try:
        message = decode_control(raw)
    except ControlDecodeError as exc:
        # Report against the *current* state. Promoting a typo in a control message to
        # ConnectionState.ERROR would pop a "cannot connect" dialog over a healthy
        # stream.
        publisher.publish_status(session.state, f"ignored control message: {exc}")
        return

    if message.type == "connect":
        session.start(message.url or "")
    elif message.type == "disconnect":
        session.stop()
    elif message.type == "setDomain":
        publisher.set_domain(message.domain or WaveformDomain.NONE)
