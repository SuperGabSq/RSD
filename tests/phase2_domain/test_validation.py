from __future__ import annotations

import pytest

from backend.domain.validation import BYTES_PER_SAMPLE, FrameValidator

EXPECTED = 20_000


@pytest.fixture
def validator() -> FrameValidator:
    return FrameValidator(expected_samples=EXPECTED)


def payload_of(sample_count: int, extra_bytes: int = 0) -> bytes:
    return b"\x00" * (sample_count * BYTES_PER_SAMPLE + extra_bytes)


def test_exact_frame_is_valid(validator):
    result = validator.validate(payload_of(EXPECTED))
    assert result == type(result)(sample_count=EXPECTED, is_valid=True, malformed=False)


def test_short_frame_is_invalid_and_reports_its_real_count(validator):
    """The simulator's --bad-frame-every fault: 19 995 samples instead of 20 000."""
    result = validator.validate(payload_of(19_995))
    assert result.sample_count == 19_995
    assert result.is_valid is False
    assert result.malformed is False  # a whole number of samples, just the wrong number


def test_long_frame_is_invalid(validator):
    result = validator.validate(payload_of(20_001))
    assert result.sample_count == 20_001
    assert result.is_valid is False
    assert result.malformed is False


@pytest.mark.parametrize("extra_bytes", [1, 2, 3])
def test_non_multiple_of_four_is_malformed(validator, extra_bytes):
    """Assumption #2: a payload that is not a whole number of int32_le samples is
    malformed. We report the truncated count and do not attempt to realign."""
    result = validator.validate(payload_of(EXPECTED, extra_bytes=extra_bytes))
    assert result.malformed is True
    assert result.is_valid is False
    assert result.sample_count == EXPECTED  # truncating division, deliberately


def test_malformed_frame_of_otherwise_correct_length_is_still_invalid(validator):
    """80 001 bytes rounds down to 20 000 samples -- the expected count. It must not
    pass on that technicality: the extra byte proves the framing is wrong."""
    result = validator.validate(payload_of(EXPECTED, extra_bytes=1))
    assert result.sample_count == EXPECTED
    assert result.is_valid is False


def test_empty_payload_is_invalid_but_not_malformed(validator):
    result = validator.validate(b"")
    assert result.sample_count == 0
    assert result.is_valid is False
    assert result.malformed is False  # zero is a multiple of four


def test_expected_sample_count_is_configurable():
    """Assumption #1: EXPECTED_SAMPLES is configuration, not a constant."""
    validator = FrameValidator(expected_samples=1_024)
    assert validator.expected_samples == 1_024
    assert validator.validate(payload_of(1_024)).is_valid is True
    assert validator.validate(payload_of(EXPECTED)).is_valid is False


@pytest.mark.parametrize("bad", [0, -1])
def test_rejects_nonsense_configuration(bad):
    with pytest.raises(ValueError):
        FrameValidator(expected_samples=bad)


def test_result_is_immutable(validator):
    result = validator.validate(payload_of(EXPECTED))
    with pytest.raises(AttributeError):
        result.is_valid = False  # type: ignore[misc]
