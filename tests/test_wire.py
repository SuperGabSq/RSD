from __future__ import annotations

import json

import numpy as np
import pytest

from backend.application.ports import ConnectionState, WaveformDomain
from backend.domain.decimation import MinMaxDecimator
from backend.domain.frame import FrameReport, RawFrame
from backend.domain.spectrum import SpectrumAnalyzer
from backend.infrastructure import wire


def report(number=1, *, valid=True, malformed=False, rate=2_000_000.0, samples=20_000):
    from datetime import datetime

    frame = RawFrame(
        number=number,
        payload=b"",
        received_at=datetime(2024, 1, 1, 12, 0, 0),
        monotonic_s=0.0,
    )
    return FrameReport.build(
        frame,
        hash_hex="a" * 32,
        sample_count=samples,
        is_valid=valid,
        malformed=malformed,
        estimated_rate_hz=rate,
    )


# ------------------------------------------------------------------ JSON messages


def test_status_message_carries_the_state_name_the_frontend_switches_on():
    message = json.loads(wire.encode_status(ConnectionState.DISCONNECTED, "cable pulled"))
    assert message == {"type": "status", "state": "disconnected", "message": "cable pulled"}


def test_frames_batch_preserves_order_and_every_field_of_the_log_line():
    batch = json.loads(wire.encode_frames([report(1), report(2), report(3)]))
    assert [item["n"] for item in batch["items"]] == [1, 2, 3]
    first = batch["items"][0]
    assert first["ts"] == "2024-01-01 12:00:00"
    assert first["samples"] == 20_000
    assert first["hash"] == "a" * 32
    assert first["valid"] is True


def test_malformed_key_is_absent_on_healthy_frames():
    """It is present 0 % of the time in normal operation and 100 % of the time when it
    matters, so its presence is the signal. Omitting it also keeps 100 messages/s
    smaller."""
    healthy = json.loads(wire.encode_frames([report()]))["items"][0]
    assert "malformed" not in healthy

    bad = json.loads(wire.encode_frames([report(valid=False, malformed=True)]))["items"][0]
    assert bad["malformed"] is True
    assert bad["valid"] is False


def test_first_frame_rate_survives_as_json_null_not_zero():
    """A missing measurement and a measurement of zero are different claims."""
    item = json.loads(wire.encode_frames([report(rate=None)]))["items"][0]
    assert item["rate"] is None


def test_rate_is_rounded_to_a_readable_precision():
    item = json.loads(wire.encode_frames([report(rate=1_999_987.654321)]))["items"][0]
    assert item["rate"] == 1_999_987.7


def test_smoothed_rate_and_drop_count_appear_only_when_meaningful():
    plain = json.loads(wire.encode_frames([report()]))
    assert "rateAvg" not in plain
    assert "dropped" not in plain

    annotated = json.loads(
        wire.encode_frames([report()], smoothed_rate_hz=1_999_000.0, dropped=7)
    )
    assert annotated["rateAvg"] == 1_999_000.0
    assert annotated["dropped"] == 7


def test_json_is_emitted_without_padding_whitespace():
    """At ~30 messages/s carrying ~4 log lines each, separator whitespace is the
    largest avoidable cost in the JSON path."""
    assert ", " not in wire.encode_frames([report()])


# --------------------------------------------------------------- binary waveforms


def test_time_domain_header_layout_and_payload_alignment():
    envelope = MinMaxDecimator(1_000).decimate(np.arange(20_000, dtype=np.int32))
    message = wire.encode_time_domain(4_242, envelope, flags=0)

    header = wire.decode_header(message)
    assert header.frame_number == 4_242
    assert header.kind == wire.KIND_TIME_DOMAIN
    assert header.point_count == 1_000
    # 8 bytes, so the payload starts 4-byte aligned and the browser can wrap it in an
    # Int32Array view with no copy. This is the whole reason the header is this size.
    assert wire.HEADER_BYTES == 8
    assert (len(message) - wire.HEADER_BYTES) % 4 == 0
    assert len(message) == wire.HEADER_BYTES + 1_000 * 2 * 4


def test_time_domain_values_round_trip_as_exact_int32_counts():
    """No lossy conversion between the ADC and the display."""
    samples = np.arange(-10_000, 10_000, dtype=np.int32)
    envelope = MinMaxDecimator(1_000).decimate(samples)
    message = wire.encode_time_domain(1, envelope)

    decoded = np.frombuffer(message[wire.HEADER_BYTES :], dtype="<i4")
    assert decoded.dtype == np.dtype("<i4")
    assert np.array_equal(decoded, envelope.values)
    assert decoded[0] == -10_000
    assert decoded[-1] == 9_999


def test_frequency_domain_is_float32_dB_with_one_point_per_bin():
    analyzer = SpectrumAnalyzer(target_bins=1_000)
    spectrum = analyzer.analyze(np.zeros(20_000, dtype=np.int32))
    message = wire.encode_frequency_domain(9, spectrum)

    header = wire.decode_header(message)
    assert header.kind == wire.KIND_FREQUENCY_DOMAIN
    assert header.point_count == 1_000
    decoded = np.frombuffer(message[wire.HEADER_BYTES :], dtype="<f4")
    assert decoded.size == 1_000
    assert np.allclose(decoded, spectrum.magnitudes_db)


@pytest.mark.parametrize(
    ("is_valid", "malformed", "expected"),
    [
        (True, False, 0),
        (False, False, wire.FLAG_INVALID),
        (False, True, wire.FLAG_INVALID | wire.FLAG_MALFORMED),
    ],
)
def test_flags_travel_with_the_waveform(is_valid, malformed, expected):
    """So the plot can tint a suspect trace immediately, without waiting for the JSON
    batch describing the same frame and matching the two up by frame number."""
    assert wire.waveform_flags(is_valid=is_valid, malformed=malformed) == expected

    envelope = MinMaxDecimator(10).decimate(np.zeros(100, dtype=np.int32))
    message = wire.encode_time_domain(1, envelope, flags=expected)
    assert wire.decode_header(message).flags == expected


def test_frame_number_wraps_rather_than_overflowing_the_header():
    """u32 wraps after ~497 days of continuous acquisition. Wrapping is correct for a
    display counter; a struct.error at 3 a.m. on day 498 would not be."""
    envelope = MinMaxDecimator(10).decimate(np.zeros(100, dtype=np.int32))
    message = wire.encode_time_domain(2**32 + 5, envelope)
    assert wire.decode_header(message).frame_number == 5


def test_point_count_beyond_the_header_field_is_refused_loudly():
    envelope = MinMaxDecimator(70_000).decimate(np.zeros(70_000, dtype=np.int32))
    with pytest.raises(ValueError, match="u16"):
        wire.encode_time_domain(1, envelope)


def test_decode_header_rejects_a_truncated_message():
    with pytest.raises(ValueError, match="header"):
        wire.decode_header(b"\x00\x01\x02")


def test_spectrum_axis_is_sent_exactly_not_reconstructed():
    """Buckets are near-uniform but not exactly uniform; interpolating the axis is how
    a peak ends up reported at the wrong frequency."""
    spectrum = SpectrumAnalyzer(target_bins=1_000).analyze(np.zeros(20_000, dtype=np.int32))
    message = json.loads(wire.encode_spectrum_axis(spectrum.frequencies_hz))
    assert message["type"] == "spectrumAxis"
    assert len(message["frequenciesHz"]) == 1_000
    assert message["frequenciesHz"] == pytest.approx(
        [round(float(f), 3) for f in spectrum.frequencies_hz]
    )


# ------------------------------------------------------------------- control path


def test_decodes_the_three_control_messages():
    assert wire.decode_control('{"type":"connect","url":" ws://uc:8765 "}') == wire.ControlMessage(
        type="connect", url="ws://uc:8765"
    )
    assert wire.decode_control('{"type":"disconnect"}').type == "disconnect"
    assert wire.decode_control('{"type":"setDomain","domain":"fd"}').domain is (
        WaveformDomain.FREQUENCY
    )


@pytest.mark.parametrize(
    "raw",
    [
        "not json at all",
        "[1,2,3]",
        '{"type":"launch_missiles"}',
        '{"type":"connect"}',
        '{"type":"connect","url":"   "}',
        '{"type":"connect","url":42}',
        '{"type":"setDomain","domain":"sideways"}',
    ],
)
def test_every_malformed_control_message_raises_one_catchable_type(raw):
    """The route has exactly one thing to catch, so a typo from the browser can never
    reach the client as a traceback."""
    with pytest.raises(wire.ControlDecodeError):
        wire.decode_control(raw)


def test_binary_control_messages_are_refused():
    """Binary means frames, and frames only travel the other way."""
    with pytest.raises(wire.ControlDecodeError):
        wire.decode_control(b'{"type":"disconnect"}')
