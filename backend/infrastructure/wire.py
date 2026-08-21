"""Browser <-> backend wire protocol.

Isolated in one file so the protocol is one testable unit rather than string-building
scattered through the route handler.

The split between the two message families is the whole architecture in miniature:

* **JSON, complete** -- status changes and frame log lines. Small, batched, never
  dropped. A missing log line is a hole in the primary deliverable.
* **Binary, lossy** -- waveforms. Latest-wins; a stale trace is worse than no trace.

Waveform framing is a hand-rolled 8-byte header rather than protobuf or msgpack. There
is exactly one message shape and its fields are fixed-width, so a schema library would
add a dependency, a build step and a parse pass to save nothing::

    offset 0  u32  frameNumber
    offset 4  u8   kind        1 = time domain, 2 = frequency domain
    offset 5  u8   flags       bit0 invalid, bit1 malformed
    offset 6  u16  pointCount
    offset 8  payload

Eight bytes, so the payload starts 4-byte aligned and the browser can wrap it in an
``Int32Array``/``Float32Array`` **view** with no copy. A 7- or 9-byte header would force
a copy of every waveform, thirty times a second, for ever.
"""

from __future__ import annotations

import json
import struct
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from backend.application.ports import ConnectionState, WaveformDomain
from backend.domain.decimation import MinMaxEnvelope
from backend.domain.frame import FrameReport
from backend.domain.spectrum import Spectrum

_HEADER = struct.Struct("<IBBH")
HEADER_BYTES = _HEADER.size

KIND_TIME_DOMAIN = 1
KIND_FREQUENCY_DOMAIN = 2

FLAG_INVALID = 0x01
FLAG_MALFORMED = 0x02

# u32 frame numbers wrap after ~497 days of continuous 100 Hz acquisition. Wrapping is
# the right behaviour for a display counter; silently sending a truncated value would
# not be, so it is explicit.
_FRAME_NUMBER_MODULUS = 1 << 32
_MAX_POINTS = 0xFFFF

# Sub-0.1 samples/s precision on a 2 000 000 samples/s estimate is noise, and this
# runs 100 times a second. Rounding keeps the log payload small without losing anything
# an operator could read.
_RATE_DECIMALS = 1


class ControlDecodeError(ValueError):
    """The browser sent something that is not a control message we understand."""


@dataclass(frozen=True, slots=True)
class ControlMessage:
    type: str
    url: str | None = None
    domain: WaveformDomain | None = None


def _dumps(payload: dict) -> str:
    # Compact separators: at ~30 messages/s the whitespace is the largest single
    # avoidable cost in the JSON path.
    return json.dumps(payload, separators=(",", ":"))


def encode_status(state: ConnectionState, message: str = "") -> str:
    """Drives the popups and the Connect/Disconnect button state."""
    return _dumps({"type": "status", "state": state.value, "message": message})


def encode_frames(
    reports: Sequence[FrameReport],
    *,
    smoothed_rate_hz: float | None = None,
    dropped: int = 0,
) -> str:
    """One batch of log lines.

    ``dropped`` is non-zero only if the browser stalled long enough to overrun the
    bounded report queue. It is reported rather than hidden: silently losing log lines
    in a deliverable whose point is a complete log would be the worst possible failure
    mode, so if it ever happens the operator sees the count.
    """
    items = []
    for report in reports:
        item: dict[str, object] = {
            "n": report.number,
            "ts": report.timestamp,
            "samples": report.sample_count,
            "hash": report.hash,
            "valid": report.is_valid,
            "rate": (
                None
                if report.estimated_rate_hz is None
                else round(report.estimated_rate_hz, _RATE_DECIMALS)
            ),
        }
        if report.malformed:
            # Only present when true: this key is absent on 100 messages/s of healthy
            # traffic, and its presence is the interesting signal.
            item["malformed"] = True
        items.append(item)

    payload: dict[str, object] = {"type": "frames", "items": items}
    if smoothed_rate_hz is not None:
        payload["rateAvg"] = round(smoothed_rate_hz, _RATE_DECIMALS)
    if dropped:
        payload["dropped"] = dropped
    return _dumps(payload)


def encode_spectrum_axis(frequencies_hz: np.ndarray) -> str:
    """The frequency-domain x-axis, sent once when the FD plot becomes visible.

    The buckets are near-uniform but not exactly uniform, so the frontend must not
    reconstruct the axis by interpolation -- in a measurement instrument, "close enough"
    on the frequency axis is how a peak ends up reported at the wrong frequency. Sending
    the exact axis costs ~10 kB, once per tab switch.
    """
    return _dumps(
        {
            "type": "spectrumAxis",
            "frequenciesHz": [round(float(f), 3) for f in frequencies_hz],
        }
    )


def _header(frame_number: int, kind: int, flags: int, point_count: int) -> bytes:
    if point_count > _MAX_POINTS:
        raise ValueError(f"point_count {point_count} exceeds the u16 header field")
    return _HEADER.pack(frame_number % _FRAME_NUMBER_MODULUS, kind, flags, point_count)


def waveform_flags(*, is_valid: bool, malformed: bool) -> int:
    """Carried on the waveform so the plot can tint a suspect trace without waiting for
    the JSON batch that describes the same frame to arrive and be matched up."""
    flags = 0
    if not is_valid:
        flags |= FLAG_INVALID
    if malformed:
        flags |= FLAG_MALFORMED
    return flags


def encode_time_domain(frame_number: int, envelope: MinMaxEnvelope, flags: int = 0) -> bytes:
    """Interleaved [min, max] int32 pairs -- exact raw ADC counts.

    ``pointCount`` is the column count, so the payload is ``2 * pointCount`` int32s.
    Staying int32 all the way to the browser means the number under the cursor is the
    number the instrument received, not a float round-trip of it.
    """
    values = np.ascontiguousarray(envelope.values, dtype="<i4")
    return _header(frame_number, KIND_TIME_DOMAIN, flags, envelope.columns) + values.tobytes()


def encode_frequency_domain(frame_number: int, spectrum: Spectrum, flags: int = 0) -> bytes:
    """dB magnitudes as float32. Unlike the time domain these are derived values, not
    measurements, so float32 loses nothing that was ever real."""
    values = np.ascontiguousarray(spectrum.magnitudes_db, dtype="<f4")
    return (
        _header(frame_number, KIND_FREQUENCY_DOMAIN, flags, values.size) + values.tobytes()
    )


@dataclass(frozen=True, slots=True)
class WaveformHeader:
    frame_number: int
    kind: int
    flags: int
    point_count: int


def decode_header(raw: bytes) -> WaveformHeader:
    """Decode a waveform header.

    The browser does this in JavaScript, not here -- this exists so the tests assert
    against the real bytes rather than against a second, hand-written idea of the
    layout. A protocol verified only by the code that produces it is not verified.
    """
    if len(raw) < HEADER_BYTES:
        raise ValueError(f"waveform message is shorter than its {HEADER_BYTES}-byte header")
    frame_number, kind, flags, point_count = _HEADER.unpack(raw[:HEADER_BYTES])
    return WaveformHeader(frame_number, kind, flags, point_count)


class WireCodec:
    """The :class:`~backend.application.ports.MessageCodec` adapter.

    A thin object wrapper so the publisher receives serialisation as a dependency
    rather than importing this module. The functions above stay module-level and
    directly testable; this class exists only to satisfy the port.
    """

    __slots__ = ()

    encode_status = staticmethod(encode_status)
    encode_frames = staticmethod(encode_frames)
    encode_spectrum_axis = staticmethod(encode_spectrum_axis)
    encode_time_domain = staticmethod(encode_time_domain)
    encode_frequency_domain = staticmethod(encode_frequency_domain)
    waveform_flags = staticmethod(waveform_flags)


def decode_control(raw: str | bytes) -> ControlMessage:
    """Parse a browser -> backend control message.

    Every failure is a :class:`ControlDecodeError`, so the route handler has one thing
    to catch and can answer with a status message. A malformed frame from a browser must
    never reach the client as a traceback.
    """
    if isinstance(raw, bytes):
        raise ControlDecodeError("control messages must be text, not binary")
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ControlDecodeError(f"not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ControlDecodeError("control message must be a JSON object")

    kind = payload.get("type")
    if kind == "connect":
        url = payload.get("url")
        if not isinstance(url, str) or not url.strip():
            raise ControlDecodeError("connect requires a non-empty 'url'")
        return ControlMessage(type="connect", url=url.strip())
    if kind == "disconnect":
        return ControlMessage(type="disconnect")
    if kind == "setDomain":
        try:
            domain = WaveformDomain(payload.get("domain"))
        except ValueError as exc:
            raise ControlDecodeError(f"unknown domain {payload.get('domain')!r}") from exc
        return ControlMessage(type="setDomain", domain=domain)
    raise ControlDecodeError(f"unknown control message type {kind!r}")
