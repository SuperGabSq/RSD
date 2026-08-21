"""Frame validation.

The brief requires that the total number of samples per frame be validated and that a
mismatch be logged in red. Two distinct things can be wrong, and conflating them would
lose information the operator needs:

* **Wrong count** -- the payload is a whole number of samples, but not the expected
  20 000. The link is fine; the producer sent a short or long frame.
* **Malformed** -- the payload length is not a multiple of 4, so it does not describe a
  whole number of ``int32_le`` samples at all. That points at truncation or framing
  corruption, not at a mis-sized frame.

Both render red. Only the second sets ``malformed``. We report the truncated count and
move on: we do not realign, and we do not reassemble across frames (assumption #2).
Frame boundaries are the protocol's, not ours to second-guess.
"""

from __future__ import annotations

from dataclasses import dataclass

BYTES_PER_SAMPLE = 4  # int32_le
DEFAULT_EXPECTED_SAMPLES = 20_000


@dataclass(frozen=True, slots=True)
class ValidationResult:
    sample_count: int
    is_valid: bool
    malformed: bool


class FrameValidator:
    """Checks a payload's length against the expected sample count."""

    __slots__ = ("_expected_samples",)

    def __init__(self, expected_samples: int = DEFAULT_EXPECTED_SAMPLES) -> None:
        if expected_samples <= 0:
            raise ValueError("expected_samples must be positive")
        self._expected_samples = expected_samples

    @property
    def expected_samples(self) -> int:
        return self._expected_samples

    def validate(self, payload: bytes) -> ValidationResult:
        malformed = len(payload) % BYTES_PER_SAMPLE != 0
        sample_count = len(payload) // BYTES_PER_SAMPLE  # truncating, deliberately
        is_valid = not malformed and sample_count == self._expected_samples
        return ValidationResult(
            sample_count=sample_count,
            is_valid=is_valid,
            malformed=malformed,
        )
