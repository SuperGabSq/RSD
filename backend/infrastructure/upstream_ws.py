"""The upstream WebSocket adapter: microcontroller -> backend.

Uses ``websockets.sync.client``, the blocking client from the same library as the async
one -- identical, mature protocol implementation, no event loop. At 100 messages/s,
asyncio would add a scheduler to a thread that does nothing but block on ``recv``.

Three settings are deliberate, and each is a same-day trip hazard if left at its
default:

* ``max_size=None``. 80 kB frames fit inside the library's 1 MB default today, so this
  is not load-bearing *yet* -- which is exactly why it is explicit. The failure mode of
  discovering the cap later is a stream that dies mysteriously at a round number.
* ``compression=None``. The payload is ADC noise, which is incompressible.
  permessage-deflate would spend the entire per-frame CPU budget to make the data
  slightly larger.
* ``open_timeout``. Without it, a host that accepts TCP but never completes the
  handshake leaves the Connect button spinning for ever with no popup.

This adapter's other job is to collapse the library's exception zoo into the two cases
the application distinguishes. Everything below is deliberate translation, not
defensive catch-alls.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator

from websockets.exceptions import (
    ConnectionClosed,
    ConnectionClosedOK,
    InvalidHandshake,
    InvalidURI,
    WebSocketException,
)
from websockets.sync.client import connect

from backend.application.ports import UpstreamClosed, UpstreamConnectionError

log = logging.getLogger(__name__)

DEFAULT_CONNECT_TIMEOUT_S = 5.0


class WebSocketUpstreamSource:
    """One connection to one microcontroller (or the simulator)."""

    def __init__(self, url: str, *, connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S) -> None:
        self._url = url
        self._connect_timeout_s = connect_timeout_s
        self._connection = None
        self._lock = threading.Lock()
        self._closed = False
        self.text_messages_ignored = 0

    @property
    def url(self) -> str:
        return self._url

    def open(self) -> None:
        try:
            connection = connect(
                self._url,
                max_size=None,
                compression=None,
                open_timeout=self._connect_timeout_s,
            )
        except (InvalidURI, ValueError) as exc:
            # ValueError as well as InvalidURI: some malformed inputs (an unterminated
            # IPv6 literal, say) blow up inside urllib before the library ever gets to
            # validate them. Both mean the same thing to the user -- the URL is wrong.
            raise UpstreamConnectionError(f"not a valid WebSocket URL: {self._url}") from exc
        except TimeoutError as exc:
            raise UpstreamConnectionError(
                f"timed out after {self._connect_timeout_s:.0f}s connecting to {self._url}"
            ) from exc
        except OSError as exc:
            # Refused, unreachable, DNS failure. errno-level detail helps nobody in a
            # dialog box, so the message names the thing the user typed.
            raise UpstreamConnectionError(f"could not reach {self._url}: {exc}") from exc
        except InvalidHandshake as exc:
            raise UpstreamConnectionError(
                f"{self._url} answered, but not as a WebSocket server: {exc}"
            ) from exc
        except WebSocketException as exc:
            raise UpstreamConnectionError(f"could not connect to {self._url}: {exc}") from exc

        with self._lock:
            if self._closed:
                # stop() beat us to it: close the connection we just opened rather than
                # leaking it, and report the race as a normal failure to connect.
                connection.close()
                raise UpstreamConnectionError("connection cancelled")
            self._connection = connection

    def messages(self) -> Iterator[bytes]:
        """Yield binary payloads until the stream ends.

        Text messages are counted and skipped, never yielded (assumption #5): only
        binary messages are frames. A microcontroller that logs a line over the same
        socket must not produce a bogus frame with a bogus hash.
        """
        connection = self._connection
        if connection is None:
            raise UpstreamConnectionError("messages() called before open()")

        while True:
            try:
                message = connection.recv()
            except ConnectionClosedOK as exc:
                raise UpstreamClosed("the microcontroller closed the connection") from exc
            except ConnectionClosed as exc:
                raise UpstreamClosed(f"connection lost: {exc}") from exc
            except OSError as exc:
                # close() from another thread pulls the socket out from under recv().
                raise UpstreamClosed(f"connection lost: {exc}") from exc

            if isinstance(message, str):
                self.text_messages_ignored += 1
                continue
            yield message

    def close(self) -> None:
        """Idempotent, and safe to call from another thread while ``messages()`` is
        blocked in ``recv()`` -- that is the only way to unblock it."""
        with self._lock:
            self._closed = True
            connection, self._connection = self._connection, None
        if connection is None:
            return
        try:
            connection.close()
        except Exception:  # noqa: BLE001 - closing a dead socket is not an error
            log.debug("ignoring error while closing upstream socket", exc_info=True)


def make_source_factory(connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S):
    """Bind the timeout so the session can be handed a plain ``url -> source``
    callable and stay ignorant of how sources are configured."""

    def factory(url: str) -> WebSocketUpstreamSource:
        return WebSocketUpstreamSource(url, connect_timeout_s=connect_timeout_s)

    return factory
